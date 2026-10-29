"""engine.lifecycle: journaled steps, the deadlines they start and meet, pieces."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text

from engine.catalogue import response_event
from engine.lifecycle import (
    LifecycleError,
    close_rules,
    context_for,
    move_to_step,
    open_procedure,
    settle_readiness,
    start_rules,
)
from regulators.testing.fake import FakeRegulator
from shared.const import DeadlineStatus, OpenedBy, ProcedureStatus, SubjectType
from shared.models.local_models import DeadlineModel, DossierModel
from tests.engine.world import journal, make_world

D = datetime.date
SENT_ON = D(2026, 2, 26)  # Thursday; the fake's calendar makes Monday 2 March a holiday


async def _procedure(world, code: str = "FAKE_CHANGE", **kw) -> tuple[object, DossierModel]:
    ctx = await context_for(world.session, world.plugin, code, 1)
    dossier = await open_procedure(
        world.session,
        ctx,
        id_community=world.id_community,
        id_sharing_operation=world.operation if code == "FAKE_CHANGE" else None,
        region=1,
        variant=kw.pop("variant", "light" if code == "FAKE_CHANGE" else None),
        opened_by=OpenedBy.SYSTEM,
        day=kw.pop("day", SENT_ON),
        **kw,
    )
    return ctx, dossier


async def _deadlines(world, dossier: DossierModel) -> dict[str, DeadlineModel]:
    rows = await world.session.scalars(
        select(DeadlineModel).where(DeadlineModel.id_dossier == dossier.id)
    )
    return {row.deadline_type: row for row in rows}


class TestSteps:
    async def test_each_move_is_journaled_with_its_step(self, db_session):
        world = await make_world(db_session)
        ctx, dossier = await _procedure(world)
        await move_to_step(db_session, ctx, dossier, "ready", day=SENT_ON, actor="user-1")

        assert (dossier.status, dossier.step) == (ProcedureStatus.READY, "ready")
        last = (await journal(world, SubjectType.DOSSIER, dossier.id))[-1]
        assert (last.from_status, last.from_step, last.to_status, last.to_step, last.actor_id) == (
            1,
            "draft",
            6,
            "ready",
            "user-1",
        )

    async def test_an_edge_the_catalogue_lacks_is_refused(self, db_session):
        world = await make_world(db_session)
        ctx, dossier = await _procedure(world)
        with pytest.raises(LifecycleError, match="no edge draft -> sent"):
            await move_to_step(db_session, ctx, dossier, "sent", day=SENT_ON)

    async def test_an_edge_of_the_other_route_is_refused(self, db_session):
        world = await make_world(db_session)
        ctx, dossier = await _procedure(world, variant="light")
        await move_to_step(db_session, ctx, dossier, "ready", day=SENT_ON)
        await move_to_step(db_session, ctx, dossier, "sent", day=SENT_ON)
        with pytest.raises(LifecycleError, match="no edge sent -> reviewed"):
            await move_to_step(db_session, ctx, dossier, "reviewed", day=SENT_ON)

    async def test_an_unknown_variant_is_refused(self, db_session):
        world = await make_world(db_session)
        with pytest.raises(LifecycleError, match="no variant 'medium'"):
            await _procedure(world, variant="medium")

    async def test_the_catalogue_must_be_synced(self, db_session):
        unsynced = FakeRegulator(code="TEST-UNSYNCED")
        with pytest.raises(LifecycleError, match="the catalogue sync has not run"):
            await context_for(db_session, unsynced, "FAKE_CHANGE", 1)


class TestDeadlines:
    async def test_entering_a_step_starts_its_clocks_on_the_plugins_calendar(self, db_session):
        world = await make_world(db_session)
        ctx, dossier = await _procedure(world)
        await move_to_step(db_session, ctx, dossier, "ready", day=SENT_ON)
        await move_to_step(db_session, ctx, dossier, "sent", day=SENT_ON)

        answer = (await _deadlines(world, dossier))["grd_answer"]
        # 10 working days from Thursday 26 February, over the fake's 2 March.
        assert answer.due_date == D(2026, 3, 13)
        assert (answer.party, answer.kind, answer.status) == (2, 2, DeadlineStatus.OPEN)
        assert answer.id_deadline_rule == ctx.rule_ids["grd_answer"]
        assert (dossier.next_action_code, dossier.next_action_due) == (
            "record_response",
            D(2026, 3, 13),
        )

    async def test_a_response_meets_the_clock_it_closes(self, db_session):
        world = await make_world(db_session)
        ctx, dossier = await _procedure(world)
        await move_to_step(db_session, ctx, dossier, "ready", day=SENT_ON)
        await move_to_step(db_session, ctx, dossier, "sent", day=SENT_ON)

        await close_rules(db_session, ctx, dossier, response_event("missing_info_notice"))
        started = await start_rules(
            db_session, ctx, dossier, response_event("missing_info_notice"), anchor=D(2026, 3, 5)
        )

        deadlines = await _deadlines(world, dossier)
        assert deadlines["grd_answer"].status == DeadlineStatus.MET
        assert [d.deadline_type for d in started] == ["supplements"]
        assert deadlines["supplements"].due_date == D(2026, 9, 5)  # six months

    async def test_a_running_clock_is_not_restarted(self, db_session):
        world = await make_world(db_session)
        ctx, dossier = await _procedure(world)
        await move_to_step(db_session, ctx, dossier, "ready", day=SENT_ON)
        await move_to_step(db_session, ctx, dossier, "sent", day=SENT_ON)
        again = await start_rules(db_session, ctx, dossier, "step:sent", anchor=D(2026, 4, 1))
        assert again == []
        assert (await _deadlines(world, dossier))["grd_answer"].due_date == D(2026, 3, 13)

    async def test_a_yearly_procedure_is_due_in_its_period(self, db_session):
        world = await make_world(db_session)
        _ctx, dossier = await _procedure(world, "FAKE_YEARLY", period_key="2026")
        assert (await _deadlines(world, dossier))["submission"].due_date == D(2026, 6, 30)
        assert dossier.next_action_due == D(2026, 6, 30)

    async def test_cancelling_cancels_the_running_clocks(self, db_session):
        world = await make_world(db_session)
        ctx, dossier = await _procedure(world, "FAKE_YEARLY", period_key="2026")
        await move_to_step(db_session, ctx, dossier, "cancelled", day=SENT_ON)
        assert (await _deadlines(world, dossier))["submission"].status == DeadlineStatus.CANCELLED
        assert dossier.next_action_due is None


async def _set_piece(world, piece_id: int, from_status: int, to_status: int) -> None:
    await world.session.execute(
        text(
            "INSERT INTO status_event (id_community, subject_type, subject_id, from_status, "
            " to_status) VALUES (:c, 1, :d, :f, :t)"
        ),
        {"c": world.id_community, "d": piece_id, "f": from_status, "t": to_status},
    )
    world.session.expire_all()


class TestReadiness:
    async def test_follows_the_required_pieces_both_ways(self, db_session):
        from tests.engine.world import EAN, meter, pieces, route

        world = await make_world(db_session)
        result = await route(world, meter(world, "operation.meter_removed", EAN[0]))
        (dossier_id,) = result.dossiers
        ctx = await context_for(db_session, world.plugin, "FAKE_CHANGE", 1)
        dossier = await db_session.get(DossierModel, dossier_id)
        assert dossier is not None
        assert not await settle_readiness(db_session, ctx, dossier, day=SENT_ON)

        form = (await pieces(world, dossier_id))[0]
        assert (form.requirement_code, form.required) == ("change_form", True)
        await _set_piece(world, form.id, 6, 2)
        dossier = await db_session.get(DossierModel, dossier_id)
        assert dossier is not None
        assert await settle_readiness(db_session, ctx, dossier, day=SENT_ON)
        assert dossier.status == ProcedureStatus.READY

        await _set_piece(world, form.id, 2, 7)
        dossier = await db_session.get(DossierModel, dossier_id)
        assert dossier is not None
        assert await settle_readiness(db_session, ctx, dossier, day=SENT_ON)
        assert dossier.status == ProcedureStatus.DRAFT
