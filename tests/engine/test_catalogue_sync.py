"""engine.catalogue_sync: the plugin's files, projected into the database."""

from __future__ import annotations

import contextlib
import datetime
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.catalogue_sync import (
    _SYNC_ADVISORY_LOCK_KEY,
    CatalogueSyncError,
    sync_plugin,
    sync_registry,
)
from engine.contracts import build_registry
from regulators.testing.fake import FakeRegulator
from shared.models.local_models import (
    DeadlineModel,
    DeadlineRuleModel,
    ProcedureDefinitionModel,
    WorkingCalendarModel,
)
from tests.engine.catalogue_files import fake_catalogue_copy, fake_document

D = datetime.date
# The fake catalogue, in the order the sync handles it (by code).
ALL = ["FAKE_BASELINE v1", "FAKE_CHANGE v1", "FAKE_COMMUNITY v1", "FAKE_YEARLY v1"]
BUT_CHANGE = [name for name in ALL if name != "FAKE_CHANGE v1"]


def _v2() -> dict:
    document = fake_document()
    document.update(version=2, valid_from=D(2026, 7, 1))
    return document


def _with_v2(tmp_path) -> FakeRegulator:
    directory = fake_catalogue_copy(tmp_path / "v2", {"FAKE_CHANGE.v2.yaml": _v2()})
    return FakeRegulator(catalogue_dir=directory)


async def _definition(session: AsyncSession, code: str, version: int = 1):
    return await session.scalar(
        select(ProcedureDefinitionModel).where(
            ProcedureDefinitionModel.regulator == "TEST-FAKE",
            ProcedureDefinitionModel.code == code,
            ProcedureDefinitionModel.version == version,
        )
    )


async def _rules(session: AsyncSession, id_definition: int) -> dict[str, DeadlineRuleModel]:
    rows = await session.scalars(
        select(DeadlineRuleModel).where(DeadlineRuleModel.id_procedure_definition == id_definition)
    )
    return {row.code: row for row in rows if row.code is not None}


@contextlib.asynccontextmanager
async def _rows_written(session: AsyncSession) -> AsyncIterator[list[str]]:
    """Every INSERT, UPDATE or DELETE that changed a row while inside. (The
    deadline relink always runs; matching nothing, it writes nothing.)"""
    connection = (await session.connection()).sync_connection
    assert connection is not None
    statements: list[str] = []

    def record(_conn, cursor, statement, _parameters, _context, _executemany):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"} and cursor.rowcount:
            statements.append(statement)

    event.listen(connection, "after_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(connection, "after_cursor_execute", record)


class TestTheFirstSync:
    async def test_inserts_every_definition(self, db_session):
        plugin = FakeRegulator()
        report = await sync_plugin(db_session, plugin)
        assert report.inserted == ALL

        change = await _definition(db_session, "FAKE_CHANGE")
        entry = plugin.catalogue().get("FAKE_CHANGE", 1)
        assert entry is not None
        assert (change.scope, change.recipient, change.channel) == (2, 1, 1)
        assert change.family == "FAKE_CHANGE"
        assert change.valid_from == D(2026, 1, 1)
        assert change.valid_to is None
        assert change.handles_change_kinds == [
            "meter_added",
            "meter_removed",
            "key_changed",
            "representative_changed",
        ]
        assert change.content_sha256 == entry.content_sha256
        assert change.synced_at is not None
        assert change.transitions_json["initial_step"] == "draft"
        assert len(change.transitions_json["steps"]) == 11
        assert change.escalation_rules_json["default_variant"] == "light"
        assert change.labels_json["nl"] == "Valse wijziging"

    async def test_inserts_the_rules(self, db_session):
        await sync_plugin(db_session, FakeRegulator())
        change = await _definition(db_session, "FAKE_CHANGE")
        rules = await _rules(db_session, change.id)
        assert sorted(rules) == [
            "decision",
            "effect",
            "grd_answer",
            "objection",
            "return_signed",
            "supplements",
        ]
        answer = rules["grd_answer"]
        assert (answer.kind, answer.anchor_event) == (1, "step:sent")
        assert (answer.offset_value, answer.offset_unit) == (10, 1)
        assert (answer.party, answer.deadline_kind) == (2, 2)
        assert answer.description == "Réponse du GRD"
        assert (answer.region, answer.dossier_type, answer.id_community) == (None, None, None)
        assert (rules["supplements"].offset_unit, rules["objection"].offset_unit) == (2, 3)

        yearly = await _definition(db_session, "FAKE_YEARLY")
        submission = (await _rules(db_session, yearly.id))["submission"]
        assert (submission.kind, submission.annual_month, submission.annual_day) == (2, 6, 30)
        assert (submission.anchor_event, submission.offset_value) == (None, None)

    async def test_projects_the_calendar(self, db_session):
        report = await sync_plugin(db_session, FakeRegulator())
        days = await db_session.scalars(
            select(WorkingCalendarModel.day).where(WorkingCalendarModel.region == "TST")
        )
        assert sorted(days) == [
            D(2026, 1, 1),
            D(2026, 3, 2),
            D(2026, 12, 25),
            D(2026, 12, 28),
            D(2027, 1, 1),
        ]
        assert report.holidays_added == 5

    async def test_holds_the_lock_until_the_transaction_ends(self, db_session):
        await sync_plugin(db_session, FakeRegulator())
        held = await db_session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND objid = :key AND pid = pg_backend_pid() AND granted)"
            ),
            {"key": _SYNC_ADVISORY_LOCK_KEY},
        )
        assert held


class TestASecondSync:
    async def test_writes_nothing(self, db_session):
        async with _rows_written(db_session) as first:
            await sync_plugin(db_session, FakeRegulator())
        assert first, "the recorder saw the first sync write nothing, so it proves nothing"
        async with _rows_written(db_session) as writes:
            report = await sync_plugin(db_session, FakeRegulator())
        assert writes == []
        assert report.unchanged == ALL
        assert not report.wrote_anything


class TestAReleasedVersionIsEvidence:
    async def test_new_content_without_a_new_version_is_refused(self, db_session, tmp_path):
        await sync_plugin(db_session, FakeRegulator())
        document = fake_document()
        document["deadline_rules"][0]["offset"]["value"] = 11
        edited = FakeRegulator(
            catalogue_dir=fake_catalogue_copy(tmp_path, {"FAKE_CHANGE.v1.yaml": document})
        )
        with pytest.raises(
            CatalogueSyncError,
            match=r"FAKE_CHANGE v1 \(FAKE_CHANGE\.v1\.yaml\) changed since it was synced.*"
            r"FAKE_CHANGE\.v2\.yaml",
        ):
            await sync_plugin(db_session, edited)

    async def test_a_wording_fix_is_applied_in_place(self, db_session, tmp_path):
        await sync_plugin(db_session, FakeRegulator())
        before = (await _definition(db_session, "FAKE_CHANGE")).content_sha256
        document = fake_document()
        document["labels"]["fr"] = "Changement factice corrigé"
        document["deadline_rules"][0]["labels"]["fr"] = "Réponse du gestionnaire de réseau"
        edited = FakeRegulator(
            catalogue_dir=fake_catalogue_copy(tmp_path, {"FAKE_CHANGE.v1.yaml": document})
        )

        report = await sync_plugin(db_session, edited)

        assert report.refreshed == ["FAKE_CHANGE v1"]
        change = await _definition(db_session, "FAKE_CHANGE")
        assert change.content_sha256 == before
        assert change.labels_json["fr"] == "Changement factice corrigé"
        rule = (await _rules(db_session, change.id))["grd_answer"]
        assert rule.description == "Réponse du gestionnaire de réseau"

    async def test_a_new_version_closes_the_previous_one(self, db_session, tmp_path):
        await sync_plugin(db_session, FakeRegulator())
        report = await sync_plugin(db_session, _with_v2(tmp_path))

        assert report.inserted == ["FAKE_CHANGE v2"]
        assert report.refreshed == ["FAKE_CHANGE v1"]  # its valid_to
        v1 = await _definition(db_session, "FAKE_CHANGE", 1)
        v2 = await _definition(db_session, "FAKE_CHANGE", 2)
        assert (v1.valid_to, v2.valid_from, v2.valid_to) == (D(2026, 6, 30), D(2026, 7, 1), None)
        assert "grd_answer" in await _rules(db_session, v2.id)

    async def test_two_versions_at_once(self, db_session, tmp_path):
        report = await sync_plugin(db_session, _with_v2(tmp_path))
        assert report.inserted == [
            "FAKE_BASELINE v1",
            "FAKE_CHANGE v1",
            "FAKE_CHANGE v2",
            "FAKE_COMMUNITY v1",
            "FAKE_YEARLY v1",
        ]
        assert (await _definition(db_session, "FAKE_CHANGE", 1)).valid_to == D(2026, 6, 30)

    async def test_a_deleted_file_is_refused(self, db_session, tmp_path):
        await sync_plugin(db_session, FakeRegulator())
        without = FakeRegulator(
            catalogue_dir=fake_catalogue_copy(tmp_path, {"FAKE_COMMUNITY.v1.yaml": None})
        )
        with pytest.raises(CatalogueSyncError, match="FAKE_COMMUNITY v1 is in the database but"):
            await sync_plugin(db_session, without)

    async def test_versions_a_newer_image_synced_are_left_alone(self, db_session, tmp_path):
        await sync_plugin(db_session, _with_v2(tmp_path))

        report = await sync_plugin(db_session, FakeRegulator())

        assert report.newer_in_database == ["FAKE_CHANGE v2"]
        assert report.unchanged == BUT_CHANGE
        # This image would make v1 open-ended again; it must not.
        assert (await _definition(db_session, "FAKE_CHANGE", 1)).valid_to == D(2026, 6, 30)


async def _reserve(
    session: AsyncSession, code: str = "FAKE_CHANGE", *, channel: int = 1, family: str | None = None
) -> int:
    """What migration 0003 writes: identity columns, a placeholder valid_from,
    no content."""
    result = await session.execute(
        text(
            "INSERT INTO procedure_definition "
            "(regulator, code, version, valid_from, scope, recipient, channel, family) "
            "VALUES ('TEST-FAKE', :code, 1, DATE '2025-01-01', 2, 1, :channel, :family) "
            "RETURNING id"
        ),
        {"code": code, "channel": channel, "family": family or code},
    )
    return int(result.scalar_one())


async def _converted_dossier(session: AsyncSession, id_definition: int) -> int:
    result = await session.execute(
        text(
            "INSERT INTO dossier (id_community, id_sharing_operation, status, step, region, "
            " id_procedure_definition, regulator, family, scope, opened_by) "
            "VALUES (1, 1, 2, 'sent', 1, :definition, 'TEST-FAKE', 'FAKE_CHANGE', 2, 3) "
            "RETURNING id"
        ),
        {"definition": id_definition},
    )
    dossier_id = int(result.scalar_one())
    await session.execute(
        text(
            "INSERT INTO status_event (id_community, subject_type, subject_id, to_status, to_step) "
            "VALUES (1, 2, :id, 2, 'sent')"
        ),
        {"id": dossier_id},
    )
    return dossier_id


async def _deadline(
    session: AsyncSession, dossier_id: int, code: str, *, rule: int | None = None
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO deadline (id_community, id_dossier, deadline_type, due_date, "
            " party, kind, id_deadline_rule) "
            "VALUES (1, :dossier, :code, DATE '2026-11-02', 2, 2, :rule) RETURNING id"
        ),
        {"dossier": dossier_id, "code": code, "rule": rule},
    )
    return int(result.scalar_one())


class TestReservedRows:
    async def test_a_reserved_row_is_filled(self, db_session):
        reserved = await _reserve(db_session)

        report = await sync_plugin(db_session, FakeRegulator())

        assert report.filled == ["FAKE_CHANGE v1"]
        assert report.inserted == BUT_CHANGE
        change = await _definition(db_session, "FAKE_CHANGE")
        assert change.id == reserved
        assert change.content_sha256 is not None
        assert change.valid_from == D(2026, 1, 1)  # from the file, not the placeholder
        assert "grd_answer" in await _rules(db_session, reserved)

    async def test_a_reservation_that_disagrees_is_refused(self, db_session):
        await _reserve(db_session, channel=2)
        with pytest.raises(
            CatalogueSyncError, match=r"other values than its file \(channel 2 vs 1\)"
        ):
            await sync_plugin(db_session, FakeRegulator())

    async def test_a_reservation_without_a_file_is_refused(self, db_session):
        await _reserve(db_session, "FAKE_GONE")
        with pytest.raises(CatalogueSyncError, match="FAKE_GONE v1 was reserved .* no such file"):
            await sync_plugin(db_session, FakeRegulator())

    async def test_converted_deadlines_are_linked_to_their_rule(self, db_session):
        reserved = await _reserve(db_session)
        dossier = await _converted_dossier(db_session, reserved)
        named = await _deadline(db_session, dossier, "grd_answer")
        unknown = await _deadline(db_session, dossier, "telegram")

        report = await sync_plugin(db_session, FakeRegulator())

        assert report.deadlines_relinked == 1
        rule = (await _rules(db_session, reserved))["grd_answer"]
        linked = await db_session.get(DeadlineModel, named)
        unlinked = await db_session.get(DeadlineModel, unknown)
        await db_session.refresh(linked)
        await db_session.refresh(unlinked)
        assert linked.id_deadline_rule == rule.id
        assert unlinked.id_deadline_rule is None
        assert (await sync_plugin(db_session, FakeRegulator())).deadlines_relinked == 0

    async def test_a_deadline_already_linked_is_left_alone(self, db_session):
        reserved = await _reserve(db_session)
        dossier = await _converted_dossier(db_session, reserved)
        legacy_rule = await db_session.scalar(
            select(DeadlineRuleModel.id).where(DeadlineRuleModel.id_procedure_definition.is_(None))
        )
        assert legacy_rule is not None, "the July seeds should provide a legacy rule"
        kept = await _deadline(db_session, dossier, "grd_answer", rule=legacy_rule)

        report = await sync_plugin(db_session, FakeRegulator())

        assert report.deadlines_relinked == 0
        row = await db_session.get(DeadlineModel, kept)
        await db_session.refresh(row)
        assert row.id_deadline_rule == legacy_rule


class TestTheCalendarProjection:
    async def test_matches_the_file_exactly(self, db_session):
        db_session.add_all(
            [
                WorkingCalendarModel(region="TST", day=D(2026, 6, 1), label="Stale"),
                WorkingCalendarModel(region="TST", day=D(2026, 12, 25), label="Old label"),
                WorkingCalendarModel(region="OTH", day=D(2026, 6, 1), label="Another region"),
            ]
        )
        await db_session.flush()

        report = await sync_plugin(db_session, FakeRegulator())

        assert (report.holidays_added, report.holidays_removed, report.holidays_relabelled) == (
            4,
            1,
            1,
        )
        rows = {
            (row.region, row.day): row.label
            for row in await db_session.scalars(select(WorkingCalendarModel))
        }
        assert rows[("TST", D(2026, 12, 25))] == "Fake winter day"
        assert ("TST", D(2026, 6, 1)) not in rows
        assert rows[("OTH", D(2026, 6, 1))] == "Another region"


class TestSyncRegistry:
    async def test_syncs_every_plugin_and_commits(self, db_session):
        connection = await db_session.connection()
        factory = async_sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        reports = await sync_registry(factory, build_registry([FakeRegulator()]))
        assert [r.regulator for r in reports] == ["TEST-FAKE"]
        assert await _definition(db_session, "FAKE_CHANGE") is not None

    async def test_an_empty_registry_syncs_nothing(self, db_session):
        connection = await db_session.connection()
        factory = async_sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        assert await sync_registry(factory, build_registry([])) == []
