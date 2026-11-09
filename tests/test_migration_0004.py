"""Migration 0004 on the database 0003 leaves behind.

Two paths reach it in production:

* an upgraded database: the released schema and reference data, July
  dossiers, then 0003, then 0004;
* a fresh install: schema.sql and the released reference data, then 0003
  (which loosens the CHECKs schema.sql declares), then 0004.

Both must end with what schema.sql declares for the columns and constraints
0004 tightens, and 0004 must refuse anything still July-shaped rather than
guess. Each scenario gets a scratch database, as in test_migration_0003.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest

from tests.test_migration_0003 import (
    CURRENT_SQL,
    _build_july_world,
    _counts,
    _july_dossier,
    _scratch,
)
from tests.test_migration_0003 import MIGRATION_SQL as M0003

pytestmark = pytest.mark.asyncio

_ROOT = Path(__file__).resolve().parents[1]
M0004 = (_ROOT / "scripts" / "sql" / "migrations" / "0004_july_lifecycle_removed.sql").read_text(
    encoding="utf-8"
)

# What 0004 makes NOT NULL.
TIGHTENED = {
    "dossier": (
        "id_procedure_definition",
        "regulator",
        "family",
        "scope",
        "opened_by",
        "step",
    ),
    "document": ("kind", "requirement_code"),
    "deadline": ("party", "kind"),
}
# The CHECKs it rewrites without a July branch, and what it drops.
CHECKS = (
    "ck_dossier_procedure",
    "ck_dossier_scope_subject",
    "ck_document_kind",
    "ck_deadline_party_kind",
)
GONE_CHECKS = ("ck_document_requirement",)
GONE_INDEXES = ("ix_dossier_type", "uq_dossier_external_ref", "uq_deadline_open_recurring")


@dataclass(frozen=True)
class Shape:
    nullable: dict[tuple[str, str], str]  # (table, column) -> "YES" | "NO"
    checks: dict[str, str | None]  # name -> definition, None when absent
    gone_indexes: list[str]  # of GONE_INDEXES, those still present


async def _shape(conn: asyncpg.Connection) -> Shape:
    nullable = {
        (row["table_name"], row["column_name"]): row["is_nullable"]
        for row in await conn.fetch(
            "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('dossier', 'document', 'deadline')"
        )
    }
    checks = {
        row["conname"]: row["definition"]
        for row in await conn.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE contype = 'c' AND conrelid IN "
            "('dossier'::regclass, 'document'::regclass, 'deadline'::regclass)"
        )
    }
    indexes = {
        row["indexname"]
        for row in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename IN ('dossier', 'document', 'deadline')"
        )
    }
    return Shape(
        nullable={
            (table, column): nullable[(table, column)]
            for table, columns in TIGHTENED.items()
            for column in columns
        },
        checks={name: checks.get(name) for name in (*CHECKS, *GONE_CHECKS)},
        gone_indexes=sorted(set(GONE_INDEXES) & indexes),
    )


async def _declared() -> Shape:
    """What schema.sql declares, on a database built from it alone."""
    async with _scratch(CURRENT_SQL) as conn:
        return await _shape(conn)


class TestTheShape:
    async def test_an_upgraded_database_ends_as_schema_sql_declares(self, test_db_ready):
        declared = await _declared()
        async with _scratch() as conn:
            await _build_july_world(conn)
            await conn.execute(M0003)
            before = await _counts(conn)
            await conn.execute(M0004)

            assert await _shape(conn) == declared
            # Nothing but the tightening: the converted rows are all still there.
            assert await _counts(conn) == before
            assert await conn.fetchval("SELECT max(version) FROM schema_version") == 4

    async def test_a_fresh_install_gets_back_what_0003_loosened(self, test_db_ready):
        declared = await _declared()
        assert set(declared.nullable.values()) == {"NO"}
        assert declared.checks["ck_document_requirement"] is None
        assert declared.gone_indexes == []
        async with _scratch(CURRENT_SQL) as conn:  # + the released reference data
            await conn.execute(M0003)
            loosened = await _shape(conn)
            await conn.execute(M0004)

            assert loosened != declared, "0003 should have loosened the CHECKs"
            assert await _shape(conn) == declared
            assert await conn.fetchval("SELECT count(*) FROM deadline_rule") == 0
            assert await conn.fetchval("SELECT max(version) FROM schema_version") == 4

    async def test_applying_it_twice_changes_nothing(self, test_db_ready):
        async with _scratch() as conn:
            await _build_july_world(conn)
            await conn.execute(M0003)
            await conn.execute(M0004)
            first = (await _shape(conn), await _counts(conn))
            await conn.execute(M0004)
            assert (await _shape(conn), await _counts(conn)) == first


class TestTheJulyRules:
    async def test_rules_reinserted_after_0003_are_deleted(self, test_db_ready):
        """The dev provisioning re-applied the July seed on every `up`."""
        async with _scratch() as conn:
            await conn.execute(M0003)
            await conn.execute(
                "INSERT INTO deadline_rule (region, dossier_type, trigger_event, "
                " deadline_type, offset_value, offset_unit) "
                "VALUES (1, 1, 'dossier.submitted', 'completeness_check', 10, 1)"
            )
            await conn.execute(M0004)
            assert await conn.fetchval("SELECT count(*) FROM deadline_rule") == 0


class TestItRefusesToGuess:
    async def _refused(self, conn: asyncpg.Connection, message: str) -> None:
        with pytest.raises(asyncpg.RaiseError, match=message):
            await conn.execute(M0004)
        # The script's own BEGIN left the session in the aborted transaction.
        await conn.execute("ROLLBACK")
        assert await conn.fetchval("SELECT max(version) FROM schema_version") == 3
        nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'dossier' AND column_name = 'id_procedure_definition'"
        )
        assert nullable == "YES", "all or nothing: no column was tightened"

    async def test_a_dossier_0003_did_not_convert(self, test_db_ready):
        async with _scratch() as conn:
            await conn.execute(M0003)
            dossier_id = await _july_dossier(conn, dossier_type=5, status=1, operation=7)
            await self._refused(conn, f"July-shaped rows remain.*dossier {dossier_id}")

    async def test_a_july_rule_a_deadline_still_points_at(self, test_db_ready):
        async with _scratch() as conn:
            await conn.execute(M0003)
            rule = await conn.fetchval(
                "INSERT INTO deadline_rule (region, dossier_type, trigger_event, "
                " deadline_type, offset_value, offset_unit) "
                "VALUES (1, 1, 'dossier.submitted', 'completeness_check', 10, 1) RETURNING id"
            )
            dossier_id = await _converted(conn)
            await conn.execute(
                "INSERT INTO deadline (id_community, id_dossier, deadline_type, due_date, "
                " party, kind, id_deadline_rule) "
                "VALUES (1, $1, 'submission', DATE '2026-12-01', 1, 1, $2)",
                dossier_id,
                rule,
            )
            await self._refused(conn, f"July rule still referenced {rule}")


async def _converted(conn: asyncpg.Connection) -> int:
    """A procedure-shaped dossier, as 0003 leaves one, with its journal head."""
    definition = await conn.fetchval(
        "INSERT INTO procedure_definition "
        "(regulator, code, version, valid_from, scope, recipient, channel, family) "
        "VALUES ('BE-WAL-CWAPE', 'TEST_TYPE', 1, DATE '2026-01-01', 2, 1, 1, 'TEST_TYPE') "
        "RETURNING id"
    )
    # One transaction: the journal check is deferred to its COMMIT.
    async with conn.transaction():
        dossier_id: int = await conn.fetchval(
            "INSERT INTO dossier (id_community, id_sharing_operation, status, step, region, "
            " id_procedure_definition, regulator, family, scope, opened_by) "
            "VALUES (1, 7, 1, 'draft', 1, $1, 'BE-WAL-CWAPE', 'TEST_TYPE', 2, 2) RETURNING id",
            definition,
        )
        await conn.execute(
            "INSERT INTO status_event (id_community, subject_type, subject_id, to_status, to_step) "
            "VALUES (1, 2, $1, 1, 'draft')",
            dossier_id,
        )
    return dossier_id
