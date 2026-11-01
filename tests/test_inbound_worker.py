"""The reception address, through the worker (``worker/inbound.py``).

Criterion 4.2: a forwarded email tagged ``[OPT-<id>]`` is attached to the right
procedure, attachments included. One Postgres serves both sessions; the
mailbox is in memory, the object store too (``fake_storage``).
"""

from __future__ import annotations

import datetime
import hashlib
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from core.config import settings
from engine.routing import route_event
from ports.crm_engine import SqlEngineCrmRead
from ports.inbound_mailbox import MemoryMailbox
from regulators.cwape.plugin import CwapePlugin
from shared.const import InboundMailStatus
from shared.models.local_models import (
    InboundAddressModel,
    InboundMailAttachmentModel,
    InboundMailModel,
)
from tests.engine.world import EAN, World, make_world, meter, set_subscription
from tests.notifications import notifications_of, seed_roster
from worker.inbound import Outcome, poll_inbound, receive

pytestmark = pytest.mark.asyncio

NOW = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=datetime.UTC)
TOKEN = "k7x2abcd9z01"
PDF = b"%PDF-1.4 avenant signe par le GRD"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "INBOUND_ADDRESS_TEMPLATE", "demarches+{token}@optimce.test")
    monkeypatch.setattr(settings, "INBOUND_IMAP_HOST", "imap.test")


async def _world(db_session, token: str = TOKEN) -> World:
    world = await make_world(db_session, plugin=CwapePlugin())
    db_session.add(InboundAddressModel(id_community=world.id_community, token=token))
    await db_session.flush()
    return world


async def _procedure(world: World, ean: str = EAN[0]) -> int:
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    result = await route_event(
        meter(world, "operation.meter_added", ean, at=when),
        local=world.session,
        crm=SqlEngineCrmRead(world.session),
        registry=world.registry,
    )
    return result.dossiers[0]


def _forwarded(subject: str, *, to: str = f"demarches+{TOKEN}@optimce.test", n: int = 2) -> bytes:
    message = EmailMessage()
    message["From"] = "gestion@cer-exemple.be"
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = "Mon, 02 Nov 2026 09:15:00 +0100"
    message.set_content("---------- Message transféré ----------\nVoici l'avenant signé.")
    message.add_attachment(PDF, maintype="application", subtype="pdf", filename="avenant.pdf")
    if n >= 2:
        message.add_attachment(
            b"EAN;membre\n541448800000000001;Mia\n",
            maintype="text",
            subtype="csv",
            filename="annexe.csv",
        )
    return message.as_bytes()


async def _poll(world: World, mailbox: MemoryMailbox):
    return await poll_inbound(
        mailbox,
        registry=world.registry,
        local_session=world.session,
        crm_session=world.session,
        now=NOW,
    )


async def _mails(world: World) -> list[InboundMailModel]:
    return list(
        await world.session.scalars(
            select(InboundMailModel)
            .where(InboundMailModel.id_community == world.id_community)
            .order_by(InboundMailModel.id)
        )
    )


class TestCriterion42:
    async def test_a_forwarded_mail_is_attached_to_its_procedure_attachments_included(
        self, db_session, fake_storage
    ):
        world = await _world(db_session)
        await seed_roster(db_session, SimpleNamespace(id=world.id_community), {"boss": "MANAGER"})
        dossier_id = await _procedure(world)
        mailbox = MemoryMailbox()
        raw = _forwarded(f"TR: RE: [OPT-{dossier_id}] Modification du partage")
        uid = mailbox.deliver(raw)

        report = await _poll(world, mailbox)

        assert (report.read, report.attached) == (1, 1)
        (mail,) = await _mails(world)
        assert (mail.status, mail.id_dossier, mail.match_reason) == (
            int(InboundMailStatus.ATTACHED),
            dossier_id,
            "reference",
        )
        assert mail.content_sha256 == hashlib.sha256(raw).hexdigest()
        attachments = list(
            await db_session.scalars(
                select(InboundMailAttachmentModel)
                .where(InboundMailAttachmentModel.id_inbound_mail == mail.id)
                .order_by(InboundMailAttachmentModel.position)
            )
        )
        assert [(a.filename, a.content_sha256) for a in attachments] == [
            ("avenant.pdf", hashlib.sha256(PDF).hexdigest()),
            (
                "annexe.csv",
                hashlib.sha256(b"EAN;membre\n541448800000000001;Mia\n").hexdigest(),
            ),
        ]
        stored = set(fake_storage.values())
        assert raw in stored and PDF in stored
        assert uid in mailbox.seen
        (told,) = await notifications_of(db_session, "admin_correspondence.received")
        assert told.data["dossier_id"] == dossier_id

    async def test_another_communitys_reference_is_not_followed(self, db_session, fake_storage):
        world = await _world(db_session)
        elsewhere = await make_world(db_session, plugin=world.plugin)
        theirs = await _procedure(elsewhere, EAN[1])
        mailbox = MemoryMailbox()
        mailbox.deliver(_forwarded(f"RE: [OPT-{theirs}] Modification du partage"))

        await _poll(world, mailbox)

        (mail,) = await _mails(world)
        assert (mail.status, mail.id_dossier) == (int(InboundMailStatus.UNMATCHED), None)

    async def test_without_a_reference_it_waits_unmatched(self, db_session, fake_storage):
        world = await _world(db_session)
        await _procedure(world)
        mailbox = MemoryMailbox()
        mailbox.deliver(_forwarded("Votre demande", n=1))

        report = await _poll(world, mailbox)

        assert report.unmatched == 1
        (mail,) = await _mails(world)
        assert mail.status == int(InboundMailStatus.UNMATCHED)

    async def test_an_external_reference_finds_its_procedure(self, db_session, fake_storage):
        world = await _world(db_session)
        dossier_id = await _procedure(world)
        await db_session.execute(
            text("UPDATE dossier SET external_ref = 'PA0026' WHERE id = :id"), {"id": dossier_id}
        )
        mailbox = MemoryMailbox()
        mailbox.deliver(_forwarded("Décision CWaPE PA0026"))

        await _poll(world, mailbox)

        (mail,) = await _mails(world)
        assert (mail.id_dossier, mail.match_reason) == (dossier_id, "external_ref")


class TestOnceAndOnlyWhatIsOurs:
    async def test_the_same_mail_twice_is_stored_once(self, db_session, fake_storage):
        world = await _world(db_session)
        mailbox = MemoryMailbox()
        raw = _forwarded("Bonjour")
        mailbox.deliver(raw)
        mailbox.deliver(raw)

        await _poll(world, mailbox)

        assert len(await _mails(world)) == 1
        assert mailbox.seen == {"1", "2"}

    async def test_an_unknown_or_revoked_code_reaches_nobody(self, db_session, fake_storage):
        world = await _world(db_session)
        mailbox = MemoryMailbox()
        mailbox.deliver(_forwarded("x", to="demarches+zzzzzzzzzzzz@optimce.test"))
        await db_session.execute(text("UPDATE inbound_address SET revoked_at = now()"))
        mailbox.deliver(_forwarded("y"))

        report = await _poll(world, mailbox)

        assert (report.dropped, await _mails(world)) == (2, [])
        assert mailbox.seen == {"1", "2"}

    async def test_a_community_with_the_module_off_receives_nothing(self, db_session, fake_storage):
        world = await _world(db_session)
        await set_subscription(world, active=False, at=NOW)

        received = await receive(
            db_session, db_session, _forwarded("x"), registry=world.registry, now=NOW
        )

        assert received.outcome == Outcome.MODULE_OFF
        assert await _mails(world) == []

    async def test_too_large_or_unreadable_is_dropped(self, db_session, fake_storage, monkeypatch):
        world = await _world(db_session)
        monkeypatch.setattr(settings, "INBOUND_MAX_BYTES", 100)

        too_large = await receive(
            db_session, db_session, _forwarded("x"), registry=world.registry, now=NOW
        )
        unreadable = await receive(db_session, db_session, b" ", registry=world.registry, now=NOW)

        assert (too_large.outcome, unreadable.outcome) == (Outcome.TOO_LARGE, Outcome.UNREADABLE)

    async def test_a_storage_failure_leaves_the_mail_unseen_for_the_next_pass(
        self, db_session, monkeypatch
    ):
        from worker import inbound

        world = await _world(db_session)

        async def _down(*_a, **_k):
            raise ConnectionError("store down")

        monkeypatch.setattr(inbound, "upload", _down)
        mailbox = MemoryMailbox()
        mailbox.deliver(_forwarded("x"))

        report = await _poll(world, mailbox)

        assert report.failed == 1
        assert mailbox.seen == set()
