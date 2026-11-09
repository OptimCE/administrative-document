"""The summary email (``worker/digest.py``), through the real daily job.

Off unless a community's admin turned it on (``digest_setting``); then sent
on its scheduled dates to the recipients chosen, still admins or managers, who
did not stop it in their profile. Criterion 4.1: at most one email per
recipient and day, even with ten deadlines close. Its news is its period's,
read from the database. Frozen dates; one Postgres serves as both sessions.
2026-04-14 is a Tuesday.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Sequence
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from engine.lifecycle import context_for, open_procedure
from shared.const import (
    Channel,
    CorrespondenceDirection,
    Counterparty,
    DeadlineKind,
    DeadlineParty,
    DeadlineStatus,
    DigestCadence,
    InboundMailStatus,
    OpenedBy,
    ProcedureStatus,
    SubjectType,
)
from shared.models.crm_notification_models import OutboundMessageModel
from shared.models.local_models import (
    CorrespondenceModel,
    DeadlineModel,
    DigestSettingModel,
    DossierModel,
    InboundAddressModel,
    InboundMailModel,
    StatusEventModel,
)
from tests.api.admin.test_effects_routes import EFFECTIVE, _morning
from tests.api.admin.test_effects_routes import _day as cwape_day
from tests.api.admin.test_effects_routes import _signed as signed_amendment
from tests.api.admin.test_effects_routes import _world as cwape_world
from tests.engine.world import World, make_world
from tests.notifications import notifications_of, seed_roster
from worker.engine_jobs import run_daily_jobs

pytestmark = pytest.mark.asyncio

UTC = datetime.UTC
D = datetime.date
TUESDAY_RUN = datetime.datetime(2026, 4, 14, 4, 0, tzinfo=UTC)  # 06:00 in Brussels
# Tuesday 15:00 in Brussels: after Tuesday's run, so in Wednesday's period.
TUESDAY_AFTERNOON = datetime.datetime(2026, 4, 14, 13, 0, tzinfo=UTC)
WEDNESDAY = datetime.datetime(2026, 4, 15, 4, 0, tzinfo=UTC)
THURSDAY = datetime.datetime(2026, 4, 16, 4, 0, tzinfo=UTC)
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


async def _turn_on(
    world: World,
    recipients: Sequence[int],
    *,
    cadence: DigestCadence = DigestCadence.WORKING_DAYS,
    weekday: int | None = None,
    first_on: datetime.date | None = None,
    since: datetime.datetime = TUESDAY_RUN,
    period_from: datetime.datetime | None = None,
    enabled: bool = True,
) -> DigestSettingModel:
    """The community's summary, as an admin saves it ``since``."""
    row = DigestSettingModel(
        id_community=world.id_community,
        enabled=enabled,
        cadence=int(cadence),
        weekday=weekday,
        first_on=first_on,
        recipient_ids=list(recipients),
        enabled_at=since,
        period_from=period_from or since,
        updated_by="anna",
    )
    world.session.add(row)
    await world.session.flush()
    return row


async def _setting(world: World) -> DigestSettingModel:
    row = await world.session.get(DigestSettingModel, world.id_community)
    assert row is not None
    await world.session.refresh(row)
    return row


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
    status: DeadlineStatus = DeadlineStatus.OPEN,
    resolved_at: datetime.datetime | None = None,
) -> DeadlineModel:
    row = DeadlineModel(
        id_community=world.id_community,
        id_dossier=dossier.id,
        deadline_type=type_,
        due_date=due,
        status=int(status),
        party=int(party),
        kind=int(
            DeadlineKind.SUBMISSION if party == DeadlineParty.COMMUNITY else DeadlineKind.RESPONSE
        ),
        resolved_at=resolved_at,
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


async def _in_force_by_hand(
    world: World, dossier: DossierModel, at: datetime.datetime, effective: datetime.date
) -> None:
    """The journal of a procedure a manager put in force ``at``."""
    dossier.effective_date = effective
    world.session.add(
        StatusEventModel(
            id_community=world.id_community,
            subject_type=int(SubjectType.DOSSIER),
            subject_id=dossier.id,
            from_status=dossier.status,
            to_status=int(ProcedureStatus.IN_FORCE),
            from_step=dossier.step,
            to_step="in_force",
            actor_id="anna",
            occurred_at=at,
            context_json={"reason": "the GRD confirmed it by phone", "manual": True},
        )
    )
    await world.session.flush()


class TestOffByDefault:
    async def test_nothing_is_sent_unless_an_admin_turned_it_on(self, db_session):
        world = await make_world(db_session)
        await seed_roster(world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"})
        dossier = await _procedure(world)
        await _deadline(world, dossier, D(2026, 4, 20))
        await _deadline(world, dossier, D(2026, 4, 10), type_="late")  # missed today

        await _run(world, WEDNESDAY)

        assert await _summaries(world) == []
        # The in-app reminders do not depend on it.
        assert len(await notifications_of(world.session, "admin_deadline.due_soon")) == 1

    async def test_turned_off_sends_nothing(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]], enabled=False)
        await _deadline(world, await _procedure(world), D(2026, 4, 20))

        await _run(world, WEDNESDAY)

        assert await _summaries(world) == []


class TestTheSchedule:
    async def test_only_on_its_scheduled_day(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]], cadence=DigestCadence.WEEKLY, weekday=1)
        dossier = await _procedure(world)
        await _deadline(world, dossier, D(2026, 4, 28))
        await _deadline(world, dossier, D(2026, 4, 10), type_="late", party=DeadlineParty.GRD)

        await _run(world, WEDNESDAY)
        assert await _summaries(world) == []

        await _run(world, MONDAY)

        (summary,) = await _summaries(world)
        assert summary.data["as_of"] == "2026-04-20"
        (section,) = summary.data["communities"]
        assert [i["due_date"] for i in section["upcoming"]] == ["2026-04-28"]
        assert [i["party"] for i in section["overdue"]] == ["grd"]

    async def test_a_missed_run_is_caught_up_at_the_next(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]], cadence=DigestCadence.WEEKLY, weekday=3)
        await _deadline(world, await _procedure(world), D(2026, 4, 24))

        await _run(world, THURSDAY)  # Wednesday's run never happened
        await _run(world, THURSDAY + datetime.timedelta(days=1))

        (summary,) = await _summaries(world)
        assert summary.data["as_of"] == "2026-04-16"
        assert (await _setting(world)).last_due_on == D(2026, 4, 15)

    async def test_turned_on_after_the_days_run_waits_for_the_next_date(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        wednesday_morning = datetime.datetime(2026, 4, 15, 8, 0, tzinfo=UTC)  # 10:00
        await _turn_on(world, [ids["anna"]], since=wednesday_morning)
        await _deadline(world, await _procedure(world), D(2026, 4, 24))

        await _run(world, wednesday_morning + datetime.timedelta(hours=5))
        assert await _summaries(world) == []

        await _run(world, THURSDAY)

        assert [m.data["as_of"] for m in await _summaries(world)] == ["2026-04-16"]

    async def test_nothing_to_tell_sends_nothing_and_the_period_moves_on(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])

        await _run(world, WEDNESDAY)

        assert await _summaries(world) == []
        setting = await _setting(world)
        assert setting.last_due_on == D(2026, 4, 15)
        assert setting.period_from is not None
        assert setting.period_from > TUESDAY_RUN


class TestRecipients:
    async def test_only_the_chosen_still_admins_or_managers(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER", "member": "MEMBER"},
        )
        # Bram is not chosen; the member was, but is no manager (any more).
        await _turn_on(world, [ids["anna"], ids["member"]])
        await _deadline(world, await _procedure(world), D(2026, 4, 20))

        await _run(world, WEDNESDAY)

        assert await _recipients(world) == ["anna@example.be"]

    async def test_a_recipient_who_stopped_it_in_their_profile_gets_nothing(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER"},
        )
        await _turn_on(world, [ids["anna"], ids["bram"]])
        await _prefer(world, ids["bram"], 3)
        await _deadline(world, await _procedure(world), D(2026, 4, 20))

        await _run(world, WEDNESDAY)

        assert await _recipients(world) == ["anna@example.be"]
        # Their in-app reminders stay.
        assert len(await notifications_of(world.session, "admin_deadline.due_soon")) == 2


class TestCriterion41:
    async def test_ten_close_deadlines_make_one_email_per_recipient(self, db_session):
        world = await make_world(db_session)
        other = await make_world(db_session, plugin=world.plugin)
        ids = await seed_roster(
            world.session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER", "member": "MEMBER"},
        )
        await _also_manager(other, ids["bram"])  # Bram manages both communities
        await _turn_on(world, [ids["anna"], ids["bram"]])
        await _turn_on(other, [ids["bram"]])
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
        assert anna.data["counts"] == {"news": 0, "overdue": 0, "upcoming": 10}
        assert anna.dedupe_key == f"{DIGEST}:u{ids['anna']}:2026-04-15"
        # In-app is still one reminder per deadline and manager: ten for each
        # of the two, and Bram's one in the other community. Only the email is
        # grouped.
        assert len(await notifications_of(world.session, "admin_deadline.due_soon")) == 10 * 2 + 1

    async def test_rerunning_the_day_sends_no_second_email(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])
        await _deadline(world, await _procedure(world), D(2026, 4, 20))

        await _run(world, WEDNESDAY)
        await _run(world, WEDNESDAY + datetime.timedelta(hours=3))

        assert await _recipients(world) == ["anna@example.be"]

    async def test_a_setting_not_moved_on_still_sends_one_email_that_day(self, db_session):
        """The CRM commits first: a lost local commit leaves the date due,
        and the rerun's email is the same message (its dedupe key)."""
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])
        await _deadline(world, await _procedure(world), D(2026, 4, 20))
        await _run(world, WEDNESDAY)
        setting = await _setting(world)
        setting.last_due_on = None
        setting.period_from = TUESDAY_RUN
        await world.session.flush()

        await _run(world, WEDNESDAY + datetime.timedelta(hours=1))

        assert await _recipients(world) == ["anna@example.be"]
        assert (await _setting(world)).last_due_on == D(2026, 4, 15)


class TestThePeriod:
    async def test_the_deadlines_news_are_the_periods(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])
        dossier = await _procedure(world)
        # Missed before the period: still overdue, no news any more.
        before = datetime.datetime(2026, 4, 2, 4, 0, tzinfo=UTC)
        await _deadline(
            world,
            dossier,
            D(2026, 4, 1),
            type_="old",
            status=DeadlineStatus.MISSED,
            resolved_at=before,
        )
        await _deadline(world, dossier, D(2026, 4, 10), type_="ours")  # missed by this run
        await _deadline(world, dossier, D(2026, 4, 10), type_="theirs", party=DeadlineParty.GRD)

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (section,) = summary.data["communities"]
        news = [(n["deadline"]["en"], n["news"]) for n in section["news"]]
        assert news == [("ours", "missed"), ("theirs", "late")]
        assert [i["deadline"]["en"] for i in section["overdue"]] == ["old", "ours", "theirs"]
        assert section["since"] == TUESDAY_RUN.isoformat()
        assert section["since_display"] == "14/04/2026"

    async def test_a_procedure_put_in_force_by_hand_is_news(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])
        ours = await _procedure(world)
        await _in_force_by_hand(world, ours, TUESDAY_AFTERNOON, D(2026, 4, 14))
        earlier = await _procedure(world)  # before the period
        await _in_force_by_hand(
            world, earlier, TUESDAY_RUN - datetime.timedelta(days=1), D(2026, 4, 13)
        )

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (news,) = summary.data["communities"][0]["news"]
        assert (news["news"], news["dossier_id"], news["effective_display"]) == (
            "in_force",
            ours.id,
            "14/04/2026",
        )

    async def test_what_is_coming_reaches_the_next_summary(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        # The month's first Monday: 4 May, then 1 June.
        await _turn_on(world, [ids["anna"]], cadence=DigestCadence.MONTHLY, weekday=1)
        dossier = await _procedure(world)
        await _deadline(world, dossier, D(2026, 5, 25), type_="within")
        await _deadline(world, dossier, D(2026, 6, 5), type_="after")

        await _run(world, datetime.datetime(2026, 5, 4, 4, 0, tzinfo=UTC))

        (summary,) = await _summaries(world)
        (section,) = summary.data["communities"]
        assert [i["deadline"]["en"] for i in section["upcoming"]] == ["within"]
        assert (section["until"], section["until_display"]) == ("2026-06-01", "01/06/2026")

    async def test_at_least_the_next_fourteen_days(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])  # every working day
        dossier = await _procedure(world)
        await _deadline(world, dossier, D(2026, 4, 29), type_="within")
        await _deadline(world, dossier, D(2026, 4, 30), type_="after")

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (section,) = summary.data["communities"]
        assert [i["deadline"]["en"] for i in section["upcoming"]] == ["within"]
        assert section["until"] == "2026-04-29"


class TestTheContent:
    async def test_items_carry_four_languages_a_date_and_a_link(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])
        dossier = await _procedure(world)
        await _deadline(world, dossier, D(2026, 4, 20))

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (section,) = summary.data["communities"]
        (item,) = section["upcoming"]
        assert item["days_left"] == 5
        assert item["due_display"] == "20/04/2026"
        assert item["path"] == f"/administrative-document/procedures/{dossier.id}"
        assert set(item["procedure"]) == {"fr", "en", "nl", "de"}
        assert all(item["procedure"].values())
        assert summary.data["as_of_display"] == "15/04/2026"


class TestTheOtherNews:
    """Écart 6 of the phase-4 recap: a period whose only news is a procedure
    in force, or an answer, sends its summary too, still one per recipient."""

    async def test_an_effect_applied_alone_sends_one_summary_per_recipient(
        self, client, db_session, fake_storage
    ):
        world = await cwape_world(db_session)
        ids = await seed_roster(
            db_session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER"},
        )
        dossier_id = await signed_amendment(client, db_session, world)
        # On before the effect's morning; its period starts now, after the
        # answers the API recorded on the way to the signed amendment.
        await _turn_on(
            world,
            [ids["anna"], ids["bram"]],
            since=_morning(EFFECTIVE) - datetime.timedelta(days=1),
            period_from=datetime.datetime.now(UTC),
        )

        day = await cwape_day(world, EFFECTIVE)
        await cwape_day(world, EFFECTIVE)  # the day again: no second email

        assert day.effects_applied == 1
        summaries = await _summaries(world)
        assert sorted(m.recipient for m in summaries) == ["anna@example.be", "bram@example.be"]
        anna = next(m for m in summaries if m.recipient == "anna@example.be")
        assert anna.dedupe_key == f"{DIGEST}:u{ids['anna']}:{EFFECTIVE.isoformat()}"
        (section,) = anna.data["communities"]
        (news,) = section["news"]  # the effect, and nothing else
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

    async def test_an_answer_recorded_alone_sends_one_summary_per_recipient(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER", "member": "MEMBER"},
        )
        await _turn_on(world, [ids["anna"], ids["bram"]])
        dossier = await _procedure(world)
        await _answer(world, dossier, TUESDAY_AFTERNOON)

        await _run(world, WEDNESDAY)
        await _run(world, WEDNESDAY + datetime.timedelta(hours=3))

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
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])
        dossier = await _procedure(world)
        await _mail(world, dossier, TUESDAY_AFTERNOON)

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (news,) = summary.data["communities"][0]["news"]
        assert (news["news"], news["dossier_id"], news["response"]) == ("answer", dossier.id, None)

    async def test_a_mail_qualified_counts_once_as_its_answer(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])
        dossier = await _procedure(world)
        recorded = await _answer(world, dossier, TUESDAY_AFTERNOON)
        await _mail(
            world, dossier, TUESDAY_AFTERNOON, InboundMailStatus.RECORDED, correspondence=recorded
        )

        await _run(world, WEDNESDAY)

        (summary,) = await _summaries(world)
        (news,) = summary.data["communities"][0]["news"]
        assert news["response"] is not None

    async def test_what_is_not_an_answer_of_the_period_sends_nothing(self, db_session):
        world = await make_world(db_session)
        ids = await seed_roster(
            world.session, SimpleNamespace(id=world.id_community), {"anna": "ADMIN"}
        )
        await _turn_on(world, [ids["anna"]])
        dossier = await _procedure(world)
        before_the_period = datetime.datetime(2026, 4, 14, 3, 30, tzinfo=UTC)  # 05:30
        await _answer(world, dossier, before_the_period)
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

    async def test_two_communities_due_the_same_day_make_one_email(self, db_session):
        world = await make_world(db_session)
        other = await make_world(db_session, plugin=world.plugin)
        ids = await seed_roster(
            world.session,
            SimpleNamespace(id=world.id_community),
            {"anna": "ADMIN", "bram": "MANAGER"},
        )
        await _also_manager(other, ids["bram"])
        await _turn_on(world, [ids["anna"], ids["bram"]])
        await _turn_on(other, [ids["bram"]], cadence=DigestCadence.WEEKLY, weekday=3)
        await _deadline(world, await _procedure(world), D(2026, 4, 20))
        await _answer(other, await _procedure(other), TUESDAY_AFTERNOON)

        await _run(world, WEDNESDAY)
        await _run(world, WEDNESDAY + datetime.timedelta(hours=3))

        summaries = await _summaries(world)
        assert sorted(m.recipient for m in summaries) == ["anna@example.be", "bram@example.be"]
        bram = next(m for m in summaries if m.recipient == "bram@example.be")
        parts = {
            c["id"]: ([n["news"] for n in c["news"]], len(c["upcoming"]))
            for c in bram.data["communities"]
        }
        assert parts == {world.id_community: ([], 1), other.id_community: (["answer"], 0)}
        assert bram.data["counts"] == {"news": 1, "overdue": 0, "upcoming": 1}
        anna = next(m for m in summaries if m.recipient == "anna@example.be")
        assert [c["id"] for c in anna.data["communities"]] == [world.id_community]
