"""The reception address on the manager's side (phase 4): the address, what was
received, attach, discard, record a response from a mail, an attachment as a
piece, and the procedure's detail."""

from __future__ import annotations

import datetime
from email.message import EmailMessage

import pytest
from sqlalchemy import select, text

from core.config import settings
from engine.routing import route_event
from ports.crm_engine import SqlEngineCrmRead
from regulators.cwape.plugin import CwapePlugin
from shared.const import InboundMailStatus
from shared.models.local_models import (
    CorrespondenceModel,
    DocumentModel,
    DocumentVersionModel,
    InboundAddressModel,
    InboundMailModel,
)
from tests.conftest import gateway_headers
from tests.engine.world import EAN, make_world, meter
from worker.inbound import receive

pytestmark = pytest.mark.asyncio

NOW = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=datetime.UTC)
TOKEN = "k7x2abcd9z01"
PDF = b"%PDF-1.4 avenant"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "INBOUND_ADDRESS_TEMPLATE", "demarches+{token}@optimce.test")
    monkeypatch.setattr(settings, "INBOUND_IMAP_HOST", "imap.test")


async def _world(db_session):
    return await make_world(db_session, plugin=CwapePlugin())


async def _headers(db_session, world, role: str = "MANAGER") -> dict[str, str]:
    auth_id = await db_session.scalar(
        text("SELECT auth_community_id FROM community WHERE id = :id"), {"id": world.id_community}
    )
    return gateway_headers(str(auth_id), role=role)


async def _procedure(world) -> int:
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    result = await route_event(
        meter(world, "operation.meter_added", EAN[0], at=when),
        local=world.session,
        crm=SqlEngineCrmRead(world.session),
        registry=world.registry,
    )
    return result.dossiers[0]


async def _received(db_session, world, subject: str) -> InboundMailModel:
    if (
        await db_session.scalar(
            select(InboundAddressModel.id).where(
                InboundAddressModel.id_community == world.id_community
            )
        )
        is None
    ):
        db_session.add(InboundAddressModel(id_community=world.id_community, token=TOKEN))
        await db_session.flush()
    message = EmailMessage()
    message["From"] = "ores@ores.be"
    message["To"] = f"demarches+{TOKEN}@optimce.test"
    message["Subject"] = subject
    message["Date"] = "Mon, 02 Nov 2026 09:15:00 +0100"
    message.set_content("Votre dossier est complet.")
    message.add_attachment(PDF, maintype="application", subtype="pdf", filename="avenant.pdf")
    received = await receive(
        db_session, db_session, message.as_bytes(), registry=world.registry, now=NOW
    )
    mail: InboundMailModel | None = await db_session.get(InboundMailModel, received.id_inbound_mail)
    assert mail is not None
    await db_session.refresh(mail)
    return mail


class TestTheAddress:
    async def test_a_manager_reads_it_made_on_first_sight(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)

        first = (await client.get("/admin/inbound-address", headers=headers)).json()["data"]
        again = (await client.get("/admin/inbound-address", headers=headers)).json()["data"]

        assert first["enabled"] is True
        assert first["address"].startswith("demarches+") and first["address"] == again["address"]

    async def test_off_without_a_template(self, client, db_session, monkeypatch):
        monkeypatch.setattr(settings, "INBOUND_ADDRESS_TEMPLATE", "")
        world = await _world(db_session)

        response = await client.get(
            "/admin/inbound-address", headers=await _headers(db_session, world)
        )

        assert response.json()["data"] == {"enabled": False, "address": None}

    async def test_an_admin_rotates_it_a_manager_may_not(self, client, db_session):
        world = await _world(db_session)
        manager = await _headers(db_session, world)
        before = (await client.get("/admin/inbound-address", headers=manager)).json()["data"]

        refused = await client.post("/admin/inbound-address/rotate", headers=manager)
        rotated = await client.post(
            "/admin/inbound-address/rotate", headers=await _headers(db_session, world, "ADMIN")
        )

        assert refused.status_code == 403
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["data"]["address"] != before["address"]
        live = await db_session.scalar(
            text(
                "SELECT count(*) FROM inbound_address WHERE id_community = :c "
                "AND revoked_at IS NULL"
            ),
            {"c": world.id_community},
        )
        assert live == 1

    async def test_members_have_no_access(self, client, db_session):
        world = await _world(db_session)

        response = await client.get(
            "/admin/inbound-address", headers=await _headers(db_session, world, "MEMBER")
        )

        assert response.status_code == 403


class TestWhatWasReceived:
    async def test_lists_attaches_and_discards(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _procedure(world)
        unmatched = await _received(db_session, world, "Bonjour")
        other = await _received(db_session, world, "Publicité")

        listed = (await client.get("/admin/inbound-mail", headers=headers)).json()["data"]
        assert {m["id"] for m in listed} == {unmatched.id, other.id}
        assert listed[0]["attachments"][0]["filename"] == "avenant.pdf"

        attached = await client.post(
            f"/admin/inbound-mail/{unmatched.id}/attach",
            json={"id_dossier": dossier_id},
            headers=headers,
        )
        discarded = await client.post(f"/admin/inbound-mail/{other.id}/discard", headers=headers)

        assert attached.json()["data"]["status"] == int(InboundMailStatus.ATTACHED)
        assert attached.json()["data"]["match_reason"] == "manual"
        assert discarded.json()["data"]["status"] == int(InboundMailStatus.DISCARDED)
        again = await client.post(f"/admin/inbound-mail/{other.id}/discard", headers=headers)
        assert again.status_code == 409

    async def test_downloads_the_mail_and_its_attachment(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        mail = await _received(db_session, world, "Bonjour")
        listed = (await client.get("/admin/inbound-mail", headers=headers)).json()["data"]
        attachment_id = listed[0]["attachments"][0]["id"]

        eml = await client.get(f"/admin/inbound-mail/{mail.id}/file", headers=headers)
        pdf = await client.get(
            f"/admin/inbound-mail/{mail.id}/attachments/{attachment_id}/file", headers=headers
        )

        assert eml.status_code == 200 and b"Subject: Bonjour" in eml.content
        assert pdf.content == PDF

    async def test_another_communitys_mail_is_not_found(self, client, db_session, admin_storage):
        world = await _world(db_session)
        elsewhere = await make_world(db_session, plugin=world.plugin)
        theirs = await _received(db_session, elsewhere, "Bonjour")

        response = await client.post(
            f"/admin/inbound-mail/{theirs.id}/discard", headers=await _headers(db_session, world)
        )

        assert response.status_code == 404


class TestFromAMailToTheProcedure:
    async def test_the_detail_shows_it_and_a_response_records_it(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _procedure(world)
        mail = await _received(db_session, world, f"RE: [OPT-{dossier_id}] Modification")

        detail = (await client.get(f"/admin/dossiers/{dossier_id}", headers=headers)).json()["data"]
        assert [m["id"] for m in detail["inbound"]] == [mail.id]
        assert any(
            e["kind"] == "inbound" and e["id_inbound_mail"] == mail.id for e in detail["thread"]
        )

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/correspondence",
            data={"type": "other", "inbound_mail_id": str(mail.id)},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        message = await db_session.scalar(
            select(CorrespondenceModel).where(CorrespondenceModel.id_dossier == dossier_id)
        )
        assert message is not None
        assert (message.original_filename, message.content_type) == (
            f"message-{mail.id}.eml",
            "message/rfc822",
        )
        assert message.subject == f"RE: [OPT-{dossier_id}] Modification"
        await db_session.refresh(mail)
        assert (mail.status, mail.id_correspondence) == (
            int(InboundMailStatus.RECORDED),
            message.id,
        )
        assert response.json()["data"]["inbound"] == []

    async def test_an_attachment_becomes_a_version_of_a_piece(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _procedure(world)
        mail = await _received(db_session, world, f"RE: [OPT-{dossier_id}] Modification")
        attachment_id = (await client.get("/admin/inbound-mail", headers=headers)).json()["data"][
            0
        ]["attachments"][0]["id"]
        piece = await db_session.scalar(
            select(DocumentModel).where(DocumentModel.id_dossier == dossier_id).limit(1)
        )
        assert piece is not None

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{piece.id}/versions/from-inbound",
            json={"id_inbound_mail": mail.id, "id_attachment": attachment_id},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        version = await db_session.scalar(
            select(DocumentVersionModel).where(DocumentVersionModel.id_document == piece.id)
        )
        assert version is not None
        assert (version.original_filename, version.content_type) == (
            "avenant.pdf",
            "application/pdf",
        )
