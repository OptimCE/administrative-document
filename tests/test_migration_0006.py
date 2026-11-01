"""Migration 0006 on the database 0005 leaves behind.

Two paths reach it, as for 0004 and 0005: an upgraded database (the released
schema, a July world, 0003 to 0006) and a fresh install (schema.sql, which
already has all of 0006, then 0003 to 0006). Both must end with what
schema.sql declares for what 0006 adds; a deadline reminded before the ladder
must come out told the rungs its reminder day had reached.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from tests.test_migration_0003 import CURRENT_SQL, _build_july_world, _counts, _scratch
from tests.test_migration_0003 import MIGRATION_SQL as M0003
from tests.test_migration_0004 import M0004
from tests.test_migration_0005 import M0005

pytestmark = pytest.mark.asyncio

_ROOT = Path(__file__).resolve().parents[1]
M0006 = (_ROOT / "scripts" / "sql" / "migrations" / "0006_phase4.sql").read_text(encoding="utf-8")

COLUMNS = {
    "deadline": ("reminders_sent",),
}


async def _shape(conn: asyncpg.Connection) -> dict[tuple[str, str], tuple[str, str, str | None]]:
    rows = await conn.fetch(
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public'"
    )
    found = {
        (row["table_name"], row["column_name"]): (
            row["data_type"],
            row["is_nullable"],
            row["column_default"],
        )
        for row in rows
    }
    return {
        (table, column): found.get((table, column), ("absent", "", None))
        for table, names in COLUMNS.items()
        for column in names
    }


async def _declared() -> dict[tuple[str, str], tuple[str, str, str | None]]:
    async with _scratch(CURRENT_SQL) as conn:
        return await _shape(conn)


async def _upgraded(conn: asyncpg.Connection) -> None:
    await _build_july_world(conn)
    await conn.execute(M0003)
    await conn.execute(M0004)
    await conn.execute(M0005)


class TestTheShape:
    async def test_an_upgraded_database_ends_as_schema_sql_declares(self, test_db_ready):
        declared = await _declared()
        assert all(value[0] != "absent" for value in declared.values())
        async with _scratch() as conn:
            await _upgraded(conn)
            before = await _counts(conn)
            await conn.execute(M0006)

            assert await _shape(conn) == declared
            assert await _counts(conn) == before
            assert await conn.fetchval("SELECT max(version) FROM schema_version") == 6

    async def test_a_fresh_install_is_left_as_it_is(self, test_db_ready):
        declared = await _declared()
        async with _scratch(CURRENT_SQL) as conn:
            for migration in (M0003, M0004, M0005, M0006):
                await conn.execute(migration)

            assert await _shape(conn) == declared
            assert await conn.fetchval("SELECT max(version) FROM schema_version") == 6

    async def test_applying_it_twice_changes_nothing(self, test_db_ready):
        async with _scratch() as conn:
            await _upgraded(conn)
            await conn.execute(M0006)
            once = (await _shape(conn), await _counts(conn))
            await conn.execute(M0006)

            assert (await _shape(conn), await _counts(conn)) == once


class TestTheLadder:
    async def test_a_deadline_reminded_before_the_ladder_keeps_its_rungs(self, test_db_ready):
        async with _scratch() as conn:
            await _upgraded(conn)
            deadline = await conn.fetchval(
                "SELECT id FROM deadline WHERE status = 1 AND party = 1 ORDER BY id LIMIT 1"
            )
            assert deadline is not None
            # Reminded 5 days before it is due: past the J-14 and J-7 rungs.
            await conn.execute(
                "UPDATE deadline SET reminded_at = "
                " (due_date - 5)::timestamp AT TIME ZONE 'Europe/Brussels' + interval '6 hours' "
                "WHERE id = $1",
                deadline,
            )
            await conn.execute(M0006)

            assert await conn.fetchval(
                "SELECT reminders_sent FROM deadline WHERE id = $1", deadline
            ) == [14, 7]
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM deadline WHERE reminded_at IS NULL "
                    "AND reminders_sent <> '{}'"
                )
                == 0
            )
