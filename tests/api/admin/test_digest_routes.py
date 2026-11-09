"""The summary email's setting (phase 4): every manager reads it, an admin
turns it on, picks its rhythm and its recipients.

Frozen clock: Wednesday 15 April 2026, 10:00 in Brussels, after the day's run.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from api.admin.digest import DigestSettingsService
from regulators.cwape.plugin import CwapePlugin
from shared.models.local_models import DigestSettingModel
from tests.conftest import gateway_headers
from tests.engine.world import make_world
from tests.notifications import seed_roster

pytestmark = pytest.mark.asyncio

NOW = datetime.datetime(2026, 4, 15, 8, 0, tzinfo=datetime.UTC)
D = datetime.date
TUESDAY = 2
URL = "/admin/digest-settings"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    frozen = {"now": NOW}
    monkeypatch.setattr(DigestSettingsService, "_now", staticmethod(lambda: frozen["now"]))
    return frozen


async def _world(db_session):
    world = await make_world(db_session, plugin=CwapePlugin())
    ids = await seed_roster(
        db_session,
        SimpleNamespace(id=world.id_community),
        {"anna": "ADMIN", "bram": "MANAGER", "mia": "MEMBER"},
    )
    return world, ids


async def _headers(db_session, world, role: str = "MANAGER") -> dict[str, str]:
    auth_id = await db_session.scalar(
        text("SELECT auth_community_id FROM community WHERE id = :id"), {"id": world.id_community}
    )
    return gateway_headers(str(auth_id), role=role)


async def _put(client, db_session, world, body: dict, role: str = "ADMIN"):
    return await client.put(URL, json=body, headers=await _headers(db_session, world, role))


async def _row(db_session, world) -> DigestSettingModel:
    row: DigestSettingModel | None = await db_session.get(DigestSettingModel, world.id_community)
    assert row is not None
    await db_session.refresh(row)
    return row


def _weekly(ids: dict[str, int], **extra) -> dict:
    return {
        "enabled": True,
        "cadence": "weekly",
        "weekday": TUESDAY,
        "recipient_ids": [ids["anna"], ids["bram"]],
        **extra,
    }


class TestRead:
    async def test_off_by_default(self, client, db_session):
        world, _ = await _world(db_session)

        response = await client.get(URL, headers=await _headers(db_session, world))

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert (data["enabled"], data["recipient_ids"], data["next_on"]) == (False, [], None)
        assert (data["cadence"], data["weekday"]) == ("weekly", 1)
        # Every two weeks may start on one of the next two Tuesdays.
        assert data["first_dates"][TUESDAY - 1] == ["2026-04-21", "2026-04-28"]

    async def test_members_have_no_access(self, client, db_session):
        world, _ = await _world(db_session)

        response = await client.get(URL, headers=await _headers(db_session, world, "MEMBER"))

        assert response.status_code == 403

    async def test_who_stopped_it_in_their_profile(self, client, db_session):
        world, ids = await _world(db_session)
        await _put(client, db_session, world, _weekly(ids))
        await db_session.execute(
            text(
                "INSERT INTO notification_preference (id_user, type_prefix, channel, mode) "
                "VALUES (:u, 'admin_digest', 2, 3)"
            ),
            {"u": ids["bram"]},
        )

        response = await client.get(URL, headers=await _headers(db_session, world))

        assert response.json()["data"]["opted_out"] == [ids["bram"]]


class TestSave:
    async def test_an_admin_turns_it_on(self, client, db_session):
        world, ids = await _world(db_session)

        response = await _put(client, db_session, world, _weekly(ids))

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["enabled"] is True
        assert data["recipient_ids"] == sorted([ids["anna"], ids["bram"]])
        assert data["next_on"] == "2026-04-21"  # this Wednesday's run is past
        row = await _row(db_session, world)
        assert (row.enabled_at, row.period_from, row.last_due_on) == (NOW, NOW, None)
        # Every manager reads it.
        read = await client.get(URL, headers=await _headers(db_session, world))
        assert read.json()["data"]["next_on"] == "2026-04-21"

    async def test_a_manager_cannot_change_it(self, client, db_session):
        world, ids = await _world(db_session)

        response = await _put(client, db_session, world, _weekly(ids), role="MANAGER")

        assert response.status_code == 403
        assert await db_session.get(DigestSettingModel, world.id_community) is None

    async def test_working_days_take_no_weekday(self, client, db_session):
        world, ids = await _world(db_session)

        response = await _put(
            client, db_session, world, _weekly(ids, cadence="working_days", weekday=5)
        )

        data = response.json()["data"]
        assert (data["cadence"], data["weekday"], data["next_on"]) == (
            "working_days",
            None,
            "2026-04-16",
        )

    async def test_every_two_weeks_from_a_date_offered(self, client, db_session, clock):
        world, ids = await _world(db_session)
        body = _weekly(ids, cadence="biweekly", first_on="2026-04-28")

        response = await _put(client, db_session, world, body)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["next_on"] == "2026-04-28"
        # A month later, saved again: the first date, past now, is kept.
        clock["now"] = NOW + datetime.timedelta(days=30)
        again = await _put(client, db_session, world, {**body, "recipient_ids": [ids["anna"]]})
        assert again.status_code == 200, again.text
        assert again.json()["data"]["next_on"] == "2026-05-26"

    async def test_changing_the_rhythm_keeps_the_period(self, client, db_session, clock):
        world, ids = await _world(db_session)
        await _put(client, db_session, world, _weekly(ids))
        later = NOW + datetime.timedelta(days=3)
        clock["now"] = later

        await _put(client, db_session, world, _weekly(ids, cadence="monthly"))

        row = await _row(db_session, world)
        assert (row.enabled_at, row.period_from) == (later, NOW)

    async def test_the_recipients_alone_move_nothing(self, client, db_session, clock):
        world, ids = await _world(db_session)
        await _put(client, db_session, world, _weekly(ids))
        clock["now"] = NOW + datetime.timedelta(days=3)

        await _put(client, db_session, world, _weekly(ids, recipient_ids=[ids["anna"]]))

        row = await _row(db_session, world)
        assert (row.enabled_at, row.period_from, row.recipient_ids) == (NOW, NOW, [ids["anna"]])

    async def test_off_then_on_again_starts_afresh(self, client, db_session, clock):
        world, ids = await _world(db_session)
        await _put(client, db_session, world, _weekly(ids))
        row = await _row(db_session, world)
        row.last_due_on = D(2026, 4, 21)
        await db_session.flush()
        await _put(client, db_session, world, _weekly(ids, enabled=False))
        off = await _row(db_session, world)
        assert (off.enabled, off.period_from) == (False, NOW)
        later = NOW + datetime.timedelta(days=20)
        clock["now"] = later

        await _put(client, db_session, world, _weekly(ids))

        row = await _row(db_session, world)
        assert (row.enabled_at, row.period_from, row.last_due_on) == (later, later, None)

    async def test_turning_it_off_keeps_the_choices(self, client, db_session):
        world, ids = await _world(db_session)
        await _put(client, db_session, world, _weekly(ids))

        response = await _put(client, db_session, world, _weekly(ids, enabled=False))

        data = response.json()["data"]
        assert (data["enabled"], data["weekday"], data["next_on"]) == (False, TUESDAY, None)
        assert data["recipient_ids"] == sorted([ids["anna"], ids["bram"]])

    @pytest.mark.parametrize(
        "change",
        [
            {"recipient_ids": []},  # on, to nobody
            {"recipient_ids": ["mia"]},  # a member
            {"recipient_ids": [999_999]},  # nobody of the community
            {"weekday": None},
            {"weekday": 8},
            {"cadence": "biweekly", "first_on": None},
            {"cadence": "biweekly", "first_on": "2026-05-05"},  # not offered
            {"cadence": "biweekly", "first_on": "2026-04-22"},  # a Wednesday
        ],
    )
    async def test_a_setting_that_does_not_hold_is_refused(self, client, db_session, change):
        world, ids = await _world(db_session)
        if change.get("recipient_ids") == ["mia"]:
            change = {"recipient_ids": [ids["anna"], ids["mia"]]}

        response = await _put(client, db_session, world, _weekly(ids, **change))

        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == 2398
        assert await db_session.get(DigestSettingModel, world.id_community) is None

    async def test_an_unknown_rhythm_is_refused(self, client, db_session):
        world, ids = await _world(db_session)

        response = await _put(client, db_session, world, _weekly(ids, cadence="hourly"))

        assert response.status_code == 422
