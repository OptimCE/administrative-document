"""GET /admin/summary (the navigation's badge) and GET /admin/changes/{id} (the
CRM's toast): phase 4, spec Notifications."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text

from engine.routing import route_event
from ports.crm_engine import SqlEngineCrmRead
from regulators.cwape.plugin import CwapePlugin
from shared.const import DeadlineKind, DeadlineParty, DeadlineStatus
from shared.models.local_models import ChangeModel, DeadlineModel
from tests.conftest import gateway_headers
from tests.engine.world import EAN, make_world, meter

pytestmark = pytest.mark.asyncio


async def _world(db_session):
    return await make_world(db_session, plugin=CwapePlugin())


async def _headers(db_session, world, role: str = "MANAGER") -> dict[str, str]:
    auth_id = await db_session.scalar(
        text("SELECT auth_community_id FROM community WHERE id = :id"), {"id": world.id_community}
    )
    return gateway_headers(str(auth_id), role=role)


async def _file(world, ean: str) -> int:
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    result = await route_event(
        meter(world, "operation.meter_added", ean, at=when),
        local=world.session,
        crm=SqlEngineCrmRead(world.session),
        registry=world.registry,
    )
    return result.dossiers[0]


async def _summary(client, headers) -> dict:
    response = await client.get("/admin/summary", headers=headers)
    assert response.status_code == 200, response.text
    body: dict = response.json()["data"]
    return body


class TestTheBadge:
    async def test_counts_the_procedures_to_do_and_the_deadlines_past_due(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        assert await _summary(client, headers) == {"todo": 0, "overdue": 0}

        dossier_id = await _file(world, EAN[0])
        assert await _summary(client, headers) == {"todo": 1, "overdue": 0}

        today = datetime.date.today()
        for party, status in (
            (DeadlineParty.COMMUNITY, DeadlineStatus.MISSED),
            (DeadlineParty.GRD, DeadlineStatus.OPEN),
        ):
            db_session.add(
                DeadlineModel(
                    id_community=world.id_community,
                    id_dossier=dossier_id,
                    deadline_type=f"late_{party.name.lower()}",
                    due_date=today - datetime.timedelta(days=3),
                    status=int(status),
                    party=int(party),
                    kind=int(DeadlineKind.RESPONSE),
                )
            )
        # Not overdue: an effect date (the effect job's), and one still to come.
        db_session.add(
            DeadlineModel(
                id_community=world.id_community,
                id_dossier=dossier_id,
                deadline_type="effect",
                due_date=today - datetime.timedelta(days=1),
                status=int(DeadlineStatus.OPEN),
                party=int(DeadlineParty.GRD),
                kind=int(DeadlineKind.EFFECT),
            )
        )
        await db_session.flush()

        assert await _summary(client, headers) == {"todo": 1, "overdue": 2}

    async def test_is_for_managers_only(self, client, db_session):
        world = await _world(db_session)

        response = await client.get(
            "/admin/summary", headers=await _headers(db_session, world, role="MEMBER")
        )

        assert response.status_code == 403


class TestTheToast:
    async def test_says_what_was_filed_where(self, client, db_session):
        world = await _world(db_session)
        dossier_id = await _file(world, EAN[0])
        change = await db_session.scalar(
            select(ChangeModel).where(ChangeModel.id_dossier == dossier_id)
        )
        assert change is not None

        response = await client.get(
            f"/admin/changes/{change.id}", headers=await _headers(db_session, world)
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert (data["kind"], data["subject_ref"], data["id_dossier"]) == (
            "meter_added",
            EAN[0],
            dossier_id,
        )
        assert (data["procedure_code"], data["recipient"], data["operation_name"]) == (
            "PARTAGE_MODIF",
            "grd_ref",
            "Op Centre",
        )
        assert data["procedure_labels"]["fr"]

    async def test_another_communitys_change_is_a_404(self, client, db_session):
        world = await _world(db_session)
        elsewhere = await make_world(db_session, plugin=world.plugin)
        theirs = await _file(elsewhere, EAN[1])
        change = await db_session.scalar(
            select(ChangeModel).where(ChangeModel.id_dossier == theirs)
        )
        assert change is not None

        response = await client.get(
            f"/admin/changes/{change.id}", headers=await _headers(db_session, world)
        )

        assert response.status_code == 404
