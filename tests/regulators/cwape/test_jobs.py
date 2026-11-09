"""CWaPE's calendar procedure: the annual report opens on 1 June and takes over
the July annual report deadline a converted notification still carries."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from engine.jobs import open_calendar_procedures
from engine.lifecycle import context_for, open_procedure
from regulators.cwape.plugin import CwapePlugin
from shared.const import DeadlineKind, DeadlineParty, DeadlineStatus, OpenedBy
from shared.models.local_models import DeadlineModel, DossierModel
from tests.engine.world import World, make_world

pytestmark = pytest.mark.asyncio

D = datetime.date


async def _converted(world: World, code: str, step: str) -> DossierModel:
    ctx = await context_for(world.session, world.plugin, code, 1)
    return await open_procedure(
        world.session,
        ctx,
        id_community=world.id_community,
        id_sharing_operation=None,
        region=1,
        variant=None,
        opened_by=OpenedBy.MIGRATION,
        day=D(2026, 1, 5),
        step=step,
    )


async def _submission(world: World, dossier: DossierModel, due: datetime.date) -> DeadlineModel:
    """A July deadline as migration 0003 left it: renamed to the rule code,
    linked to nothing."""
    row = DeadlineModel(
        id_community=world.id_community,
        id_dossier=dossier.id,
        deadline_type="submission",
        due_date=due,
        status=int(DeadlineStatus.OPEN),
        party=int(DeadlineParty.COMMUNITY),
        kind=int(DeadlineKind.SUBMISSION),
    )
    world.session.add(row)
    await world.session.flush()
    return row


async def _open(world: World, today: datetime.date) -> list[int]:
    return await open_calendar_procedures(
        world.session, world.plugin, world.id_community, today=today, region_of=lambda _c: 1
    )


async def test_the_annual_report_opens_on_1_june_and_takes_over_the_july_leftover(db_session):
    world = await make_world(db_session, plugin=CwapePlugin())
    notification = await _converted(world, "CE_NOTIFICATION", "in_force")
    leftover = await _submission(world, notification, D(2026, 9, 1))
    update = await _converted(world, "CE_ACTUALISATION", "sent")
    its_own = await _submission(world, update, D(2026, 6, 19))

    assert await _open(world, D(2026, 5, 31)) == []
    await world.session.refresh(leftover)
    assert leftover.status == int(DeadlineStatus.OPEN)

    [opened] = await _open(world, D(2026, 6, 1))

    report = await world.session.get(DossierModel, opened)
    assert report is not None
    await world.session.refresh(report)
    assert (report.family, report.period_key) == ("CE_RAPPORT_ANNUEL", "2026")
    deadlines = list(
        await world.session.scalars(
            select(DeadlineModel).where(DeadlineModel.id_dossier == report.id)
        )
    )
    assert [(d.deadline_type, d.due_date) for d in deadlines] == [("submission", D(2026, 9, 1))]
    await world.session.refresh(leftover)
    assert leftover.status == int(DeadlineStatus.CANCELLED)
    # CE_ACTUALISATION has a submission rule of its own: not a leftover.
    await world.session.refresh(its_own)
    assert its_own.status == int(DeadlineStatus.OPEN)


async def test_nothing_opens_after_the_annual_due_date(db_session):
    """A community activated in October files that year's report by hand."""
    world = await make_world(db_session, plugin=CwapePlugin())

    assert await _open(world, D(2026, 9, 2)) == []
    assert len(await _open(world, D(2026, 9, 1))) == 1
