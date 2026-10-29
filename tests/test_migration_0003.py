"""Migration 0003 on the schema it actually upgrades.

Every other test runs against today's schema.sql. This one builds a database the
way production's was built before 0003 — the released schema (vendored as
tests/sql/schema_before_0003.sql) plus the seeds — fills it with July dossiers of
every type and status, applies scripts/sql/migrations/0003_procedures_engine.sql
as one multi-statement script (as optimce-migrator does), and checks the result
against the mapping agreed before the migration was written.

The migration COMMITs, so it cannot run inside the rolled-back per-test
transaction: each scenario gets a scratch database of its own on the test
server, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engine.catalogue_sync import SyncReport, sync_plugin
from regulators.cwape.plugin import CwapePlugin

pytestmark = pytest.mark.asyncio

_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SQL = (_ROOT / "tests" / "sql" / "schema_before_0003.sql").read_text(encoding="utf-8")
CURRENT_SQL = (_ROOT / "scripts" / "sql" / "schema.sql").read_text(encoding="utf-8")
MIGRATION_SQL = (_ROOT / "scripts" / "sql" / "migrations" / "0003_procedures_engine.sql").read_text(
    encoding="utf-8"
)
SEEDS_SQL = [
    p.read_text(encoding="utf-8") for p in sorted((_ROOT / "scripts/sql/seeds").glob("*.sql"))
]

SERVER = "postgresql://postgres:postgres@localhost:5433"
MIGRATION_DESCRIPTION = (
    "Procedures engine: catalogue, changes, correspondence; July dossiers converted"
)

SUBMITTED_AT = datetime.datetime(2026, 9, 15, 8, 30, tzinfo=datetime.UTC)
CREATED_AT = datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.UTC)

# The agreed mapping, restated independently of the SQL that implements it.
# July type -> (procedure, operation kept?)
TYPES = {
    1: ("CE_NOTIFICATION", False),
    2: ("CE_ACTUALISATION", False),
    3: ("CE_RAPPORT_ANNUEL", False),
    4: ("PARTAGE_AUTORISATION", True),
    5: ("PARTAGE_MODIF", True),
    6: ("PARTAGE_FIN", True),
}
# (operation scope?, July status) -> (ProcedureStatus, step)
STATUSES = {
    (True, 1): (1, "draft"),
    (True, 2): (2, "sent"),
    (True, 3): (2, "in_review"),
    (True, 4): (7, "in_force"),
    (True, 5): (5, "lapsed"),
    (False, 1): (1, "draft"),
    (False, 2): (2, "sent"),
    (False, 3): (7, "in_force"),
    (False, 4): (7, "in_force"),
    (False, 5): (5, "lapsed"),
}
# The journal path a July dossier took to reach each status.
DOSSIER_PATHS = {1: [1], 2: [1, 2], 3: [1, 2, 3], 4: [1, 2, 3, 4], 5: [1, 2, 5]}

# Every status_event column the July schema had.
JOURNAL_COLUMNS = (
    "id, id_community, subject_type, subject_id, from_status, to_status, "
    "is_corrective, actor_id, occurred_at, context_json"
)


def _operation(dossier_type: int) -> int:
    return 100 + dossier_type


# ---------------------------------------------------------------------------
# Scratch databases
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _scratch(schema_sql: str = BASELINE_SQL) -> AsyncIterator[asyncpg.Connection]:
    """A database built from `schema_sql` plus the seeds; dropped on exit."""
    name = f"m0003_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{SERVER}/test_db_be")
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        conn = await asyncpg.connect(f"{SERVER}/{name}")
        try:
            await conn.execute(schema_sql)
            for seed in SEEDS_SQL:
                await conn.execute(seed)
            yield conn
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _journal(
    conn: asyncpg.Connection,
    *,
    subject_type: int,
    subject_id: int,
    path: list[int],
    contexts: dict[int, dict] | None = None,
) -> None:
    previous = None
    for to_status in path:
        await conn.execute(
            "INSERT INTO status_event (id_community, subject_type, subject_id, from_status, "
            " to_status, actor_id, context_json) "
            "VALUES (1, $1, $2, $3, $4, 'auth-user-1', $5::jsonb)",
            subject_type,
            subject_id,
            previous,
            to_status,
            json.dumps((contexts or {}).get(to_status, {})),
        )
        previous = to_status


async def _july_dossier(
    conn: asyncpg.Connection,
    *,
    dossier_type: int,
    status: int,
    operation: int | None = None,
    region: int = 1,
) -> int:
    async with conn.transaction():
        dossier_id = await conn.fetchval(
            "INSERT INTO dossier (id_community, id_sharing_operation, dossier_type, status, "
            " region, submitted_at, created_at) VALUES (1, $1, $2, $3, $4, $5, $6) RETURNING id",
            operation if operation is not None else _operation(dossier_type),
            dossier_type,
            status,
            region,
            SUBMITTED_AT if status >= 2 else None,
            CREATED_AT,
        )
        await _journal(
            conn,
            subject_type=2,
            subject_id=dossier_id,
            path=DOSSIER_PATHS[status],
            contexts={2: {"submission_date": "2026-09-15"}},
        )
    return int(dossier_id)


async def _july_document(
    conn: asyncpg.Connection,
    *,
    dossier_id: int,
    doc_type: str,
    status: int,
    origin: int = 2,
    with_version: bool = False,
    ack: dict | None = None,
) -> int:
    paths = {1: [1], 2: [1, 2], 3: [1, 2, 3], 4: [1, 2, 3, 4], 5: [1, 5]}
    async with conn.transaction():
        document_id = await conn.fetchval(
            "INSERT INTO document (id_community, id_dossier, doc_type, origin, status, title) "
            "VALUES (1, $1, $2, $3, $4, $2) RETURNING id",
            dossier_id,
            doc_type,
            origin,
            status,
        )
        if with_version:
            version_id = await conn.fetchval(
                "INSERT INTO document_version (id_community, id_document, version_no, file_ref) "
                "VALUES (1, $1, 1, 's3://bucket/key') RETURNING id",
                document_id,
            )
            await conn.execute(
                "UPDATE document SET current_version_id = $1 WHERE id = $2", version_id, document_id
            )
        await _journal(
            conn,
            subject_type=1,
            subject_id=document_id,
            path=paths[status],
            contexts={3: {"submission_date": "2026-09-15"}, 4: ack or {}},
        )
    return int(document_id)


async def _july_deadline(
    conn: asyncpg.Connection, *, dossier_id: int, deadline_type: str, recurring: bool = False
) -> int:
    rule = await conn.fetchval(
        "SELECT id FROM deadline_rule WHERE deadline_type = $1 AND id_community IS NULL",
        deadline_type,
    )
    return int(
        await conn.fetchval(
            "INSERT INTO deadline (id_community, id_dossier, deadline_type, due_date, recurring, "
            " id_deadline_rule) VALUES (1, $1, $2, DATE '2026-10-01', $3, $4) RETURNING id",
            dossier_id,
            deadline_type,
            recurring,
            rule,
        )
    )


@dataclass
class JulyWorld:
    dossiers: dict[tuple[int, int], int] = field(default_factory=dict)  # (type, status) -> id
    documents: dict[str, int] = field(default_factory=dict)
    deadlines: dict[str, int] = field(default_factory=dict)
    journal_before: list[dict] = field(default_factory=list)


async def _build_july_world(conn: asyncpg.Connection) -> JulyWorld:
    world = JulyWorld()
    for dossier_type in TYPES:
        for status in DOSSIER_PATHS:
            world.dossiers[(dossier_type, status)] = await _july_dossier(
                conn, dossier_type=dossier_type, status=status
            )

    sent_sharing = world.dossiers[(5, 2)]
    for name, status, version in (
        ("draft_empty", 1, False),
        ("draft_with_version", 1, True),
        ("ready", 2, True),
        ("sent", 3, True),
        ("obsolete", 5, False),
    ):
        world.documents[name] = await _july_document(
            conn,
            dossier_id=sent_sharing,
            doc_type=f"annex_{name}",
            status=status,
            with_version=version,
        )
    world.documents["acknowledged"] = await _july_document(
        conn,
        dossier_id=sent_sharing,
        doc_type="annex6_sharing_form",
        status=4,
        origin=1,
        with_version=True,
        ack={
            "acknowledged_date": "2026-09-29",
            "authority_file_ref": "ORES-2026-0042",
            "result": "complete",
        },
    )
    sent_ce = world.dossiers[(1, 2)]
    world.documents["incomplete"] = await _july_document(
        conn,
        dossier_id=sent_ce,
        doc_type="annex6_notification",
        status=4,
        origin=1,
        with_version=True,
        ack={
            "acknowledged_date": "2026-09-30",
            "authority_file_ref": "CWAPE-CE-77",
            "result": "incomplete",
        },
    )

    world.deadlines["completeness_check"] = await _july_deadline(
        conn, dossier_id=sent_sharing, deadline_type="completeness_check"
    )
    world.deadlines["completeness_check_ce"] = await _july_deadline(
        conn, dossier_id=sent_ce, deadline_type="completeness_check"
    )
    world.deadlines["lapse"] = await _july_deadline(conn, dossier_id=sent_ce, deadline_type="lapse")
    world.deadlines["modification_notification"] = await _july_deadline(
        conn, dossier_id=world.dossiers[(2, 2)], deadline_type="modification_notification"
    )
    world.deadlines["annual_report"] = await _july_deadline(
        conn, dossier_id=world.dossiers[(1, 3)], deadline_type="annual_report", recurring=True
    )
    # A community override of a July rule: rules come from the catalogue now.
    await conn.execute(
        "INSERT INTO deadline_rule (id_community, region, dossier_type, trigger_event, "
        " deadline_type, offset_value, offset_unit) "
        "VALUES (1, 1, 1, 'dossier.submitted', 'completeness_check', 12, 1)"
    )

    world.journal_before = [
        dict(row)
        # A module constant, not input.
        for row in await conn.fetch(f"SELECT {JOURNAL_COLUMNS} FROM status_event ORDER BY id")  # noqa: S608
    ]
    return world


# ---------------------------------------------------------------------------
# The migrated July world — built once, read by the tests below
# ---------------------------------------------------------------------------


@dataclass
class Migrated:
    dsn: str
    world: JulyWorld


async def _migrate_a_july_world(name: str) -> JulyWorld:
    """Create database `name` as production's was, fill it, apply 0003."""
    admin = await asyncpg.connect(f"{SERVER}/test_db_be")
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    conn = await asyncpg.connect(f"{SERVER}/{name}")
    try:
        await conn.execute(BASELINE_SQL)
        for seed in SEEDS_SQL:
            await conn.execute(seed)
        world = await _build_july_world(conn)
        await conn.execute(MIGRATION_SQL)
        return world
    finally:
        await conn.close()


async def _drop(name: str) -> None:
    admin = await asyncpg.connect(f"{SERVER}/test_db_be")
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


@pytest.fixture(scope="module")
def migrated(test_db_ready) -> Iterator[Migrated]:
    """Sync fixture driving asyncio.run, like conftest.py's schema fixture: an
    async module-scoped fixture would outlive its event loop on Windows."""
    name = f"m0003_{uuid.uuid4().hex[:12]}"
    try:
        world = asyncio.run(_migrate_a_july_world(name))
        yield Migrated(dsn=f"{SERVER}/{name}", world=world)
    finally:
        asyncio.run(_drop(name))


@contextlib.asynccontextmanager
async def _read(migrated: Migrated | Synced) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated.dsn)
    try:
        yield conn
    finally:
        await conn.close()


class TestDossiersBecomeProcedures:
    async def test_every_july_type_and_status_lands_where_agreed(self, migrated):
        async with _read(migrated) as conn:
            rows = {
                r["id"]: r
                for r in await conn.fetch(
                    "SELECT d.*, pd.code, pd.regulator AS pd_regulator FROM dossier d "
                    "JOIN procedure_definition pd ON pd.id = d.id_procedure_definition"
                )
            }
        assert len(rows) == len(migrated.world.dossiers)
        for (dossier_type, status), dossier_id in migrated.world.dossiers.items():
            row = rows[dossier_id]
            code, keeps_operation = TYPES[dossier_type]
            assert row["code"] == code == row["family"]
            assert row["regulator"] == row["pd_regulator"] == "BE-WAL-CWAPE"
            assert (row["status"], row["step"]) == STATUSES[(keeps_operation, status)], (
                dossier_type,
                status,
            )
            assert row["scope"] == (2 if keeps_operation else 1)
            assert row["id_sharing_operation"] == (
                _operation(dossier_type) if keeps_operation else None
            )
            assert row["opened_by"] == 3  # OpenedBy.MIGRATION

    async def test_what_a_dossier_loses_is_kept_in_its_metadata(self, migrated):
        dossier_id = migrated.world.dossiers[(1, 2)]
        async with _read(migrated) as conn:
            metadata = json.loads(
                await conn.fetchval("SELECT metadata_json FROM dossier WHERE id = $1", dossier_id)
            )
        assert metadata["legacy"] == {
            "migration": "0003",
            "dossier_type": 1,
            "status": 2,
            "id_sharing_operation": _operation(1),
        }

    async def test_a_modification_takes_the_authorization_route(self, migrated):
        async with _read(migrated) as conn:
            variants = {
                r["family"]: r["variant"]
                for r in await conn.fetch("SELECT DISTINCT family, variant FROM dossier")
            }
        assert variants["PARTAGE_MODIF"] == "authorization"
        assert {f for f, v in variants.items() if v is not None} == {"PARTAGE_MODIF"}

    async def test_a_sent_procedure_is_frozen_and_a_draft_is_not(self, migrated):
        async with _read(migrated) as conn:
            frozen = {
                r["id"]: r["frozen_at"]
                for r in await conn.fetch("SELECT id, frozen_at FROM dossier")
            }
        for (_, status), dossier_id in migrated.world.dossiers.items():
            if status == 1:
                assert frozen[dossier_id] is None
            else:
                assert frozen[dossier_id] == SUBMITTED_AT

    async def test_an_annual_report_gets_its_period(self, migrated):
        async with _read(migrated) as conn:
            periods = {
                r["family"]: r["period_key"]
                for r in await conn.fetch("SELECT DISTINCT family, period_key FROM dossier")
            }
        assert periods.pop("CE_RAPPORT_ANNUEL") == "2026"
        assert set(periods.values()) == {None}

    async def test_the_catalogue_rows_are_reserved_for_the_sync(self, migrated):
        async with _read(migrated) as conn:
            rows = await conn.fetch(
                "SELECT code, version, scope, recipient, channel, family, content_sha256 "
                "FROM procedure_definition ORDER BY code"
            )
        assert {r["code"] for r in rows} == {code for code, _ in TYPES.values()}
        for row in rows:
            operation = row["code"].startswith("PARTAGE_")
            assert row["version"] == 1
            assert row["content_sha256"] is None  # filled by the catalogue sync
            assert row["family"] == row["code"]
            assert (row["scope"], row["recipient"], row["channel"]) == (
                (2, 1, 1) if operation else (1, 2, 2)
            )

    async def test_the_version_is_recorded(self, migrated):
        async with _read(migrated) as conn:
            description = await conn.fetchval(
                "SELECT description FROM schema_version WHERE version = 3"
            )
        assert description == MIGRATION_DESCRIPTION


class TestTheJournalIsExtendedNotRewritten:
    async def test_the_july_history_is_untouched(self, migrated):
        before = migrated.world.journal_before
        async with _read(migrated) as conn:
            after = await conn.fetch(
                f"SELECT {JOURNAL_COLUMNS}, from_step, to_step FROM status_event "  # noqa: S608
                "WHERE id = ANY($1::bigint[]) ORDER BY id",
                [row["id"] for row in before],
            )
        assert [{column: row[column] for column in before[0]} for row in after] == before
        assert all(row["from_step"] is None and row["to_step"] is None for row in after)

    async def test_each_dossier_gets_one_migration_event(self, migrated):
        async with _read(migrated) as conn:
            events = await conn.fetch(
                "SELECT subject_id, from_status, to_status, from_step, to_step, actor_id, "
                " context_json FROM status_event "
                "WHERE subject_type = 2 AND context_json ->> 'migration' = '0003'"
            )
        by_dossier = {e["subject_id"]: e for e in events}
        assert len(events) == len(by_dossier) == len(migrated.world.dossiers)
        for (dossier_type, status), dossier_id in migrated.world.dossiers.items():
            event = by_dossier[dossier_id]
            expected = STATUSES[(TYPES[dossier_type][1], status)]
            assert event["from_status"] == status
            assert (event["to_status"], event["to_step"]) == expected
            assert event["from_step"] is None
            assert event["actor_id"] == "system:migration"
            assert json.loads(event["context_json"]) == {
                "migration": "0003",
                "legacy_status": status,
            }

    async def test_the_new_triggers_guard_a_migrated_database(self, migrated):
        """The upgrade path installs the same guard as a fresh schema.sql."""
        dossier_id = migrated.world.dossiers[(5, 1)]
        async with _read(migrated) as conn:
            with pytest.raises(asyncpg.IntegrityConstraintViolationError, match="never directly"):
                await conn.execute("UPDATE dossier SET status = 6 WHERE id = $1", dossier_id)


class TestDocuments:
    @pytest.mark.parametrize(
        ("name", "status", "required", "kind"),
        [
            ("draft_empty", 6, True, 2),  # MISSING, collected
            ("draft_with_version", 2, True, 2),  # READY
            ("ready", 2, True, 2),
            ("sent", 2, True, 2),
            ("acknowledged", 2, True, 1),  # generated
            ("obsolete", 5, False, 2),
        ],
    )
    async def test_a_july_document_becomes_a_piece(self, migrated, name, status, required, kind):
        async with _read(migrated) as conn:
            row = await conn.fetchrow(
                "SELECT status, required, kind, requirement_code, doc_type FROM document "
                "WHERE id = $1",
                migrated.world.documents[name],
            )
            journaled = await conn.fetchval(
                "SELECT count(*) FROM status_event WHERE subject_type = 1 AND subject_id = $1 "
                "AND context_json ->> 'migration' = '0003'",
                migrated.world.documents[name],
            )
        assert (row["status"], row["required"], row["kind"]) == (status, required, kind)
        assert row["requirement_code"] == row["doc_type"]
        assert journaled == 1


class TestCorrespondenceIsReplayed:
    async def test_every_submission_is_an_outbound_message(self, migrated):
        async with _read(migrated) as conn:
            rows = await conn.fetch(
                "SELECT c.id_dossier, c.direction, c.counterparty, c.channel, c.occurred_at "
                "FROM correspondence c WHERE c.type = 'sent'"
            )
        submitted = {
            dossier_id: dossier_type
            for (dossier_type, status), dossier_id in migrated.world.dossiers.items()
            if status >= 2
        }
        assert sorted(r["id_dossier"] for r in rows) == sorted(submitted)
        for row in rows:
            operation = TYPES[submitted[row["id_dossier"]]][1]
            assert row["direction"] == 1
            # grd + email for a sharing procedure, regulator + online form for a CE one
            assert (row["counterparty"], row["channel"]) == ((1, 1) if operation else (2, 2))
            # The declared submission date, at Brussels midnight.
            assert row["occurred_at"] == datetime.datetime(2026, 9, 14, 22, 0, tzinfo=datetime.UTC)

    async def test_acknowledgements_are_inbound_answers(self, migrated):
        async with _read(migrated) as conn:
            rows = {
                r["type"]: r
                for r in await conn.fetch(
                    "SELECT id_dossier, direction, counterparty, type, external_ref, occurred_at "
                    "FROM correspondence WHERE direction = 2"
                )
            }
        assert set(rows) == {"completeness_ack", "incomplete_notice"}
        ack = rows["completeness_ack"]
        assert ack["id_dossier"] == migrated.world.dossiers[(5, 2)]
        assert (ack["counterparty"], ack["external_ref"]) == (1, "ORES-2026-0042")
        notice = rows["incomplete_notice"]
        assert notice["id_dossier"] == migrated.world.dossiers[(1, 2)]
        assert (notice["counterparty"], notice["external_ref"]) == (2, "CWAPE-CE-77")

    async def test_the_authority_reference_becomes_the_procedures(self, migrated):
        async with _read(migrated) as conn:
            refs = {
                r["id"]: r["external_ref"]
                for r in await conn.fetch(
                    "SELECT id, external_ref FROM dossier WHERE external_ref IS NOT NULL"
                )
            }
        assert refs == {
            migrated.world.dossiers[(5, 2)]: "ORES-2026-0042",
            migrated.world.dossiers[(1, 2)]: "CWAPE-CE-77",
        }


class TestDeadlines:
    @pytest.mark.parametrize(
        ("name", "code", "party", "kind"),
        [
            ("completeness_check", "completeness", 2, 2),  # the GRD answers
            ("completeness_check_ce", "completeness", 3, 2),  # the CWaPE answers
            ("lapse", "lapse", 1, 3),
            ("modification_notification", "submission", 1, 1),
            ("annual_report", "submission", 1, 1),
        ],
    )
    async def test_an_open_deadline_keeps_its_date_and_gains_who_and_what(
        self, migrated, name, code, party, kind
    ):
        async with _read(migrated) as conn:
            row = await conn.fetchrow(
                "SELECT deadline_type, party, kind, due_date, recurring, id_deadline_rule, status "
                "FROM deadline WHERE id = $1",
                migrated.world.deadlines[name],
            )
        assert (row["deadline_type"], row["party"], row["kind"]) == (code, party, kind)
        assert row["due_date"] == datetime.date(2026, 10, 1)
        assert row["status"] == 1  # still open
        assert row["recurring"] is False  # each year is its own procedure now
        assert row["id_deadline_rule"] is None  # relinked by the catalogue sync

    async def test_the_july_rules_are_gone_overrides_included(self, migrated):
        async with _read(migrated) as conn:
            assert await conn.fetchval("SELECT count(*) FROM deadline_rule") == 0


# ---------------------------------------------------------------------------
# Re-running, fresh installs, and the cases it refuses to guess
# ---------------------------------------------------------------------------


async def _counts(conn: asyncpg.Connection) -> dict[str, int]:
    tables = (
        "dossier",
        "document",
        "status_event",
        "correspondence",
        "deadline",
        "procedure_definition",
        "deadline_rule",
    )
    # Table names from the tuple above, not input.
    return {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in tables}  # noqa: S608


class TestReruns:
    async def test_applying_it_twice_changes_nothing_the_second_time(self, test_db_ready):
        async with _scratch() as conn:
            await _build_july_world(conn)
            await conn.execute(MIGRATION_SQL)
            first = await _counts(conn)
            states = await conn.fetch(
                "SELECT id, status, step, updated_at FROM dossier ORDER BY id"
            )
            await conn.execute(MIGRATION_SQL)
            assert await _counts(conn) == first
            assert (
                await conn.fetch("SELECT id, status, step, updated_at FROM dossier ORDER BY id")
                == states
            )

    async def test_on_a_fresh_install_it_only_drops_the_july_rules(self, test_db_ready):
        """schema.sql already embodies 0003 (and self-inserts only version 1)."""
        async with _scratch(CURRENT_SQL) as conn:
            before = await _counts(conn)
            await conn.execute(MIGRATION_SQL)
            after = await _counts(conn)
            assert after == {**before, "deadline_rule": 0}
            assert await conn.fetchval("SELECT max(version) FROM schema_version") == 3


class TestItRefusesToGuess:
    async def _refused(self, conn: asyncpg.Connection, message: str) -> None:
        with pytest.raises(asyncpg.RaiseError, match=message):
            await conn.execute(MIGRATION_SQL)
        # The script's own BEGIN left the session in the aborted transaction.
        await conn.execute("ROLLBACK")
        # All or nothing: the schema part rolled back with the data part.
        assert await conn.fetchval("SELECT to_regclass('procedure_definition')") is None
        assert await conn.fetchval("SELECT max(version) FROM schema_version") == 2

    async def test_two_drafts_that_would_share_a_drawer(self, test_db_ready):
        async with _scratch() as conn:
            first = await _july_dossier(conn, dossier_type=5, status=1, operation=7)
            second = await _july_dossier(conn, dossier_type=5, status=1, operation=7)
            await self._refused(conn, f"PARTAGE_MODIF: dossiers {first}, {second}")

    async def test_two_ce_drafts_of_different_operations_also_collide(self, test_db_ready):
        """Their operations are dropped, so both would be the community's draft."""
        async with _scratch() as conn:
            await _july_dossier(conn, dossier_type=2, status=1, operation=7)
            await _july_dossier(conn, dossier_type=2, status=1, operation=8)
            await self._refused(conn, "two open drafts of one family")

    async def test_a_dossier_outside_wallonia(self, test_db_ready):
        async with _scratch() as conn:
            dossier_id = await _july_dossier(conn, dossier_type=4, status=2, region=2)
            await self._refused(conn, f"outside Wallonia .*: dossier {dossier_id} ")

    async def test_a_deadline_type_with_no_mapping(self, test_db_ready):
        async with _scratch() as conn:
            dossier_id = await _july_dossier(conn, dossier_type=4, status=2)
            await conn.execute(
                "INSERT INTO deadline (id_community, id_dossier, deadline_type, due_date) "
                "VALUES (1, $1, 'custom_override_clock', DATE '2026-12-01')",
                dossier_id,
            )
            await self._refused(conn, "custom_override_clock")


# ---------------------------------------------------------------------------
# Then the real CWaPE catalogue syncs onto it, as the first deploy will
# ---------------------------------------------------------------------------


@dataclass
class Synced:
    dsn: str
    world: JulyWorld
    report: SyncReport


@pytest.fixture(scope="module")
def synced(test_db_ready) -> Iterator[Synced]:
    """A second migrated July world (`migrated` must stay as 0003 left it),
    then engine/catalogue_sync.py with the shipped CWaPE plugin."""
    name = f"m0003_{uuid.uuid4().hex[:12]}"

    async def _build() -> tuple[JulyWorld, SyncReport]:
        world = await _migrate_a_july_world(name)
        engine = create_async_engine(
            f"postgresql+asyncpg://postgres:postgres@localhost:5433/{name}", poolclass=NullPool
        )
        try:
            async with async_sessionmaker(engine)() as session, session.begin():
                report = await sync_plugin(session, CwapePlugin())
        finally:
            await engine.dispose()
        return world, report

    try:
        world, report = asyncio.run(_build())
        yield Synced(dsn=f"{SERVER}/{name}", world=world, report=report)
    finally:
        asyncio.run(_drop(name))


class TestTheCatalogueSyncsOntoIt:
    async def test_every_reserved_row_is_filled_and_none_refused(self, synced):
        assert sorted(synced.report.filled) == sorted(f"{code} v1" for code, _ in TYPES.values())
        # Not a July type: the engine's baseline procedure, new with the engine.
        assert synced.report.inserted == ["ETAT_DE_DEPART v1"]
        async with _read(synced) as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM procedure_definition WHERE content_sha256 IS NULL"
                )
                == 0
            )

    async def test_every_converted_dossier_sits_on_a_step_of_its_definition(self, synced):
        catalogue = CwapePlugin().catalogue()
        async with _read(synced) as conn:
            rows = await conn.fetch(
                "SELECT d.id, d.status, d.step, d.variant, pd.code, pd.version FROM dossier d "
                "JOIN procedure_definition pd ON pd.id = d.id_procedure_definition"
            )
        assert len(rows) == len(TYPES) * len(DOSSIER_PATHS)
        for row in rows:
            entry = catalogue.get(row["code"], row["version"])
            assert entry is not None, row
            definition = entry.definition
            step = definition.step(row["step"])
            assert step.common_status == row["status"], dict(row)
            assert row["variant"] in (definition.variants or (None,)), dict(row)
            assert step.variants is None or row["variant"] in step.variants, dict(row)

    @pytest.mark.parametrize(
        ("name", "procedure", "rule"),
        [
            ("completeness_check", "PARTAGE_MODIF", "completeness"),
            ("completeness_check_ce", "CE_NOTIFICATION", "completeness"),
            ("lapse", "CE_NOTIFICATION", "lapse"),
            ("modification_notification", "CE_ACTUALISATION", "submission"),
        ],
    )
    async def test_the_converted_deadlines_find_their_rule(self, synced, name, procedure, rule):
        async with _read(synced) as conn:
            row = await conn.fetchrow(
                "SELECT pd.code AS procedure, r.code, r.party, r.deadline_kind, "
                "       dl.party AS deadline_party, dl.kind AS deadline_kind_now "
                "FROM deadline dl "
                "JOIN deadline_rule r ON r.id = dl.id_deadline_rule "
                "JOIN procedure_definition pd ON pd.id = r.id_procedure_definition "
                "WHERE dl.id = $1",
                synced.world.deadlines[name],
            )
        assert row is not None, f"{name} was not linked to a rule"
        assert (row["procedure"], row["code"]) == (procedure, rule)
        # Who and what 0003 gave the deadline agree with the rule it now follows.
        assert (row["deadline_party"], row["deadline_kind_now"]) == (
            row["party"],
            row["deadline_kind"],
        )

    async def test_the_july_annual_report_clock_waits_for_the_daily_job(self, synced):
        """CE_NOTIFICATION has no 'submission' rule: the yearly filing is
        CE_RAPPORT_ANNUEL's now, and the daily job closes this leftover when it
        opens the year's report."""
        async with _read(synced) as conn:
            assert (
                await conn.fetchval(
                    "SELECT id_deadline_rule FROM deadline WHERE id = $1",
                    synced.world.deadlines["annual_report"],
                )
                is None
            )
        assert synced.report.deadlines_relinked == 4

    async def test_the_holidays_are_projected(self, synced):
        async with _read(synced) as conn:
            assert (
                await conn.fetchval("SELECT count(*) FROM working_calendar WHERE region = 'WAL'")
                == 100
            )
