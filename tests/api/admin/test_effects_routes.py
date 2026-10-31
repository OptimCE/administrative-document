"""The /admin routes and the core's badges (phase 3), with the CWaPE plugin.

A meter added to an operation goes the authorisation route: GRD, CWaPE, the
GRD's amendment, the amendment signed. Each step is recorded over HTTP, and
each shows in the core's ``regulatory_link_status``.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from engine.baseline import brussels_today
from engine.calendar import WorkingCalendar
from engine.routing import route_event
from ports.crm_engine import SqlEngineCrmRead
from regulators.cwape.plugin import CwapePlugin
from shared.const import ProcedureStatus, RegulatoryStatus
from tests.conftest import gateway_headers
from tests.engine.world import EAN, make_world, meter

pytestmark = pytest.mark.asyncio

TODAY = brussels_today(datetime.datetime.now(datetime.UTC))
WAL = WorkingCalendar(CwapePlugin().calendar())


async def _world(db_session):
    return await make_world(db_session, plugin=CwapePlugin())


async def _headers(db_session, world) -> dict[str, str]:
    auth_id = await db_session.scalar(
        text("SELECT auth_community_id FROM community WHERE id = :id"), {"id": world.id_community}
    )
    return gateway_headers(str(auth_id), role="MANAGER")


async def _file(world, event_type: str, ean: str) -> int:
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    result = await route_event(
        meter(world, event_type, ean, at=when),
        local=world.session,
        crm=SqlEngineCrmRead(world.session),
        registry=world.registry,
    )
    return result.dossiers[0]


async def _get(client, headers, dossier_id: int) -> dict:
    response = await client.get(f"/admin/dossiers/{dossier_id}", headers=headers)
    assert response.status_code == 200, response.text
    body: dict = response.json()["data"]
    return body


async def _complete_and_send(client, headers, dossier_id: int) -> dict:
    """Every required piece uploaded (an upload is a final), then sent today."""
    detail = await _get(client, headers, dossier_id)
    for piece in detail["pieces"]:
        if piece["required"] and piece["versions"] == 0:
            response = await client.post(
                f"/admin/dossiers/{dossier_id}/documents/{piece['id']}/versions",
                files={"file": (f"{piece['code']}.pdf", b"%PDF-1.4 x", "application/pdf")},
                headers=headers,
            )
            assert response.status_code == 200, response.text
    response = await client.post(
        f"/admin/dossiers/{dossier_id}/send",
        data={"sent_on": TODAY.isoformat()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict = response.json()["data"]
    return body


async def _respond(client, headers, dossier_id: int, type_: str, *, dry_run: bool = False):
    response = await client.post(
        f"/admin/dossiers/{dossier_id}/correspondence" + ("?dry_run=true" if dry_run else ""),
        data={"type": type_, "occurred_on": TODAY.isoformat()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _core_row(db_session, world, ean: str) -> dict:
    rows = (
        await db_session.execute(
            text(
                "SELECT regulatory_status, valid_from, valid_to, regulatory_ref "
                "FROM regulatory_link_status WHERE id_community = :c AND subject_ref = :ean"
            ),
            {"c": world.id_community, "ean": ean},
        )
    ).all()
    assert len(rows) == 1, rows
    return dict(rows[0]._mapping)


def _badge(detail: dict) -> tuple:
    (change,) = detail["changes"]
    return (change["regulatory_status"], change["valid_from"], change["valid_to"])


class TestCriterion31:
    """Acceptance criterion 3.1: recording « décision d'autorisation », then
    the GRD's « proposition d'avenant » and « avenant signé » (decided on
    30/10/2026: the catalogue keeps the GRD's proposal between the two), sets
    the effect date on the 20th working day, and the CRM shows it coming."""

    async def test_the_signed_amendment_sets_the_effect_on_the_20th_working_day(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_added", EAN[0])
        seen = [_badge(await _complete_and_send(client, headers, dossier_id))]
        for type_ in (
            "completeness_ack",
            "technical_opinion",
            "decision_granted",
            "amendment_proposal",
        ):
            seen.append(_badge(await _respond(client, headers, dossier_id, type_)))

        preview = await _respond(client, headers, dossier_id, "amendment_signed", dry_run=True)
        detail = await _respond(client, headers, dossier_id, "amendment_signed")

        effective = WAL.add_working_days(TODAY, 20)
        assert seen == [
            (int(RegulatoryStatus.PENDING_GRD), None, None),  # sent
            (int(RegulatoryStatus.PENDING_GRD), None, None),  # the GRD's completeness check
            (int(RegulatoryStatus.PENDING_REGULATOR), None, None),  # at the CWaPE
            (int(RegulatoryStatus.AUTHORIZED), None, None),  # authorised
            (int(RegulatoryStatus.AUTHORIZED), None, None),  # the GRD's amendment
        ]
        assert preview["to_status"] == int(ProcedureStatus.SENT)
        assert preview["effective_date"] == effective.isoformat()
        assert preview["effects"] == [
            {
                "subject_type": 1,
                "subject_ref": EAN[0],
                "regulatory_status": int(RegulatoryStatus.AUTHORIZED),
                "valid_from": effective.isoformat(),
                "valid_to": None,
            }
        ]
        assert detail["step"] == "amendment_signed"
        assert detail["effective_date"] == effective.isoformat()
        (effect,) = [d for d in detail["deadlines"] if d["code"] == "effect"]
        assert effect["due_date"] == effective.isoformat()
        assert _badge(detail) == (int(RegulatoryStatus.AUTHORIZED), effective.isoformat(), None)
        assert await _core_row(db_session, world, EAN[0]) == {
            "regulatory_status": int(RegulatoryStatus.AUTHORIZED),
            "valid_from": effective,
            "valid_to": None,
            "regulatory_ref": str(dossier_id),
        }

    async def test_a_later_date_the_community_asked_for_wins(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_added", EAN[0])
        later = TODAY + datetime.timedelta(days=200)
        await db_session.execute(
            text("UPDATE dossier SET requested_effective_date = :d WHERE id = :id"),
            {"d": later, "id": dossier_id},
        )
        await _complete_and_send(client, headers, dossier_id)
        for type_ in (
            "completeness_ack",
            "technical_opinion",
            "decision_granted",
            "amendment_proposal",
        ):
            await _respond(client, headers, dossier_id, type_)

        detail = await _respond(client, headers, dossier_id, "amendment_signed")

        assert detail["effective_date"] == later.isoformat()

    async def test_a_dry_run_writes_nothing_in_the_core(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_added", EAN[0])
        await _complete_and_send(client, headers, dossier_id)
        before = await _core_row(db_session, world, EAN[0])

        preview = await _respond(client, headers, dossier_id, "completeness_ack", dry_run=True)

        assert preview["effects"] == []  # still with the GRD
        assert await _core_row(db_session, world, EAN[0]) == before
