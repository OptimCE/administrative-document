"""The summary email (``worker/digest.py``), through the real daily job.

Criterion 4.1: a manager receives at most one email per day, even with ten
deadlines close. Frozen dates; one Postgres serves as both sessions.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from engine.lifecycle import context_for, open_procedure
from shared.const import DeadlineKind, DeadlineParty, DeadlineStatus, OpenedBy
from shared.models.crm_notification_models import OutboundMessageModel
from shared.models.local_models import DeadlineModel, DossierModel
from tests.engine.world import World, make_world
from tests.notifications import notifications_of, seed_roster
from worker.digest import WEEKLY, wants_summary
from worker.engine_jobs import run_daily_jobs

pytestmark = pytest.mark.asyncio

UTC = datetime.UTC
D = datetime.date
WEDNESDAY = datetime.datetime(2026, 4, 15, 4, 0, tzinfo=UTC)  # 06:00 in Brussels
MONDAY = datetime.datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
DIGEST = "admin_digest.summary"


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
) -> DeadlineModel:
    row = DeadlineModel(
        id_community=world.id_community,
        id_dossier=dossier.id,
        deadline_type=type_,
        due_date=due,
        status=int(DeadlineStatus.OPEN),
        party=int(party),
        kind=int(
            DeadlineKind.SUBMISSION if party == DeadlineParty.COMMUNITY else DeadlineKind.RESPONSE
        ),
    )
    world.session.add(row)
    await world.session.flush()
    return row


async def _also_manager(world: World, user_id: int) -> None:
    await world.session.execute(
        text("INSERT INTO community_user (id_community, id_user, role) VALUES (:c, :u, 'MANAGER')"),
        {"c": world.id_community, "u": user_id},
    )


async def _prefer(world: World, user_id: int, mode: int) -> None:
    await world.session.execute(
        text(
            "INSERT INTO notification_preference (id_user, type_prefix, channel, mode) "
            "VALUES (:u, 'admin_digest', 2, :m)"
        ),
        {"u": user_id, "m": mode},
    )


async def _summaries(world: World) -> list[OutboundMessageModel]:
    rows = await world.session.scalars(
        select(OutboundMessageModel)
        .where(OutboundMessageModel.type == DIGEST)
        .order_by(OutboundMessageModel.id)
    )
    return list(rows)


async def _recipients(world: World) -> list[str]:
    return sorted(m.recipient for m in await _summaries(world))


class TestCriterion41:
    async def test_ten_close_deadlines_make_one_email_per_manager(self, db_session):
        world = await make_world(db_session)
        other = await make_world(db_session, plugin=world.plugin)
        ids = await seed_roster(
            world.session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER", "member": "MEMBER"},
        )
        await _also_manager(other, ids["bram"])  # Bram manages both communities
        dossier = await _procedure(world)
        for n in range(10):  # ten deadlines close: 1 to 10 days out
            await _deadline(world, dossier, D(2026, 4, 16 + n), type_=f"step_{n}")
        await _deadline(other, await _procedure(other), D(2026, 4, 20))

        report = await _run(world, WEDNESDAY)

        summaries = await _summaries(world)
        assert sorted(m.recipient for m in summaries) == ["anna@example.be", "bram@example.be"]
        assert report.digests == 2
        bram = next(m for m in summaries if m.recipient == "bram@example.be")
        assert [c["id"] for c in bram.data["communities"]] == sorted(
            [world.id_community, other.id_community]
        )
        anna = next(m for m in summaries if m.recipient == "anna@example.be")
        assert [c["id"] for c in anna.data["communities"]] == [world.id_community]
        assert anna.data["counts"] == {"news": 10, "overdue": 0, "upcoming": 10}
        assert anna.dedupe_key == f"{DIGEST}:u{ids['anna']}:2026-04-15"
        # In-app is still one reminder per deadline and manager: ten for each
        # of the two, and Bram's one in the other community. Only the email is
        # grouped.
        assert len(await notifications_of(world.session, "admin_deadline.due_soon")) == 10 * 2 + 1

    async def test_rerunning_the_day_sends_no_second_email(self, db_session):
        world = await make_world(db_session)
        await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"})
        await _deadline(world, await _procedure(world), D(2026, 4, 20))

        await _run(world, WEDNESDAY)
        await _run(world, WEDNESDAY + datetime.timedelta(hours=3))

        assert await _recipients(world) == ["anna@example.be"]

    async def test_a_quiet_day_sends_nothing(self, db_session):
        """Daily: an email only on a day that brought news."""
        world = await make_world(db_session)
        await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"})
        await _deadline(world, await _procedure(world), D(2026, 4, 25))  # J-10 on the 15th

        await _run(world, WEDNESDAY)  # J-14 rung told on its first pass
        await _run(world, WEDNESDAY + datetime.timedelta(days=1))  # J-9: no rung

        assert [m.data["as_of"] for m in await _summaries(world)] == ["2026-04-15"]

    async def test_no_email_without_a_manager(self, db_session):
        world = await make_world(db_session)
        await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"m": "MEMBER"})
        await _deadline(world, await _procedure(world), D(2026, 4, 20))

        await _run(world, WEDNESDAY)

        assert await _summaries(world) == []


class TestPreferences:
    async def test_weekly_waits_for_monday_and_then_lists_what_is_open(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _prefer(world, ids["anna"], WEEKLY)
        dossier = await _procedure(world)
        await _deadline(world, dossier, D(2026, 4, 28))
        await _deadline(world, dossier, D(2026, 4, 10), type_="late", party=DeadlineParty.GRD)

        await _run(world, WEDNESDAY)
        assert await _summaries(world) == []

        await _run(world, MONDAY)

        (summary,) = await _summaries(world)
        assert summary.data["period"] == "weekly"
        (section,) = summary.data["communities"]
        assert [i["due_date"] for i in section["upcoming"]] == ["2026-04-28"]
        assert [i["party"] for i in section["overdue"]] == ["grd"]

    async def test_none_sends_no_email_but_keeps_the_in_app_reminders(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _prefer(world, ids["anna"], 3)
        await _deadline(world, await _procedure(world), D(2026, 4, 20))

        await _run(world, WEDNESDAY)

        assert await _summaries(world) == []
        assert len(await notifications_of(world.session, "admin_deadline.due_soon")) == 1


class TestTheContent:
    async def test_items_carry_four_languages_a_date_and_a_link(self, db_session):
        world = await make_world(db_session)
        await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"})
        dossier = await _procedure(world)
        await _deadline(world, dossier, D(2026, 4, 20))

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (section,) = summary.data["communities"]
        (news,) = section["news"]
        assert (news["news"], news["rung"], news["days_left"]) == ("reminder", 7, 5)
        assert news["due_display"] == "20/04/2026"
        assert news["path"] == f"/administrative-document/procedures/{dossier.id}"
        assert set(news["procedure"]) == {"fr", "en", "nl", "de"}
        assert all(news["procedure"].values())
        assert summary.data["as_of_display"] == "15/04/2026"


@pytest.mark.parametrize(
    ("mode", "day", "has_news", "expected"),
    [
        (None, D(2026, 4, 15), True, True),
        (None, D(2026, 4, 15), False, False),
        (1, D(2026, 4, 15), True, True),
        (3, D(2026, 4, 15), True, False),
        (WEEKLY, D(2026, 4, 15), True, False),
        (WEEKLY, D(2026, 4, 20), False, True),
    ],
)
def test_who_wants_a_summary_today(mode, day, has_news, expected):
    assert wants_summary(mode, today=day, has_news=has_news) is expected
