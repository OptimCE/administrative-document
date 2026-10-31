"""The /admin routes and the core's badges (phase 3), with the CWaPE plugin.

A meter added to an operation goes the authorisation route: GRD, CWaPE, the
GRD's amendment, the amendment signed. Each step is recorded over HTTP, and
each shows in the core's ``regulatory_link_status``.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text

from core.audit_log import AuditActions
from engine import effects
from engine.baseline import brussels_today
from engine.calendar import WorkingCalendar
from engine.events import BRUSSELS
from engine.routing import route_event
from ports.crm_engine import SqlEngineCrmRead
from regulators.cwape.plugin import CwapePlugin
from shared.const import ChangeStatus, DeadlineStatus, ProcedureStatus, RegulatoryStatus
from shared.models.local_models import ChangeModel, DeadlineModel, DossierModel
from tests.conftest import create_sharing_operation, gateway_headers
from tests.engine.world import EAN, make_world, meter
from worker.engine_jobs import run_daily_jobs

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


async def _complete_and_send(
    client, headers, dossier_id: int, *, on: datetime.date = TODAY
) -> dict:
    """Every required piece uploaded (an upload is a final), then sent ``on``."""
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
        data={"sent_on": on.isoformat()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict = response.json()["data"]
    return body


async def _respond(
    client,
    headers,
    dossier_id: int,
    type_: str,
    *,
    dry_run: bool = False,
    on: datetime.date = TODAY,
):
    response = await client.post(
        f"/admin/dossiers/{dossier_id}/correspondence" + ("?dry_run=true" if dry_run else ""),
        data={"type": type_, "occurred_on": on.isoformat()},
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


# -- criterion 3.2: on the effect date, by itself ------------------------------------

SENT = TODAY - datetime.timedelta(days=80)
SIGNED = TODAY - datetime.timedelta(days=60)
EFFECTIVE = WAL.add_working_days(SIGNED, 20)


def _morning(day: datetime.date) -> datetime.datetime:
    """06:00 in Brussels, when the scheduler runs the day."""
    return datetime.datetime.combine(day, datetime.time(6), BRUSSELS)


async def _signed(client, db_session, world) -> int:
    """A meter added, sent and taken to the signed amendment, all in the past."""
    headers = await _headers(db_session, world)
    dossier_id = await _file(world, "operation.meter_added", EAN[0])
    await _complete_and_send(client, headers, dossier_id, on=SENT)
    for type_ in (
        "completeness_ack",
        "technical_opinion",
        "decision_granted",
        "amendment_proposal",
    ):
        await _respond(client, headers, dossier_id, type_, on=SENT)
    detail = await _respond(client, headers, dossier_id, "amendment_signed", on=SIGNED)
    assert detail["effective_date"] == EFFECTIVE.isoformat()
    return dossier_id


async def _day(world, day: datetime.date):
    return await run_daily_jobs(
        registry=world.registry,
        local_session=world.session,
        crm_session=world.session,
        now=_morning(day),
    )


async def _audits(db_session, world) -> int:
    count = await db_session.scalar(
        text(
            "SELECT count(*) FROM audit_log WHERE id_community = :c AND action = :a "
            "AND payload->>'reason' = 'in_force'"
        ),
        {"c": world.id_community, "a": AuditActions.REGULATORY_EFFECT_APPLIED},
    )
    return int(count or 0)


async def _procedure(db_session, dossier_id: int) -> DossierModel:
    dossier: DossierModel | None = await db_session.get(DossierModel, dossier_id)
    assert dossier is not None
    await db_session.refresh(dossier)
    return dossier


class TestCriterion32:
    """Acceptance criterion 3.2: on the effect date, the meters are « Actif
    depuis » in the CRM with no manual action: the daily job writes the core,
    then puts the procedure in force."""

    async def test_on_its_effect_date_the_meter_is_active_with_no_manual_action(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        dossier_id = await _signed(client, db_session, world)

        eve = await _day(world, EFFECTIVE - datetime.timedelta(days=1))
        assert (eve.effects_applied, (await _procedure(db_session, dossier_id)).step) == (
            0,
            "amendment_signed",
        )
        assert (await _core_row(db_session, world, EAN[0]))["regulatory_status"] == int(
            RegulatoryStatus.AUTHORIZED
        )

        day = await _day(world, EFFECTIVE)

        procedure = await _procedure(db_session, dossier_id)
        assert day.effects_applied == 1
        assert (procedure.step, procedure.status) == ("in_force", int(ProcedureStatus.IN_FORCE))
        assert procedure.effective_date == EFFECTIVE
        assert await _core_row(db_session, world, EAN[0]) == {
            "regulatory_status": int(RegulatoryStatus.ACTIVE),
            "valid_from": EFFECTIVE,
            "valid_to": None,
            "regulatory_ref": str(dossier_id),
        }
        changes = (
            await db_session.scalars(
                select(ChangeModel).where(ChangeModel.id_dossier == dossier_id)
            )
        ).all()
        assert [c.status for c in changes] == [int(ChangeStatus.APPLIED)]
        effect = await db_session.scalar(
            select(DeadlineModel).where(
                DeadlineModel.id_dossier == dossier_id, DeadlineModel.deadline_type == "effect"
            )
        )
        assert effect is not None and effect.status == int(DeadlineStatus.MET)
        assert await _audits(db_session, world) == 1

    async def test_running_the_day_again_changes_nothing(self, client, db_session, admin_storage):
        world = await _world(db_session)
        await _signed(client, db_session, world)
        await _day(world, EFFECTIVE)

        again = await _day(world, EFFECTIVE)

        assert (again.effects_due, again.effects_applied) == (0, 0)
        assert await _audits(db_session, world) == 1

    async def test_a_meter_still_active_elsewhere_blocks_it_until_it_leaves(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        # Still active in another operation, open-ended: the core refuses the
        # badges along the way (kept as a "sync" error), then the effect.
        other = await create_sharing_operation(
            db_session, id_community=world.id_community, name="Op Sud"
        )
        await db_session.execute(
            text(
                "INSERT INTO regulatory_link_status (id_community, id_sharing_operation, "
                " subject_type, subject_ref, id_dossier_origin, regulatory_status, valid_from) "
                "VALUES (:c, :op, 1, :ean, 999999, 5, :since)"
            ),
            {"c": world.id_community, "op": other, "ean": EAN[0], "since": SENT},
        )
        dossier_id = await _signed(client, db_session, world)
        assert (await _procedure(db_session, dossier_id)).effect_error_json["kind"] == "sync"

        blocked = await _day(world, EFFECTIVE)

        procedure = await _procedure(db_session, dossier_id)
        assert blocked.effects_blocked == 1
        assert procedure.step == "amendment_signed"
        assert procedure.effect_error_json["kind"] == "in_force"
        assert procedure.effect_error_json["subjects"] == [EAN[0]]

        await db_session.execute(
            text(
                "UPDATE regulatory_link_status SET regulatory_status = 7, valid_to = :last "
                "WHERE id_dossier_origin = 999999"
            ),
            {"last": EFFECTIVE - datetime.timedelta(days=1)},
        )
        unblocked = await _day(world, EFFECTIVE + datetime.timedelta(days=1))

        procedure = await _procedure(db_session, dossier_id)
        assert unblocked.effects_applied == 1
        assert procedure.step == "in_force"
        assert procedure.effect_error_json is None

    async def test_a_crash_between_the_core_and_the_step_is_completed_the_next_day(
        self, client, db_session, admin_storage, monkeypatch
    ):
        world = await _world(db_session)
        dossier_id = await _signed(client, db_session, world)
        real = effects.move_to_step

        async def crash(*_a, **_kw):
            raise RuntimeError("the worker died here")

        monkeypatch.setattr(effects, "move_to_step", crash)
        await _day(world, EFFECTIVE)
        assert (await _procedure(db_session, dossier_id)).step == "amendment_signed"
        assert (await _core_row(db_session, world, EAN[0]))["regulatory_status"] == int(
            RegulatoryStatus.ACTIVE
        )

        monkeypatch.setattr(effects, "move_to_step", real)
        await _day(world, EFFECTIVE + datetime.timedelta(days=1))

        assert (await _procedure(db_session, dossier_id)).step == "in_force"
        assert await _audits(db_session, world) == 1  # the core was not written twice


class TestIntoForceByHand:
    async def test_the_core_is_written_first_on_the_date_given(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _signed(client, db_session, world)
        on = EFFECTIVE + datetime.timedelta(days=3)

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/transition",
            json={"to_step": "in_force", "reason": "GRD confirmed by phone", "on": on.isoformat()},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert (detail["step"], detail["effective_date"]) == ("in_force", on.isoformat())
        assert _badge(detail) == (int(RegulatoryStatus.ACTIVE), on.isoformat(), None)
        assert (await _core_row(db_session, world, EAN[0]))["valid_from"] == on

    async def test_a_core_that_refuses_is_a_409_listing_the_meters(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        other = await create_sharing_operation(
            db_session, id_community=world.id_community, name="Op Sud"
        )
        await db_session.execute(
            text(
                "INSERT INTO regulatory_link_status (id_community, id_sharing_operation, "
                " subject_type, subject_ref, id_dossier_origin, regulatory_status, valid_from) "
                "VALUES (:c, :op, 1, :ean, 999999, 5, :since)"
            ),
            {"c": world.id_community, "op": other, "ean": EAN[0], "since": SENT},
        )
        dossier_id = await _signed(client, db_session, world)

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/transition",
            json={"to_step": "in_force", "reason": "by hand"},
            headers=headers,
        )

        assert response.status_code == 409, response.text
        body = response.json()
        assert (body["error_code"], body["conflicts"]) == (2391, [EAN[0]])
        assert (await _procedure(db_session, dossier_id)).step == "amendment_signed"
