"""The daily jobs over every community (``worker/engine_jobs.py``).

Frozen dates; one Postgres serves as both the annexe's and the CRM's session,
as in the engine tests. The fake regulator's FAKE_YEARLY opens on 1 April.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from core.notifications.repository import NotificationRepository
from engine.contracts import build_registry
from engine.lifecycle import context_for, open_procedure
from regulators.testing.fake import FakeRegulator
from shared.const import (
    DeadlineKind,
    DeadlineParty,
    DeadlineStatus,
    OpenedBy,
)
from shared.models.crm_models import NotificationModel
from shared.models.crm_notification_models import OutboundMessageModel
from shared.models.local_models import DeadlineModel, DossierModel, ModuleActivationModel
from tests.engine.world import World, baseline_procedure, make_world, set_subscription
from tests.notifications import notifications_of, seed_roster
from worker import engine_jobs
from worker.engine_jobs import _JOBS_ADVISORY_LOCK_KEY, run_daily_jobs, try_run_daily_jobs

pytestmark = pytest.mark.asyncio

UTC = datetime.UTC
D = datetime.date
APRIL_1 = datetime.datetime(2026, 4, 1, 4, 0, tzinfo=UTC)  # 06:00 in Brussels
APRIL_15 = datetime.datetime(2026, 4, 15, 4, 0, tzinfo=UTC)
NOVEMBER = datetime.datetime(2026, 11, 2, 5, 0, tzinfo=UTC)  # outside FAKE_YEARLY's window


async def _run(world: World, now: datetime.datetime):
    return await run_daily_jobs(
        registry=world.registry,
        local_session=world.session,
        crm_session=world.session,
        now=now,
        region_of=lambda _code: 1,
    )


async def _procedure(world: World) -> DossierModel:
    ctx = await context_for(world.session, world.plugin, "FAKE_COMMUNITY", 1)
    return await open_procedure(
        world.session,
        ctx,
        id_community=world.id_community,
        id_sharing_operation=None,
        region=1,
        variant=None,
        opened_by=OpenedBy.SYSTEM,
        day=D(2026, 3, 2),
    )


async def _deadline(
    world: World,
    dossier: DossierModel,
    due: datetime.date,
    *,
    type_: str = "submission",
    party: DeadlineParty = DeadlineParty.COMMUNITY,
    kind: DeadlineKind = DeadlineKind.SUBMISSION,
) -> DeadlineModel:
    row = DeadlineModel(
        id_community=world.id_community,
        id_dossier=dossier.id,
        deadline_type=type_,
        due_date=due,
        status=int(DeadlineStatus.OPEN),
        party=int(party),
        kind=int(kind),
    )
    world.session.add(row)
    await world.session.flush()
    return row


async def _status(world: World, row: DeadlineModel) -> int:
    await world.session.refresh(row)
    return row.status


async def _dossiers(world: World) -> list[DossierModel]:
    return list(
        await world.session.scalars(
            select(DossierModel).where(DossierModel.id_community == world.id_community)
        )
    )


async def _period(world: World) -> ModuleActivationModel | None:
    row = await world.session.scalar(
        select(ModuleActivationModel)
        .where(ModuleActivationModel.id_community == world.id_community)
        .order_by(ModuleActivationModel.id.desc())
    )
    if row is not None:
        await world.session.refresh(row)
    return row


async def _manager(world: World) -> None:
    await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"m-sub": "MANAGER"})


class TestWhoTheDayIsFor:
    async def test_an_active_community_gets_its_baseline_and_this_years_procedure(self, db_session):
        world = await make_world(db_session)

        report = await _run(world, APRIL_1)

        assert report.opened == 1
        assert report.failed == 0
        assert await baseline_procedure(world) is not None
        yearly = [d for d in await _dossiers(world) if d.family == "FAKE_YEARLY"]
        assert [d.period_key for d in yearly] == ["2026"]

        again = await _run(world, APRIL_1 + datetime.timedelta(days=1))
        assert again.opened == 0
        assert len(await _dossiers(world)) == 2  # the baseline and this year's

    async def test_a_community_switched_off_is_left_alone(self, db_session):
        """Jobs ignore it and nothing is notified (spec, désactivation); its
        period is closed."""
        world = await make_world(db_session)
        await _run(world, NOVEMBER - datetime.timedelta(days=300))  # opens the period
        overdue = await _deadline(world, await _procedure(world), D(2026, 4, 1))
        await _manager(world)
        await set_subscription(world, active=False, at=APRIL_1 + datetime.timedelta(days=5))
        before = [d.id for d in await _dossiers(world)]

        report = await _run(world, APRIL_15)

        assert report.failed == 0
        period = await _period(world)
        assert period is not None and period.disabled_at is not None
        assert await _status(world, overdue) == int(DeadlineStatus.OPEN)
        assert [d.id for d in await _dossiers(world)] == before  # no FAKE_YEARLY
        assert await notifications_of(world.session) == []

    async def test_a_community_never_activated_is_not_visited(self, db_session):
        world = await make_world(db_session, active=False)

        await _run(world, APRIL_1)

        assert await _dossiers(world) == []
        assert await _period(world) is None

    async def test_a_regulator_without_a_plugin_gets_nothing(self, db_session):
        world = await make_world(db_session, regulator="BE-BRU-BRUGEL")

        report = await _run(world, APRIL_1)

        assert report.failed == 0
        assert await _dossiers(world) == []


class TestNotifications:
    async def test_the_day_is_notified_once(self, db_session):
        world = await make_world(db_session)
        await _manager(world)
        dossier = await _procedure(world)
        missed = await _deadline(world, dossier, D(2026, 4, 1))
        soon = await _deadline(world, dossier, D(2026, 4, 20), type_="soon")
        late = await _deadline(
            world,
            dossier,
            D(2026, 4, 1),
            type_="answer",
            party=DeadlineParty.GRD,
            kind=DeadlineKind.RESPONSE,
        )

        report = await _run(world, APRIL_15)

        assert (report.missed, report.reminded, report.late_answers) == (1, 1, 1)
        assert await _status(world, missed) == int(DeadlineStatus.MISSED)
        assert await _status(world, late) == int(DeadlineStatus.OPEN)
        notified = {n.data["deadline_id"]: n for n in await notifications_of(world.session)}
        assert set(notified) == {missed.id, soon.id, late.id}
        assert notified[missed.id].type == "admin_deadline.missed"
        assert notified[missed.id].data["procedure_code"] == "FAKE_COMMUNITY"
        assert notified[missed.id].data["party"] == "community"
        assert notified[missed.id].data["kind"] == int(DeadlineKind.SUBMISSION)
        assert notified[soon.id].type == "admin_deadline.due_soon"
        assert notified[late.id].type == "admin_deadline.missed"
        assert notified[late.id].data["party"] == "grd"
        # In-app per deadline; the email is the day's ONE summary (criterion
        # 4.1), linked to no in-app row.
        emails = list(await world.session.scalars(select(OutboundMessageModel)))
        assert [(m.type, m.id_notification) for m in emails] == [("admin_digest.summary", None)]
        (section,) = emails[0].data["communities"]
        assert sorted(n["news"] for n in section["news"]) == ["late", "missed", "reminder"]

        await _run(world, APRIL_15 + datetime.timedelta(hours=2))
        assert len(await notifications_of(world.session)) == 3

    async def test_a_failing_notification_store_does_not_undo_the_day(
        self, db_session, monkeypatch
    ):
        """``publish`` runs in a SAVEPOINT: the deadline still goes MISSED, and
        the rolled-back savepoint leaves no half-written notification."""
        world = await make_world(db_session)
        await _manager(world)
        missed = await _deadline(world, await _procedure(world), D(2026, 4, 1))

        async def _boom(self: NotificationRepository, rows: list[NotificationModel]) -> None:
            raise RuntimeError("notification store unavailable")

        monkeypatch.setattr(NotificationRepository, "insert_many", _boom)

        report = await _run(world, APRIL_15)

        assert report.failed == 0
        assert await _status(world, missed) == int(DeadlineStatus.MISSED)
        assert await notifications_of(world.session) == []

    async def test_a_community_without_a_due_date_coming_is_not_notified(self, db_session):
        world = await make_world(db_session)
        await _manager(world)
        await _deadline(world, await _procedure(world), D(2026, 6, 1))

        await _run(world, APRIL_15)

        assert await notifications_of(world.session) == []


class TestIsolation:
    async def test_one_communitys_failure_costs_only_its_day(self, db_session, monkeypatch):
        failing = await make_world(db_session)
        healthy = await make_world(db_session, plugin=failing.plugin)
        doomed = await _deadline(failing, await _procedure(failing), D(2026, 4, 1))
        fine = await _deadline(healthy, await _procedure(healthy), D(2026, 4, 1))
        await db_session.commit()
        real = engine_jobs.chase_deadlines

        async def chase(local, id_community, **kwargs):
            if id_community == failing.id_community:
                raise RuntimeError("boom")
            return await real(local, id_community, **kwargs)

        monkeypatch.setattr(engine_jobs, "chase_deadlines", chase)

        report = await run_daily_jobs(
            registry=failing.registry,
            local_session=db_session,
            crm_session=db_session,
            now=APRIL_15,
            region_of=lambda _code: 1,
        )

        assert report.failed == 1
        assert await _status(failing, doomed) == int(DeadlineStatus.OPEN)
        assert await _status(healthy, fine) == int(DeadlineStatus.MISSED)


class TestTheLock:
    async def test_a_held_lock_skips_the_run(self, db_session, test_engine, monkeypatch):
        """Two replicas both running would give every manager two bell entries."""
        calls = []

        async def run(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(engine_jobs, "run_daily_jobs", run)
        await db_session.execute(
            text("SELECT pg_advisory_lock(:k)"), {"k": _JOBS_ADVISORY_LOCK_KEY}
        )
        try:
            ran = await try_run_daily_jobs(FakeRegistry, engine=test_engine)
        finally:
            await db_session.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _JOBS_ADVISORY_LOCK_KEY}
            )

        assert ran is False
        assert calls == []

    async def test_the_lock_is_released_after_a_run(self, db_session, test_engine, monkeypatch):
        calls = []

        async def run(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(engine_jobs, "run_daily_jobs", run)

        assert await try_run_daily_jobs(FakeRegistry, engine=test_engine) is True
        assert len(calls) == 1
        free = await db_session.scalar(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": _JOBS_ADVISORY_LOCK_KEY}
        )
        assert free is True
        await db_session.execute(
            text("SELECT pg_advisory_unlock(:k)"), {"k": _JOBS_ADVISORY_LOCK_KEY}
        )

    async def test_the_key_is_its_own(self):
        """The retired July sweep's, the catalogue sync's, the activation's
        and billing's keys live in the same cluster."""
        assert _JOBS_ADVISORY_LOCK_KEY not in {0x0AD3_0001, 0x0AD3_0002, 0x0AD3_0003, 0x0B111_0001}


FakeRegistry = build_registry([FakeRegulator()])


async def test_the_scheduled_tick_runs_the_engine_jobs(monkeypatch):
    """The scheduler calls the jobs, no longer the July sweep."""
    import asyncio

    from worker import scheduler

    shutdown = asyncio.Event()
    seen = []

    async def jobs(registry):
        seen.append(registry)
        shutdown.set()
        return True

    monkeypatch.setattr(scheduler, "try_run_daily_jobs", jobs)
    monkeypatch.setattr(scheduler, "seconds_until_next_run", lambda _now, _hour: 0)
    registry = build_registry([FakeRegulator()])

    await asyncio.wait_for(scheduler.run_deadline_scheduler(shutdown, registry=registry), 5)

    assert seen == [registry]
