"""The summary email (``worker/digest.py``), through the real daily job.

Criterion 4.1: a manager receives at most one email per day, even with ten
deadlines close. A day's news is the deadline pass's, the procedures the
effects pass put in force, and the answers recorded or received since
yesterday's run (écart 6 of the phase-4 recap). Frozen dates; one Postgres
serves as both sessions.
"""

from __future__ import annotations

import datetime
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from engine.events import BRUSSELS
from engine.lifecycle import context_for, open_procedure
from shared.const import (
    Channel,
    CorrespondenceDirection,
    Counterparty,
    DeadlineKind,
    DeadlineParty,
    DeadlineStatus,
    InboundMailStatus,
    OpenedBy,
)
from shared.models.crm_notification_models import OutboundMessageModel
from shared.models.local_models import (
    CorrespondenceModel,
    DeadlineModel,
    DossierModel,
    InboundAddressModel,
    InboundMailModel,
)
from tests.api.admin.test_effects_routes import EFFECTIVE
from tests.api.admin.test_effects_routes import _day as cwape_day
from tests.api.admin.test_effects_routes import _signed as signed_amendment
from tests.api.admin.test_effects_routes import _world as cwape_world
from tests.engine.world import World, make_world
from tests.notifications import notifications_of, seed_roster
from worker.digest import WEEKLY, answers_since, wants_summary
from worker.engine_jobs import run_daily_jobs

pytestmark = pytest.mark.asyncio

UTC = datetime.UTC
D = datetime.date
WEDNESDAY = datetime.datetime(2026, 4, 15, 4, 0, tzinfo=UTC)  # 06:00 in Brussels
MONDAY = datetime.datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
# Tuesday 15:00 in Brussels: after Tuesday's run, so Wednesday's news.
TUESDAY_AFTERNOON = datetime.datetime(2026, 4, 14, 13, 0, tzinfo=UTC)
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


async def _answer(
    world: World,
    dossier: DossierModel,
    at: datetime.datetime,
    *,
    type_: str = "confirmation",
    direction: CorrespondenceDirection = CorrespondenceDirection.INBOUND,
) -> CorrespondenceModel:
    """A message recorded on the procedure ``at``, as the API records one."""
    row = CorrespondenceModel(
        id_community=world.id_community,
        id_dossier=dossier.id,
        direction=int(direction),
        counterparty=int(Counterparty.REGULATOR),
        channel=int(Channel.EMAIL),
        type=type_,
        occurred_at=at,
        recorded_by="anna",
        context_json={},
        created_at=at,
    )
    world.session.add(row)
    await world.session.flush()
    return row


async def _mail(
    world: World,
    dossier: DossierModel | None,
    at: datetime.datetime,
    status: InboundMailStatus = InboundMailStatus.ATTACHED,
    *,
    correspondence: CorrespondenceModel | None = None,
) -> None:
    """A mail the reception address received ``at``."""
    address = await world.session.scalar(
        select(InboundAddressModel.id).where(InboundAddressModel.id_community == world.id_community)
    )
    if address is None:
        row = InboundAddressModel(id_community=world.id_community, token=f"t{world.id_community}")
        world.session.add(row)
        await world.session.flush()
        address = row.id
    world.session.add(
        InboundMailModel(
            id_community=world.id_community,
            id_inbound_address=address,
            content_sha256=hashlib.sha256(f"{at}{status}".encode()).hexdigest(),
            file_ref="s3://test/message.eml",
            byte_size=1,
            received_at=at,
            status=int(status),
            id_dossier=dossier.id if dossier else None,
            id_correspondence=correspondence.id if correspondence else None,
            handled_at=at if correspondence else None,
        )
    )
    await world.session.flush()


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


class TestTheDaysOtherNews:
    """Écart 6 of the phase-4 recap: a day whose only news is a procedure in
    force, or an answer, sends its summary too, still one per manager."""

    async def test_an_effect_applied_alone_sends_one_summary_per_manager(
        self, client, db_session, fake_storage
    ):
        world = await cwape_world(db_session)
        ids = await seed_roster(
            db_session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER"},
        )
        dossier_id = await signed_amendment(client, db_session, world)

        day = await cwape_day(world, EFFECTIVE)
        await cwape_day(world, EFFECTIVE)  # the day again: no second email

        assert day.effects_applied == 1
        summaries = await _summaries(world)
        assert sorted(m.recipient for m in summaries) == ["anna@example.be", "bram@example.be"]
        anna = next(m for m in summaries if m.recipient == "anna@example.be")
        assert anna.dedupe_key == f"{DIGEST}:u{ids['anna']}:{EFFECTIVE.isoformat()}"
        (section,) = anna.data["communities"]
        (news,) = section["news"]  # the effect, and nothing else that day
        assert news == {
            "dossier_id": dossier_id,
            "path": f"/administrative-document/procedures/{dossier_id}",
            "procedure": news["procedure"],
            "operation": "Op Centre",
            "news": "in_force",
            "effective_date": EFFECTIVE.isoformat(),
            "effective_display": EFFECTIVE.strftime("%d/%m/%Y"),
            "response": None,
        }
        assert set(news["procedure"]) == {"fr", "en", "nl", "de"}
        assert all(news["procedure"].values())

    async def test_an_answer_recorded_alone_sends_one_summary_per_manager(self, db_session):
        world = await make_world(db_session)
        await seed_roster(
            world.session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER", "member": "MEMBER"},
        )
        dossier = await _procedure(world)
        await _answer(world, dossier, TUESDAY_AFTERNOON)

        await _run(world, WEDNESDAY)
        await _run(world, WEDNESDAY + datetime.timedelta(hours=3))  # sees it again

        summaries = await _summaries(world)
        assert sorted(m.recipient for m in summaries) == ["anna@example.be", "bram@example.be"]
        (section,) = summaries[0].data["communities"]
        (news,) = section["news"]
        assert (news["news"], news["dossier_id"], news["effective_date"]) == (
            "answer",
            dossier.id,
            None,
        )
        # The response type's labels, from the procedure's own catalogue version.
        assert news["response"] == {
            "fr": "Confirmation",
            "en": "Confirmation",
            "nl": "Bevestiging",
            "de": "Bestätigung",
        }
        assert news["path"] == f"/administrative-document/procedures/{dossier.id}"
        assert summaries[0].data["counts"]["news"] == 1

    async def test_a_mail_attached_and_not_qualified_is_an_answer_too(self, db_session):
        world = await make_world(db_session)
        await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"})
        dossier = await _procedure(world)
        await _mail(world, dossier, TUESDAY_AFTERNOON)

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (news,) = summary.data["communities"][0]["news"]
        assert (news["news"], news["dossier_id"], news["response"]) == ("answer", dossier.id, None)

    async def test_a_mail_qualified_counts_once_as_its_answer(self, db_session):
        world = await make_world(db_session)
        await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"})
        dossier = await _procedure(world)
        recorded = await _answer(world, dossier, TUESDAY_AFTERNOON)
        await _mail(
            world, dossier, TUESDAY_AFTERNOON, InboundMailStatus.RECORDED, correspondence=recorded
        )

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (news,) = summary.data["communities"][0]["news"]
        assert news["response"] is not None

    async def test_what_is_not_an_answer_of_the_day_sends_nothing(self, db_session):
        world = await make_world(db_session)
        await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"})
        dossier = await _procedure(world)
        before_tuesdays_run = datetime.datetime(2026, 4, 14, 3, 30, tzinfo=UTC)  # 05:30
        await _answer(world, dossier, before_tuesdays_run)  # Tuesday's news
        await _answer(
            world,
            dossier,
            TUESDAY_AFTERNOON,
            type_="sent",
            direction=CorrespondenceDirection.OUTBOUND,
        )
        await _mail(world, None, TUESDAY_AFTERNOON, InboundMailStatus.UNMATCHED)
        await _mail(
            world,
            dossier,
            TUESDAY_AFTERNOON + datetime.timedelta(minutes=1),
            InboundMailStatus.DISCARDED,
        )

        await _run(world, WEDNESDAY)

        assert await _summaries(world) == []

    async def test_answers_and_deadlines_of_two_communities_make_one_email(self, db_session):
        world = await make_world(db_session)
        other = await make_world(db_session, plugin=world.plugin)
        ids = await seed_roster(
            world.session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER"},
        )
        await _also_manager(other, ids["bram"])
        await _deadline(world, await _procedure(world), D(2026, 4, 20))  # J-7 rung
        await _answer(other, await _procedure(other), TUESDAY_AFTERNOON)

        await _run(world, WEDNESDAY)
        await _run(world, WEDNESDAY + datetime.timedelta(hours=3))

        summaries = await _summaries(world)
        assert sorted(m.recipient for m in summaries) == ["anna@example.be", "bram@example.be"]
        bram = next(m for m in summaries if m.recipient == "bram@example.be")
        news = {c["id"]: [n["news"] for n in c["news"]] for c in bram.data["communities"]}
        assert news == {world.id_community: ["reminder"], other.id_community: ["answer"]}
        assert bram.data["counts"]["news"] == 2
        anna = next(m for m in summaries if m.recipient == "anna@example.be")
        assert [c["id"] for c in anna.data["communities"]] == [world.id_community]


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (D(2026, 4, 15), datetime.datetime(2026, 4, 14, 4, 0, tzinfo=UTC)),
        # The day after the clocks went forward: still 06:00 in Brussels.
        (D(2026, 3, 30), datetime.datetime(2026, 3, 29, 4, 0, tzinfo=UTC)),
        (D(2026, 3, 29), datetime.datetime(2026, 3, 28, 5, 0, tzinfo=UTC)),
    ],
)
def test_the_days_answers_start_at_yesterdays_run(today, expected):
    assert answers_since(today) == expected
    assert answers_since(today).astimezone(BRUSSELS).hour == 6


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
