"""The daily jobs' decisions (``engine/jobs.py``), with the fake regulator.

Frozen dates throughout: the jobs take ``today`` and nothing reads the clock
for a decision. FAKE_YEARLY opens on 1 April and is due on 30 June.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import func, select

from engine.jobs import chase_deadlines, due_effects, open_calendar_procedures
from engine.lifecycle import UNRESOLVED, context_for, open_procedure, refresh_next_action
from shared.const import (
    DeadlineKind,
    DeadlineParty,
    DeadlineStatus,
    OpenedBy,
    ProcedureStatus,
)
from shared.models.local_models import DeadlineModel, DossierModel
from tests.engine.world import World, make_world

pytestmark = pytest.mark.asyncio

D = datetime.date


def _region(_code: str) -> int:
    return 1


async def _yearly(world: World, today: datetime.date) -> list[int]:
    return await open_calendar_procedures(
        world.session, world.plugin, world.id_community, today=today, region_of=_region
    )


async def _procedure(
    world: World,
    code: str = "FAKE_COMMUNITY",
    *,
    step: str | None = None,
    opened_by: OpenedBy = OpenedBy.SYSTEM,
) -> DossierModel:
    ctx = await context_for(world.session, world.plugin, code, 1)
    return await open_procedure(
        world.session,
        ctx,
        id_community=world.id_community,
        id_sharing_operation=None,
        region=1,
        variant=None,
        opened_by=opened_by,
        day=D(2026, 4, 1),
        step=step,
    )


async def _deadline(
    world: World,
    dossier: DossierModel,
    due: datetime.date,
    *,
    type_: str = "submission",
    party: DeadlineParty = DeadlineParty.COMMUNITY,
    kind: DeadlineKind = DeadlineKind.SUBMISSION,
    status: DeadlineStatus = DeadlineStatus.OPEN,
) -> DeadlineModel:
    row = DeadlineModel(
        id_community=world.id_community,
        id_dossier=dossier.id,
        deadline_type=type_,
        due_date=due,
        status=int(status),
        party=int(party),
        kind=int(kind),
    )
    world.session.add(row)
    await world.session.flush()
    return row


async def _fresh(world: World, row: DeadlineModel) -> DeadlineModel:
    await world.session.refresh(row)
    return row


async def _yearly_procedures(world: World) -> list[DossierModel]:
    return list(
        await world.session.scalars(
            select(DossierModel)
            .where(
                DossierModel.id_community == world.id_community,
                DossierModel.family == "FAKE_YEARLY",
            )
            .order_by(DossierModel.id)
        )
    )


class TestCalendarProcedures:
    async def test_opens_on_its_opening_day_with_its_annual_deadline(self, db_session):
        world = await make_world(db_session)

        opened = await _yearly(world, D(2026, 4, 1))

        assert len(opened) == 1
        dossier = await world.session.get(DossierModel, opened[0])
        assert dossier is not None
        await world.session.refresh(dossier)
        assert dossier.family == "FAKE_YEARLY"
        assert dossier.period_key == "2026"
        assert dossier.opened_by == int(OpenedBy.SYSTEM)
        assert dossier.status == int(ProcedureStatus.DRAFT)
        assert dossier.id_sharing_operation is None
        deadlines = list(
            await world.session.scalars(
                select(DeadlineModel).where(DeadlineModel.id_dossier == dossier.id)
            )
        )
        assert [(d.deadline_type, d.due_date, d.status) for d in deadlines] == [
            ("submission", D(2026, 6, 30), int(DeadlineStatus.OPEN))
        ]
        assert dossier.next_action_due == D(2026, 6, 30)

    async def test_a_second_run_opens_nothing(self, db_session):
        world = await make_world(db_session)
        await _yearly(world, D(2026, 4, 1))

        assert await _yearly(world, D(2026, 4, 1)) == []
        assert await _yearly(world, D(2026, 4, 2)) == []
        assert len(await _yearly_procedures(world)) == 1

    async def test_once_a_year_whatever_became_of_it(self, db_session):
        """Cancelled by hand, it does not come back that year; next year opens
        its own."""
        world = await make_world(db_session)
        [first] = await _yearly(world, D(2026, 4, 1))
        dossier = await world.session.get(DossierModel, first)
        assert dossier is not None
        from engine.lifecycle import move_to_step

        ctx = await context_for(world.session, world.plugin, "FAKE_YEARLY", 1)
        await move_to_step(world.session, ctx, dossier, "cancelled", day=D(2026, 4, 2))
        await world.session.flush()

        assert await _yearly(world, D(2026, 5, 4)) == []
        assert len(await _yearly(world, D(2027, 4, 1))) == 1
        assert [d.period_key for d in await _yearly_procedures(world)] == ["2026", "2027"]

    @pytest.mark.parametrize("day", [D(2026, 3, 31), D(2026, 7, 1), D(2026, 12, 31)])
    async def test_nothing_opens_outside_its_window(self, db_session, day):
        world = await make_world(db_session)

        assert await _yearly(world, day) == []

    async def test_a_community_activated_mid_window_gets_this_years(self, db_session):
        world = await make_world(db_session)

        assert len(await _yearly(world, D(2026, 6, 30))) == 1

    async def test_each_community_has_its_own(self, db_session):
        first = await make_world(db_session)
        second = await make_world(db_session, plugin=first.plugin)

        assert len(await _yearly(first, D(2026, 4, 1))) == 1
        assert len(await _yearly(second, D(2026, 4, 1))) == 1


class TestTakingOverConvertedDeadlines:
    async def test_the_new_procedure_supersedes_an_orphan_july_deadline(self, db_session):
        """A July annual report deadline hung on a converted dossier whose own
        procedure has no such rule: the calendar procedure takes it over."""
        world = await make_world(db_session)
        converted = await _procedure(world, step="sent", opened_by=OpenedBy.MIGRATION)
        orphan = await _deadline(world, converted, D(2026, 5, 1))
        missed = await _deadline(world, converted, D(2026, 3, 1), status=DeadlineStatus.MISSED)
        other_type = await _deadline(world, converted, D(2026, 9, 1), type_="yearly_other")
        other_party = await _deadline(world, converted, D(2026, 9, 2), party=DeadlineParty.GRD)
        ctx = await context_for(world.session, world.plugin, "FAKE_COMMUNITY", 1)
        await refresh_next_action(world.session, ctx, converted)
        assert converted.next_action_due == D(2026, 3, 1)

        await _yearly(world, D(2026, 4, 1))

        assert (await _fresh(world, orphan)).status == int(DeadlineStatus.CANCELLED)
        assert (await _fresh(world, missed)).status == int(DeadlineStatus.CANCELLED)
        assert (await _fresh(world, other_type)).status == int(DeadlineStatus.OPEN)
        assert (await _fresh(world, other_party)).status == int(DeadlineStatus.OPEN)
        await world.session.refresh(converted)
        soonest = await world.session.scalar(
            select(func.min(DeadlineModel.due_date)).where(
                DeadlineModel.id_dossier == converted.id, DeadlineModel.status.in_(UNRESOLVED)
            )
        )
        assert converted.next_action_due == soonest
        assert soonest not in (D(2026, 3, 1), D(2026, 5, 1))

    async def test_a_procedure_opened_here_keeps_its_deadline(self, db_session):
        world = await make_world(db_session)
        ours = await _procedure(world)
        deadline = await _deadline(world, ours, D(2026, 5, 1))

        await _yearly(world, D(2026, 4, 1))

        assert (await _fresh(world, deadline)).status == int(DeadlineStatus.OPEN)

    async def test_a_deadline_its_own_procedure_has_a_rule_for_is_kept(self, db_session):
        """``confirmation`` is a FAKE_COMMUNITY rule: even unlinked, it is that
        procedure's, not a leftover."""
        world = await make_world(db_session)
        converted = await _procedure(world, step="sent", opened_by=OpenedBy.MIGRATION)
        own = await _deadline(world, converted, D(2026, 5, 1), type_="confirmation")
        # Same code as one of FAKE_YEARLY's annual rules would need; here it is
        # FAKE_COMMUNITY's own, and FAKE_YEARLY has no rule of that name either.
        await _yearly(world, D(2026, 4, 1))

        assert (await _fresh(world, own)).status == int(DeadlineStatus.OPEN)

    async def test_another_communitys_deadline_is_untouched(self, db_session):
        world = await make_world(db_session)
        elsewhere = await make_world(db_session, plugin=world.plugin)
        converted = await _procedure(elsewhere, step="sent", opened_by=OpenedBy.MIGRATION)
        theirs = await _deadline(elsewhere, converted, D(2026, 5, 1))

        await _yearly(world, D(2026, 4, 1))

        assert (await _fresh(elsewhere, theirs)).status == int(DeadlineStatus.OPEN)


TODAY = D(2026, 5, 4)  # a Monday


class TestDeadlineChase:
    async def _chase(self, world: World):
        return await chase_deadlines(
            world.session, world.id_community, today=TODAY, reminder_days=14
        )

    async def test_the_communitys_deadline_past_due_is_missed_once_and_stays_unresolved(
        self, db_session
    ):
        world = await make_world(db_session)
        dossier = await _procedure(world)
        late = await _deadline(world, dossier, D(2026, 4, 30))
        later = await _deadline(world, dossier, D(2026, 6, 30), type_="later")

        chase = await self._chase(world)

        assert [d.id for d in chase.missed] == [late.id]
        assert (await _fresh(world, late)).status == int(DeadlineStatus.MISSED)
        assert late.resolved_at is not None
        assert (await _fresh(world, later)).status == int(DeadlineStatus.OPEN)
        # Still counts for the next action: the dashboard keeps it overdue.
        ctx = await context_for(world.session, world.plugin, "FAKE_COMMUNITY", 1)
        await refresh_next_action(world.session, ctx, dossier)
        assert dossier.next_action_due == D(2026, 4, 30)

        again = await self._chase(world)
        assert (again.missed, again.reminded, again.late_answers) == ([], [], [])

    async def test_one_due_within_the_window_is_reminded_once(self, db_session):
        world = await make_world(db_session)
        dossier = await _procedure(world)
        soon = await _deadline(world, dossier, D(2026, 5, 18))  # today + 14
        today = await _deadline(world, dossier, TODAY, type_="today")
        far = await _deadline(world, dossier, D(2026, 5, 19), type_="far")

        chase = await self._chase(world)

        assert sorted(d.id for d in chase.reminded) == sorted([soon.id, today.id])
        assert chase.missed == []
        assert (await _fresh(world, soon)).reminded_at is not None
        assert (await _fresh(world, soon)).status == int(DeadlineStatus.OPEN)
        assert (await _fresh(world, far)).reminded_at is None
        assert (await self._chase(world)).reminded == []

    async def test_a_third_partys_late_answer_stays_open_and_is_told_once(self, db_session):
        world = await make_world(db_session)
        dossier = await _procedure(world)
        late = await _deadline(
            world, dossier, D(2026, 4, 30), party=DeadlineParty.GRD, kind=DeadlineKind.RESPONSE
        )
        soon = await _deadline(
            world,
            dossier,
            D(2026, 5, 6),
            type_="answer_soon",
            party=DeadlineParty.REGULATOR,
            kind=DeadlineKind.RESPONSE,
        )

        chase = await self._chase(world)

        assert [d.id for d in chase.late_answers] == [late.id]
        assert chase.missed == [] and chase.reminded == []
        late = await _fresh(world, late)
        assert late.status == int(DeadlineStatus.OPEN)
        assert late.reminded_at is not None
        # Not the community's to meet: no reminder before it is late.
        assert (await _fresh(world, soon)).reminded_at is None
        assert (await self._chase(world)).late_answers == []

    async def test_an_effect_date_is_not_chased(self, db_session):
        world = await make_world(db_session)
        dossier = await _procedure(world)
        effect = await _deadline(
            world, dossier, D(2026, 4, 30), type_="effect", kind=DeadlineKind.EFFECT
        )
        soon = await _deadline(
            world, dossier, D(2026, 5, 6), type_="effect_soon", kind=DeadlineKind.EFFECT
        )

        chase = await self._chase(world)

        assert (chase.missed, chase.reminded, chase.late_answers) == ([], [], [])
        for row in (effect, soon):
            row = await _fresh(world, row)
            assert row.status == int(DeadlineStatus.OPEN)
            assert row.reminded_at is None

    async def test_a_finished_procedures_window_closes_silently(self, db_session):
        """The informative appeal window of a refusal: no reminder, no missed
        notice, and once past it leaves the agenda."""
        world = await make_world(db_session)
        dossier = await _procedure(world, step="in_force")
        past = await _deadline(world, dossier, D(2026, 4, 30), type_="appeal")
        soon = await _deadline(world, dossier, D(2026, 5, 6), type_="appeal_soon")

        chase = await self._chase(world)

        assert (chase.missed, chase.reminded, chase.late_answers) == ([], [], [])
        assert chase.expired == 1
        assert (await _fresh(world, past)).status == int(DeadlineStatus.CANCELLED)
        soon = await _fresh(world, soon)
        assert soon.status == int(DeadlineStatus.OPEN)
        assert soon.reminded_at is None

    async def test_another_communitys_deadlines_are_not_chased(self, db_session):
        world = await make_world(db_session)
        elsewhere = await make_world(db_session, plugin=world.plugin)
        theirs = await _deadline(elsewhere, await _procedure(elsewhere), D(2026, 4, 30))

        assert (await self._chase(world)).missed == []
        assert (await _fresh(elsewhere, theirs)).status == int(DeadlineStatus.OPEN)


class TestEffectsDue:
    async def test_lists_the_effect_dates_that_have_come_and_writes_nothing(self, db_session):
        world = await make_world(db_session)
        dossier = await _procedure(world, step="sent")
        due = await _deadline(world, dossier, TODAY, type_="effect", kind=DeadlineKind.EFFECT)
        await _deadline(
            world, dossier, TODAY + datetime.timedelta(days=1), type_="t", kind=DeadlineKind.EFFECT
        )
        await _deadline(
            world,
            dossier,
            D(2026, 4, 1),
            type_="met",
            kind=DeadlineKind.EFFECT,
            status=DeadlineStatus.MET,
        )
        await _deadline(world, dossier, D(2026, 4, 1), type_="not_effect")
        finished = await _procedure(world, step="in_force")
        await _deadline(world, finished, D(2026, 4, 1), type_="e", kind=DeadlineKind.EFFECT)

        assert await due_effects(world.session, world.id_community, today=TODAY) == [
            (dossier.id, due.id)
        ]
        assert (await _fresh(world, due)).status == int(DeadlineStatus.OPEN)
