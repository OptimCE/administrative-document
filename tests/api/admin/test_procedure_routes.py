"""The /admin procedure routes over HTTP, with the CWaPE plugin.

Procedures are filed the way production files them: a core event through the
router, then the routes act on them.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import func, select, text

from api.admin.procedures import ProcedureService
from engine.baseline import brussels_today
from engine.calendar import WorkingCalendar
from engine.routing import route_event
from ports.crm_engine import SqlEngineCrmRead
from regulators.cwape.plugin import CwapePlugin
from shared.models.local_models import DeadlineModel, StatusEventModel
from tests.conftest import create_sharing_operation, gateway_headers
from tests.engine.world import EAN, make_world, meter

pytestmark = pytest.mark.asyncio

TODAY = brussels_today(datetime.datetime.now(datetime.UTC))
WAL = WorkingCalendar(CwapePlugin().calendar())


async def _world(db_session):
    return await make_world(db_session, plugin=CwapePlugin())


async def _headers(db_session, world, role: str = "MANAGER") -> dict[str, str]:
    auth_id = await db_session.scalar(
        text("SELECT auth_community_id FROM community WHERE id = :id"), {"id": world.id_community}
    )
    return gateway_headers(str(auth_id), role=role)


async def _file(world, event_type: str, ean: str, **kw) -> int:
    """A meter event through the router, as the worker files it."""
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    result = await route_event(
        meter(world, event_type, ean, at=when, **kw),
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


def _piece(detail: dict, code: str) -> dict:
    return next(p for p in detail["pieces"] if p["code"] == code)


async def _ready(client, headers, dossier_id: int) -> dict:
    """Upload the two generated pieces a simple modification requires."""
    detail = await _get(client, headers, dossier_id)
    for code in ("sharing_modification", "annex6_sharing_form"):
        response = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{_piece(detail, code)['id']}/versions",
            files={"file": (f"{code}.pdf", b"%PDF-1.4 " + code.encode(), "application/pdf")},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
    return detail


async def _send(client, headers, dossier_id: int, **data) -> dict:
    response = await client.post(
        f"/admin/dossiers/{dossier_id}/send",
        data={"sent_on": TODAY.isoformat(), **data},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict = response.json()["data"]
    return body


class TestTheDashboard:
    async def test_shows_the_next_action_and_its_due_date_without_opening(
        self, client, db_session, admin_storage
    ):
        """Acceptance criterion 1.5: next action, due date, "in N working days"
        counted server-side."""
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        await _ready(client, headers, dossier_id)
        await _send(client, headers, dossier_id)

        response = await client.get("/admin/inbox", headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        (waiting,) = body["groups"]["waiting"]
        due = WAL.add_working_days(TODAY, 20)  # the GRD's check, simple route
        assert waiting["id"] == dossier_id
        assert waiting["next_action"]["code"] == "record_response"
        assert waiting["next_action"]["labels"]["fr"] == "Réponse du GRD attendue"
        assert waiting["next_action_due"] == due.isoformat()
        assert waiting["due_in_working_days"] == 20
        assert waiting["operation_name"] == "Op Centre"
        assert body["banner"][0]["code"] == "grd_control"
        assert body["banner"][0]["due_in_working_days"] == 20

    async def test_groups_drafts_under_todo_with_their_progress(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        await _file(world, "operation.meter_removed", EAN[0])

        body = (await client.get("/admin/inbox?bucket=todo", headers=headers)).json()["data"]

        (draft,) = body["groups"]["todo"]
        assert (draft["code"], draft["step"], draft["variant"]) == (
            "PARTAGE_MODIF",
            "draft",
            "simple",
        )
        assert (draft["changes_count"], draft["pieces_ready"], draft["pieces_required"]) == (
            1,
            0,
            2,
        )
        assert body["groups"]["waiting"] == []

    async def test_is_for_managers_only(self, client, db_session):
        world = await _world(db_session)
        response = await client.get(
            "/admin/inbox", headers=await _headers(db_session, world, "MEMBER")
        )
        assert response.status_code == 403

    async def test_status_and_catalogue(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        status = (await client.get("/admin/status", headers=headers)).json()["data"]
        assert (status["active"], status["supported"], status["regulator"]) == (
            True,
            True,
            "BE-WAL-CWAPE",
        )
        assert status["baseline"] == "confirmed"  # nothing to confirm in an empty community
        definitions = (await client.get("/admin/procedure-definitions", headers=headers)).json()
        assert len(definitions["data"]) == 7

    async def test_the_agenda_lists_open_deadlines(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        await _ready(client, headers, dossier_id)
        await _send(client, headers, dossier_id)
        rows = (await client.get("/admin/deadlines", headers=headers)).json()["data"]
        assert [(r["code"], r["id_dossier"]) for r in rows] == [("grd_control", dossier_id)]


class TestTheFullList:
    async def test_keeps_what_the_dashboard_lets_go_newest_first(
        self, client, db_session, monkeypatch
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        modification = await _file(world, "operation.meter_removed", EAN[0])
        created = await client.post(
            "/admin/dossiers",
            json={"code": "PARTAGE_FIN", "id_sharing_operation": world.operation},
            headers=headers,
        )
        ended = created.json()["data"]["id"]
        cancelled = await client.post(
            f"/admin/dossiers/{ended}/transition",
            json={"to_step": "cancelled", "reason": "opened by mistake"},
            headers=headers,
        )
        assert cancelled.status_code == 200, cancelled.text
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=40)
        monkeypatch.setattr(ProcedureService, "_now", staticmethod(lambda: later))

        inbox = (await client.get("/admin/inbox", headers=headers)).json()["data"]
        listed = await client.get("/admin/dossiers", headers=headers)

        assert [r["id"] for r in inbox["groups"]["done"]] == []
        assert listed.status_code == 200, listed.text
        body = listed.json()
        # The starting state, confirmed at once in a community with no meter,
        # is a procedure too: the oldest.
        assert [(r["id"], r["code"], r["step"]) for r in body["data"]] == [
            (ended, "PARTAGE_FIN", "cancelled"),
            (modification, "PARTAGE_MODIF", "draft"),
            (world_baseline := body["data"][2]["id"], "ETAT_DE_DEPART", "in_force"),
        ]
        assert world_baseline < modification
        assert body["data"][1]["changes_count"] == 1
        assert body["pagination"] == {"page": 1, "limit": 20, "total": 3, "total_pages": 1}

    async def test_filters_and_pages(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        modification = await _file(world, "operation.meter_removed", EAN[0])
        created = await client.post(
            "/admin/dossiers",
            json={"code": "PARTAGE_FIN", "id_sharing_operation": world.operation},
            headers=headers,
        )
        ended = created.json()["data"]["id"]

        async def ids(query: str) -> list[int]:
            response = await client.get(f"/admin/dossiers?{query}", headers=headers)
            assert response.status_code == 200, response.text
            return [r["id"] for r in response.json()["data"]]

        assert await ids("code=PARTAGE_MODIF") == [modification]
        assert await ids("status=1") == [ended, modification]
        assert await ids("status=2") == []
        assert await ids(f"operation_id={world.operation}") == [ended, modification]
        assert await ids("operation_id=999999") == []
        second = await client.get("/admin/dossiers?limit=1&page=2", headers=headers)
        assert [r["id"] for r in second.json()["data"]] == [modification]
        assert second.json()["pagination"]["total_pages"] == 3  # with the starting state

    async def test_is_for_managers_only(self, client, db_session):
        world = await _world(db_session)
        response = await client.get(
            "/admin/dossiers", headers=await _headers(db_session, world, "MEMBER")
        )
        assert response.status_code == 403


class TestTheDetail:
    async def test_tells_everything_about_one_procedure(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0], grd="RESA")

        detail = await _get(client, headers, dossier_id)

        assert [c["subject_ref"] for c in detail["changes"]] == [EAN[0]]
        assert [s["code"] for s in detail["timeline"] if s["current"]] == ["draft"]
        assert "in_review" not in [s["code"] for s in detail["timeline"]]  # authorization only
        assert {t["to_step"] for t in detail["transitions"]} == {"ready", "cancelled"}
        assert detail["send_preview"]["reference"] == f"[OPT-{dossier_id}]"
        assert detail["send_preview"]["subjects"]["fr"].startswith(f"[OPT-{dossier_id}] ")
        assert detail["grd_ref"] == "RESA"
        assert detail["thread"][0]["kind"] == "step"

    async def test_of_another_community_is_not_found(self, client, db_session):
        world = await _world(db_session)
        other = await make_world(db_session, plugin=CwapePlugin())
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        response = await client.get(
            f"/admin/dossiers/{dossier_id}", headers=await _headers(db_session, other)
        )
        assert response.status_code == 404


class TestCreatingByHand:
    async def test_an_end_of_sharing(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        body = {"code": "PARTAGE_FIN", "id_sharing_operation": world.operation}

        created = await client.post("/admin/dossiers", json=body, headers=headers)
        again = await client.post("/admin/dossiers", json=body, headers=headers)

        assert created.status_code == 200, created.text
        detail = created.json()["data"]
        assert (detail["code"], detail["step"], detail["opened_by"]) == ("PARTAGE_FIN", "draft", 2)
        assert [p["code"] for p in detail["pieces"]] == ["sharing_termination"]
        assert again.status_code == 409
        assert again.json()["error_code"] == 2377

    @pytest.mark.parametrize("code", ["NOPE", "ETAT_DE_DEPART"])
    async def test_only_a_filing_type_of_the_catalogue(self, client, db_session, code):
        world = await _world(db_session)
        response = await client.post(
            "/admin/dossiers",
            json={"code": code, "id_sharing_operation": world.operation},
            headers=await _headers(db_session, world),
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == 2376

    async def test_on_an_operation_of_this_community_only(self, client, db_session):
        world = await _world(db_session)
        other = await make_world(db_session, plugin=CwapePlugin())
        response = await client.post(
            "/admin/dossiers",
            json={"code": "PARTAGE_FIN", "id_sharing_operation": other.operation},
            headers=await _headers(db_session, world),
        )
        assert response.status_code == 404


class TestChanges:
    async def test_excluding_the_only_change_cancels_the_system_draft(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        change = (await _get(client, headers, dossier_id))["changes"][0]

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/changes/{change['id']}/exclude",
            json={"reason": "a correction, not a removal"},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert detail["step"] == "cancelled"
        assert detail["changes"][0]["excluded_reason"] == "a correction, not a removal"

    async def test_a_deferred_change_joins_the_next_draft_once_this_one_is_sent(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        await _file(world, "operation.meter_removed", EAN[1])
        detail = await _get(client, headers, dossier_id)
        later = next(c for c in detail["changes"] if c["subject_ref"] == EAN[1])

        deferred = await client.post(
            f"/admin/dossiers/{dossier_id}/changes/{later['id']}/defer", headers=headers
        )
        assert deferred.status_code == 200, deferred.text
        await _ready(client, headers, dossier_id)
        await _send(client, headers, dossier_id)

        todo = (await client.get("/admin/inbox", headers=headers)).json()["data"]["groups"]["todo"]
        (next_draft,) = todo
        assert next_draft["id"] != dossier_id
        assert [
            c["subject_ref"] for c in (await _get(client, headers, next_draft["id"]))["changes"]
        ] == [EAN[1]]


class TestAfterSending:
    async def test_the_procedure_is_frozen(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        await _ready(client, headers, dossier_id)
        detail = await _send(client, headers, dossier_id)
        change = detail["changes"][0]
        form = _piece(detail, "sharing_modification")

        excluded = await client.post(
            f"/admin/dossiers/{dossier_id}/changes/{change['id']}/exclude",
            json={"reason": "too late"},
            headers=headers,
        )
        uploaded = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{form['id']}/versions",
            files={"file": ("v2.pdf", b"%PDF-1.4 v2", "application/pdf")},
            headers=headers,
        )

        assert (excluded.status_code, excluded.json()["error_code"]) == (409, 2378)
        assert (uploaded.status_code, uploaded.json()["error_code"]) == (409, 2378)

    async def test_a_received_piece_still_arrives(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        await _ready(client, headers, dossier_id)
        detail = await _send(client, headers, dossier_id)
        amendment = _piece(detail, "dso_amendment")

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{amendment['id']}/versions",
            files={"file": ("avenant.pdf", b"%PDF-1.4 avenant", "application/pdf")},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert _piece(response.json()["data"], "dso_amendment")["status"] == 2  # READY

    async def test_sending_records_the_message_and_freezes(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        ready = await _ready(client, headers, dossier_id)
        assert ready["step"] == "ready"  # the two required pieces made it ready

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/send",
            data={"sent_on": TODAY.isoformat(), "subject": f"[OPT-{dossier_id}] envoi"},
            files={"file": ("sent.eml", b"From: cer@example.org\n\nci-joint", "message/rfc822")},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert (detail["step"], detail["status"]) == ("sent", 2)
        assert detail["frozen_at"] is not None
        (message,) = [e for e in detail["thread"] if e["kind"] == "correspondence"]
        assert (message["type"], message["direction"], message["has_file"]) == ("sent", 1, True)
        download = await client.get(
            f"/admin/dossiers/{dossier_id}/correspondence/{message['id_correspondence']}/file",
            headers=headers,
        )
        assert download.status_code == 200
        assert download.content == b"From: cer@example.org\n\nci-joint"

    async def test_a_draft_cannot_be_sent(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        response = await client.post(f"/admin/dossiers/{dossier_id}/send", data={}, headers=headers)
        assert (response.status_code, response.json()["error_code"]) == (409, 2379)


async def _sent(client, db_session, admin_storage) -> tuple[object, dict, int]:
    world = await _world(db_session)
    headers = await _headers(db_session, world)
    dossier_id = await _file(world, "operation.meter_removed", EAN[0])
    await _ready(client, headers, dossier_id)
    await _send(client, headers, dossier_id)
    return world, headers, dossier_id


class TestResponses:
    async def test_a_dry_run_says_what_would_happen_and_writes_nothing(
        self, client, db_session, admin_storage
    ):
        _world_, headers, dossier_id = await _sent(client, db_session, admin_storage)
        before = (
            await db_session.scalar(select(func.count()).select_from(StatusEventModel)),
            await db_session.scalar(select(func.count()).select_from(DeadlineModel)),
        )
        on = TODAY + datetime.timedelta(days=7)

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/correspondence?dry_run=true",
            data={"type": "amendment_proposal", "occurred_on": on.isoformat()},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        proposal = response.json()["data"]
        assert (proposal["from_step"], proposal["to_step"], proposal["transition_allowed"]) == (
            "sent",
            "accepted",
            True,
        )
        assert proposal["deadlines_met"] == ["grd_control"]
        (started,) = proposal["deadlines_started"]
        assert (started["code"], started["due_date"]) == (
            "amendment_return",
            WAL.add_working_days(on, 10).isoformat(),
        )
        after = (
            await db_session.scalar(select(func.count()).select_from(StatusEventModel)),
            await db_session.scalar(select(func.count()).select_from(DeadlineModel)),
        )
        assert after == before

    async def test_recording_it_does_it(self, client, db_session, admin_storage):
        _world_, headers, dossier_id = await _sent(client, db_session, admin_storage)
        response = await client.post(
            f"/admin/dossiers/{dossier_id}/correspondence",
            data={
                "type": "amendment_proposal",
                "occurred_on": TODAY.isoformat(),
                "external_ref": "ORES-2026-0099",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert (detail["step"], detail["external_ref"]) == ("accepted", "ORES-2026-0099")
        by_code = {d["code"]: d["status"] for d in detail["deadlines"]}
        assert by_code == {"grd_control": 2, "amendment_return": 1}  # met, open
        assert detail["next_action"]["labels"]["fr"] == "Renvoyer l'avenant signé"

    async def test_a_response_that_does_not_fit_now_is_refused(
        self, client, db_session, admin_storage
    ):
        _world_, headers, dossier_id = await _sent(client, db_session, admin_storage)
        not_now = await client.post(
            f"/admin/dossiers/{dossier_id}/correspondence",
            data={"type": "amendment_signed"},
            headers=headers,
        )
        unknown = await client.post(
            f"/admin/dossiers/{dossier_id}/correspondence",
            data={"type": "telegram"},
            headers=headers,
        )
        assert (not_now.status_code, not_now.json()["error_code"]) == (409, 2379)
        assert (unknown.status_code, unknown.json()["error_code"]) == (422, 2380)


class TestManualTransitions:
    async def test_along_an_edge_with_its_reason(self, client, db_session, admin_storage):
        _world_, headers, dossier_id = await _sent(client, db_session, admin_storage)
        response = await client.post(
            f"/admin/dossiers/{dossier_id}/transition",
            json={"to_step": "refused", "reason": "refused by phone, letter to follow"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert detail["step"] == "refused"
        assert detail["thread"][-1]["reason"] == "refused by phone, letter to follow"

    async def test_off_the_catalogue_is_refused(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        response = await client.post(
            f"/admin/dossiers/{dossier_id}/transition",
            json={"to_step": "in_force", "reason": "skip it all"},
            headers=headers,
        )
        assert (response.status_code, response.json()["error_code"]) == (409, 2379)


class TestJulyGeneration:
    async def test_a_sent_procedures_piece_cannot_be_regenerated(
        self, client, db_session, admin_storage
    ):
        _world_, headers, dossier_id = await _sent(client, db_session, admin_storage)
        form = _piece(await _get(client, headers, dossier_id), "sharing_modification")
        response = await client.post(
            f"/documents/{form['id']}/generate", json={"data": {}}, headers=headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == 2360  # GENERATION_NOT_ALLOWED


async def test_an_operation_of_another_community_has_no_deadline_here(client, db_session):
    world = await _world(db_session)
    await create_sharing_operation(db_session, id_community=world.id_community, name="Op Sud")
    rows = (await client.get("/admin/deadlines", headers=await _headers(db_session, world))).json()[
        "data"
    ]
    assert rows == []
