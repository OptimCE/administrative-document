"""GET /admin/baseline and POST /admin/baseline/confirm, over HTTP, with CWaPE."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from regulators.cwape.plugin import CwapePlugin
from tests.conftest import create_sharing_operation, gateway_headers
from tests.engine.world import EAN, add_meter, make_world

pytestmark = pytest.mark.asyncio


async def _cwape_world(db_session, **kw):
    world = await make_world(db_session, plugin=CwapePlugin(), **kw)
    await add_meter(world, EAN[0])
    await add_meter(world, EAN[1], injection=3)
    return world


async def _headers(db_session, world, role: str = "MANAGER") -> dict[str, str]:
    auth_id = await db_session.scalar(
        text("SELECT auth_community_id FROM community WHERE id = :id"), {"id": world.id_community}
    )
    return gateway_headers(str(auth_id), role=role)


class TestTheStartingState:
    async def test_is_listed_for_confirmation(self, client, db_session):
        world = await _cwape_world(db_session)
        response = await client.get("/admin/baseline", headers=await _headers(db_session, world))

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert (body["status"], body["kind"], body["regulator"]) == (
            "pending",
            "first",
            "BE-WAL-CWAPE",
        )
        assert sorted(i["subject_ref"] for i in body["items"]) == [EAN[0], EAN[1]]
        item = body["items"][0]
        assert item["choices"] == ["active", "not_notified", "in_progress"]
        assert item["default_choice"] == "active"
        assert item["operation_name"] == "Op Centre"

    async def test_is_confirmed_with_the_choices_made(self, client, db_session):
        world = await _cwape_world(db_session)
        headers = await _headers(db_session, world)
        listed = (await client.get("/admin/baseline", headers=headers)).json()["data"]
        first = next(i for i in listed["items"] if i["subject_ref"] == EAN[0])

        response = await client.post(
            "/admin/baseline/confirm",
            json={"choices": [{"key": first["key"], "choice": "not_notified"}]},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["status"] == "confirmed"
        chosen = {i["subject_ref"]: i["chosen"] for i in body["items"]}
        assert chosen == {EAN[0]: "not_notified", EAN[1]: "active"}

    async def test_a_conflict_in_the_core_is_a_409_naming_the_meters(self, client, db_session):
        world = await _cwape_world(db_session)
        elsewhere = await create_sharing_operation(
            db_session, id_community=world.id_community, name="Op Sud"
        )
        await db_session.execute(
            text(
                "INSERT INTO regulatory_link_status (id_community, id_sharing_operation, "
                " subject_type, subject_ref, id_dossier_origin, regulatory_status) "
                "VALUES (:c, :o, 1, :ean, 999, 5)"
            ),
            {"c": world.id_community, "o": elsewhere, "ean": EAN[1]},
        )

        response = await client.post(
            "/admin/baseline/confirm", json={}, headers=await _headers(db_session, world)
        )

        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == 2373
        assert response.json()["conflicts"] == [EAN[1]]

    async def test_a_choice_that_does_not_fit_is_a_422(self, client, db_session):
        world = await _cwape_world(db_session)
        headers = await _headers(db_session, world)
        listed = (await client.get("/admin/baseline", headers=headers)).json()["data"]

        response = await client.post(
            "/admin/baseline/confirm",
            json={"choices": [{"key": listed["items"][0]["key"], "choice": "in_progress"}]},
            headers=headers,
        )

        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == 2372

    async def test_is_for_managers_only(self, client, db_session):
        world = await _cwape_world(db_session)
        response = await client.get(
            "/admin/baseline", headers=await _headers(db_session, world, role="MEMBER")
        )
        assert response.status_code == 403

    async def test_a_regulator_without_a_plugin_has_none(self, client, db_session):
        world = await make_world(db_session, plugin=CwapePlugin(), regulator="BE-BRU-BRUGEL")
        response = await client.get("/admin/baseline", headers=await _headers(db_session, world))
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "not_supported"
