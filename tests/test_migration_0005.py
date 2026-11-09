"""Migration 0005 on the database 0004 leaves behind.

Two paths reach it, as for 0004: an upgraded database (the released schema, a
July world, 0003, 0004, then 0005) and a fresh install (schema.sql, which
already has all of 0005, then 0003 to 0005). Both must end with what schema.sql
declares for the columns, constraints and triggers 0005 adds; every existing
version must come out FINAL (none was ever stamped), and every piece that can
render must come out pinned to a template.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest

from tests.test_migration_0003 import CURRENT_SQL, _build_july_world, _counts, _scratch
from tests.test_migration_0003 import MIGRATION_SQL as M0003
from tests.test_migration_0004 import M0004

pytestmark = pytest.mark.asyncio

_ROOT = Path(__file__).resolve().parents[1]
M0005 = (_ROOT / "scripts" / "sql" / "migrations" / "0005_generation_finals.sql").read_text(
    encoding="utf-8"
)

COLUMNS = {
    "document_version": ("stage", "watermark", "prefill_json", "template_version", "renderer_json"),
    "document": ("id_template",),
    "document_render": ("purpose", "stage", "watermark", "prefill_json", "id_document_version"),
    "version_check": (
        "id_document_version",
        "docgen_request_id",
        "outcome",
        "content_sha256",
        "renderer_json",
        "error_json",
        "requested_at",
        "checked_at",
    ),
}
CONSTRAINTS = (
    "ck_document_version_stage",
    "ck_document_render_purpose",
    "ck_document_render_stage",
    "ck_version_check_outcome",
    "uq_version_check_request",
)
TRIGGERS = {
    "document_template": ("trg_document_template_in_use",),
    "document_version": ("trg_document_version_after_freeze",),
    "document": ("trg_document_sent_guard",),
    "dossier": ("trg_dossier_sent_guard",),
    "version_check": ("trg_version_check_immutable",),
}


@dataclass(frozen=True)
class Shape:
    columns: dict[tuple[str, str], tuple[str, str, str | None]]  # -> (type, nullable, default)
    constraints: dict[str, str | None]
    triggers: dict[str, list[str]]


async def _shape(conn: asyncpg.Connection) -> Shape:
    columns = {
        (row["table_name"], row["column_name"]): (
            row["data_type"],
            row["is_nullable"],
            row["column_default"],
        )
        for row in await conn.fetch(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        )
    }
    constraints = {
        row["conname"]: row["definition"]
        for row in await conn.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint"
        )
    }
    triggers: dict[str, list[str]] = {}
    for row in await conn.fetch(
        "SELECT event_object_table AS t, trigger_name AS n FROM information_schema.triggers "
        "WHERE trigger_schema = 'public'"
    ):
        triggers.setdefault(row["t"], []).append(row["n"])
    return Shape(
        columns={
            (table, column): columns.get((table, column), ("absent", "", None))
            for table, names in COLUMNS.items()
            for column in names
        },
        constraints={name: constraints.get(name) for name in CONSTRAINTS},
        triggers={
            table: sorted(set(triggers.get(table, [])) & set(names))
            for table, names in TRIGGERS.items()
        },
    )


async def _declared() -> Shape:
    async with _scratch(CURRENT_SQL) as conn:
        return await _shape(conn)


async def _upgraded(conn: asyncpg.Connection) -> None:
    await _build_july_world(conn)
    await conn.execute(M0003)
    await conn.execute(M0004)


async def _template(conn: asyncpg.Connection, doc_type: str) -> int:
    return int(
        await conn.fetchval(
            "SELECT id FROM document_template WHERE doc_type = $1 AND valid_to IS NULL "
            "AND id_community IS NULL",
            doc_type,
        )
    )


class TestTheShape:
    async def test_an_upgraded_database_ends_as_schema_sql_declares(self, test_db_ready):
        declared = await _declared()
        assert all(value[0] != "absent" for value in declared.columns.values())
        assert all(declared.constraints.values())
        async with _scratch() as conn:
            await _upgraded(conn)
            before = await _counts(conn)
            await conn.execute(M0005)

            assert await _shape(conn) == declared
            assert await _counts(conn) == before
            assert await conn.fetchval("SELECT max(version) FROM schema_version") == 5

    async def test_a_fresh_install_is_left_as_it_is(self, test_db_ready):
        declared = await _declared()
        async with _scratch(CURRENT_SQL) as conn:
            await conn.execute(M0003)
            await conn.execute(M0004)
            await conn.execute(M0005)

            assert await _shape(conn) == declared
            assert await conn.fetchval("SELECT max(version) FROM schema_version") == 5

    async def test_applying_it_twice_changes_nothing(self, test_db_ready):
        async with _scratch() as conn:
            await _upgraded(conn)
            await conn.execute(M0005)
            once = (await _shape(conn), await _counts(conn))
            await conn.execute(M0005)

            assert (await _shape(conn), await _counts(conn)) == once


class TestTheRows:
    async def test_every_existing_version_is_final(self, test_db_ready):
        async with _scratch() as conn:
            await _upgraded(conn)
            await conn.execute(M0005)

            stages = await conn.fetch(
                "SELECT DISTINCT stage, watermark FROM document_version ORDER BY stage"
            )
            assert [tuple(row) for row in stages] == [(2, None)]

    async def test_a_piece_that_can_render_is_pinned_to_the_form_in_force(self, test_db_ready):
        async with _scratch() as conn:
            await _upgraded(conn)
            await conn.execute(M0005)

            pinned = {
                row["doc_type"]: row["id_template"]
                for row in await conn.fetch("SELECT doc_type, id_template FROM document")
            }
            assert pinned["annex6_sharing_form"] == await _template(conn, "annex6_sharing_form")
            assert pinned["annex6_notification"] == await _template(conn, "annex6_notification")
            # The July test pieces name no registered form: nothing to pin.
            assert pinned["annex_draft_empty"] is None

    async def test_a_piece_keeps_the_template_its_version_was_rendered_with(self, test_db_ready):
        """A form revised since must not repin a piece that already rendered."""
        async with _scratch() as conn:
            await _upgraded(conn)
            first = await _template(conn, "annex6_sharing_form")
            piece = await conn.fetchval(
                "SELECT id FROM document WHERE doc_type = 'annex6_sharing_form'"
            )
            await conn.execute(
                "INSERT INTO document_version (id_community, id_document, version_no, file_ref, "
                " id_template, docgen_request_id) VALUES (1, $1, 99, 's3://x', $2, 'r-99')",
                piece,
                first,
            )
            # 0004 left the guard off, so this repointing is the July world's own.
            version = await conn.fetchval(
                "SELECT id FROM document_version WHERE docgen_request_id = 'r-99'"
            )
            await conn.execute(
                "UPDATE document SET current_version_id = $1 WHERE id = $2", version, piece
            )
            await conn.execute(
                "UPDATE document_template SET valid_to = DATE '2026-12-31' WHERE id = $1", first
            )
            await conn.execute(
                "INSERT INTO document_template (region, doc_type, version, valid_from, file_ref, "
                " output_format) VALUES (1, 'annex6_sharing_form', 2, DATE '2027-01-01', "
                " 's3://optimce-templates/administrative-document/annex6_sharing_form/v2/', "
                " 'xlsx')"
            )
            await conn.execute(M0005)

            assert await conn.fetchval("SELECT id_template FROM document WHERE id = $1", piece) == (
                first
            )

    async def test_a_sent_piece_is_guarded_once_it_has_run(self, test_db_ready):
        async with _scratch() as conn:
            await _upgraded(conn)
            await conn.execute(M0005)
            sent_piece = await conn.fetchval(
                "SELECT d.id FROM document d JOIN dossier p ON p.id = d.id_dossier "
                "WHERE p.frozen_at IS NOT NULL AND d.current_version_id IS NOT NULL "
                "AND d.kind <> 3 LIMIT 1"
            )
            assert sent_piece is not None

            with pytest.raises(asyncpg.IntegrityConstraintViolationError):
                await conn.execute(
                    "UPDATE document SET current_version_id = NULL WHERE id = $1", sent_piece
                )


class TestTheKeyDescriptionIsRegistered:
    async def test_on_an_upgraded_database(self, test_db_ready):
        async with _scratch() as conn:
            await _upgraded(conn)
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM document_template WHERE doc_type = 'key_description'"
                )
                == 0
            )
            await conn.execute(M0005)

            row = await conn.fetchrow(
                "SELECT region, version, output_format, file_ref, valid_to "
                "FROM document_template WHERE doc_type = 'key_description'"
            )
            assert dict(row) == {
                "region": 1,
                "version": 1,
                "output_format": "pdf",
                "file_ref": "s3://optimce-templates/administrative-document/key_description/v1/",
                "valid_to": None,
            }

    async def test_once_whatever_ran_first(self, test_db_ready):
        """The dev stack's seed 0004 writes the same row; either order is one row."""
        seed = (_ROOT / "scripts" / "sql" / "seeds" / "0004_wal_key_description.sql").read_text(
            encoding="utf-8"
        )
        async with _scratch() as conn:
            await _upgraded(conn)
            await conn.execute(seed)
            await conn.execute(M0005)
            await conn.execute(seed)

            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM document_template WHERE doc_type = 'key_description'"
                )
                == 1
            )
