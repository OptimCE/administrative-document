"""Phase 4: a procedure that ends its operation (PARTAGE_FIN v2), and what a
refused addition leaves behind. On the fake regulator."""

from __future__ import annotations

import datetime
from pathlib import Path

from sqlalchemy import text

from api.admin.service import AdminService
from engine import effects
from engine.filing import context_of_procedure
from engine.lifecycle import move_to_step
from ports.core_effects_sqlalchemy import SqlCoreEffects
from ports.crm_engine import SqlEngineCrmRead
from regulators.testing.fake import FakeRegulator
from shared.const import ChangeStatus, EventOutcome, ProcedureStatus, RegulatoryStatus
from shared.models.local_models import DossierModel
from tests.engine.catalogue_files import fake_catalogue_copy, fake_document
from tests.engine.world import (
    EAN,
    NOW,
    World,
    add_key,
    add_meter,
    changes,
    dossier,
    event,
    make_world,
    meter,
    procedures,
    route,
)

DAY = NOW.date()
EFFECT = DAY + datetime.timedelta(days=30)
LAST_DAY = EFFECT - datetime.timedelta(days=1)


def _ending_plugin(directory: Path) -> FakeRegulator:
    """The fake plus FAKE_END: an operation's end, notified, then in force."""
    document = fake_document("FAKE_COMMUNITY.v1.yaml")
    document.update(
        code="FAKE_END",
        family="FAKE_END",
        scope="operation",
        recipient="grd_ref",
        handles_change_kinds=["operation_closed"],
        ends_operation=True,
    )
    return FakeRegulator(
        catalogue_dir=fake_catalogue_copy(directory, {"FAKE_END.v1.yaml": document})
    )


async def _step(world: World, procedure: DossierModel, to_step: str) -> None:
    ctx = await context_of_procedure(world.session, world.plugin, procedure)
    await move_to_step(world.session, ctx, procedure, to_step, day=DAY)


async def _sync(world: World, *ids: int) -> None:
    writes = await effects.plan(
        world.session, SqlEngineCrmRead(world.session), world.registry, world.id_community, ids
    )
    await SqlCoreEffects(world.session).apply(world.id_community, writes, audit_payload={})


async def _rows(world: World) -> dict[str, tuple]:
    rows = await world.session.execute(
        text(
            "SELECT subject_ref, regulatory_status, valid_to, regulatory_ref "
            "FROM regulatory_link_status WHERE id_community = :c ORDER BY id"
        ),
        {"c": world.id_community},
    )
    return {r.subject_ref: (r.regulatory_status, r.valid_to, r.regulatory_ref) for r in rows}


async def _running_operation(db_session, tmp_path) -> tuple[World, int]:
    """Two meters and a key confirmed active, and a third meter's addition
    still in a draft."""
    world = await make_world(db_session, plugin=_ending_plugin(tmp_path))
    await add_meter(world, EAN[0])
    await add_meter(world, EAN[1])
    key = await add_key(world)
    service = AdminService(
        local_session=world.session,
        crm_session=world.session,
        crm_read=SqlEngineCrmRead(world.session),
        effects=SqlCoreEffects(world.session),
        registry=world.registry,
        region_of=lambda _code: 1,
    )
    await service.confirm_baseline(world.id_community, [], actor="user-1")
    added = await route(world, meter(world, "operation.meter_added", EAN[2]))
    await _sync(world, *added.dossiers)
    return world, key


async def _ended(world: World) -> DossierModel:
    closed = await route(world, event(world, "operation.closed", payload={}))
    end = await dossier(world, closed.dossiers[0])
    await _step(world, end, "ready")
    await _step(world, end, "sent")
    blocked = await effects.go_in_force(
        world.session,
        world.session,
        SqlEngineCrmRead(world.session),
        SqlCoreEffects(world.session),
        world.registry,
        end.id,
        effective_date=EFFECT,
        now=NOW,
        to_step="in_force",
    )
    assert blocked == []
    return await dossier(world, end.id)


class TestTheEndOfAnOperation:
    async def test_in_force_every_meter_and_the_key_leave_the_day_before(
        self, db_session, tmp_path
    ):
        world, key = await _running_operation(db_session, tmp_path)

        end = await _ended(world)

        assert end.status == ProcedureStatus.IN_FORCE
        rows = await _rows(world)
        ended = (int(RegulatoryStatus.ENDED), LAST_DAY, str(end.id))
        assert rows[EAN[0]] == ended
        assert rows[EAN[1]] == ended
        assert rows[str(key)] == ended
        removals = [c for c in await changes(world, end.id) if c.kind == "meter_removed"]
        assert sorted(c.subject_ref for c in removals) == [EAN[0], EAN[1]]
        assert {c.status for c in removals} == {ChangeStatus.APPLIED}
        assert {c.payload_json["reason"] for c in removals} == {"operation_ended"}

    async def test_what_was_still_to_notify_is_withdrawn_with_its_draft(self, db_session, tmp_path):
        world, _key = await _running_operation(db_session, tmp_path)
        (draft,) = [d for d in await procedures(world) if d.family == "FAKE_CHANGE"]

        await _ended(world)
        await _sync(world, draft.id)

        assert (await dossier(world, draft.id)).status == ProcedureStatus.CANCELLED
        assert (await _rows(world))[EAN[2]][0] == int(RegulatoryStatus.WITHDRAWN)

    async def test_the_manager_then_removing_the_meters_files_nothing(self, db_session, tmp_path):
        world, _key = await _running_operation(db_session, tmp_path)
        await _ended(world)

        removed = await route(world, meter(world, "operation.meter_removed", EAN[0]))

        assert removed.outcome == EventOutcome.ALREADY_REFLECTED
        assert [
            d.family for d in await procedures(world) if d.status == ProcedureStatus.DRAFT
        ] == []


class TestARefusedAddition:
    async def test_its_removal_still_in_a_draft_is_excluded(self, db_session):
        world = await make_world(db_session)
        added = await route(world, meter(world, "operation.meter_added", EAN[0]))
        request = await dossier(world, added.dossiers[0])
        assert request.variant == "full"
        await _step(world, request, "ready")
        await _step(world, request, "sent")
        removed = await route(world, meter(world, "operation.meter_removed", EAN[0]))
        removal_draft = await dossier(world, removed.dossiers[0])
        assert removal_draft.id != request.id

        await _step(world, request, "reviewed")
        await _step(world, request, "refused")

        (removal,) = await changes(world, removal_draft.id)
        assert removal.status == ChangeStatus.EXCLUDED
        assert removal.payload_json["excluded"]["reason"] == "addition_refused"
        # The system draft held nothing else: it is cancelled.
        assert (await dossier(world, removal_draft.id)).status == ProcedureStatus.CANCELLED

    async def test_a_removal_already_sent_stays_the_grd_s(self, db_session):
        world = await make_world(db_session)
        added = await route(world, meter(world, "operation.meter_added", EAN[0]))
        request = await dossier(world, added.dossiers[0])
        await _step(world, request, "ready")
        await _step(world, request, "sent")
        removed = await route(world, meter(world, "operation.meter_removed", EAN[0]))
        removal_procedure = await dossier(world, removed.dossiers[0])
        await _step(world, removal_procedure, "ready")
        await _step(world, removal_procedure, "sent")

        await _step(world, request, "reviewed")
        await _step(world, request, "refused")

        (removal,) = await changes(world, removal_procedure.id)
        assert removal.status == ChangeStatus.ATTACHED
