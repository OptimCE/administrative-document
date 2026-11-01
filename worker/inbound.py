"""The reception address: what the worker does with each mail (phase 4).

Every ``INBOUND_POLL_SECONDS`` the worker reads the mailbox's unseen mails
(``ports/inbound_mailbox.py``) and, for each (criterion 4.2: a forwarded email
tagged ``[OPT-<id>]`` is attached to the right procedure, attachments
included):

1. **Read it** (``domain/inbound_mail.py``). Bytes that are no mail, or a mail
   over ``INBOUND_MAX_BYTES``, are dropped and logged.
2. **Find its community** from the code in the address it reached. An unknown
   or revoked code, or a community whose module is off, drops it (spec,
   désactivation: nothing is processed).
3. **Once only**: the same bytes for the same community are stored once (a
   crash before the mail was marked seen reads it again).
4. **Store** the raw ``.eml`` and each attachment, content-addressed.
5. **Find its procedure**: the first ``[OPT-<id>]`` naming one of the
   community's procedures, else a procedure's ``external_ref`` (the CWaPE's
   PA00xx, the GRD's) the mail mentions. None: the mail waits as unmatched,
   for a manager to attach or discard.
6. **Suggest** a response type (the plugin's keywords), kept only when the
   procedure's catalogue offers it from its current step.
7. **Tell** the managers in-app (``admin_correspondence.received``), and hint
   the procedure's screens.

Nothing moves a procedure: anyone can send an email, so a received mail is
evidence a manager qualifies (« Ajouter une réponse »), never an instruction.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import enum
import hashlib
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from core.notifications import (
    MANAGER_ROLES,
    Channel,
    CommunityTarget,
    NotificationCategory,
    NotificationService,
    NotificationTypes,
)
from core.realtime import CommunityAudience, Tier, emit
from core.storage import build_s3_uri, upload
from domain.inbound_mail import (
    InboundMailError,
    ParsedMail,
    address_pattern,
    parse,
    references,
    tokens,
)
from engine.contracts import PluginRegistry, plugin_for
from engine.jobs import FINISHED
from ports.crm_engine import SqlEngineCrmRead
from ports.inbound_mailbox import ImapMailbox, InboundMailbox
from shared.const import STORAGE_KEY_PREFIX, InboundMailStatus
from shared.models.local_models import (
    DossierModel,
    InboundAddressModel,
    InboundMailAttachmentModel,
    InboundMailModel,
    ProcedureDefinitionModel,
)
from worker.context import with_tenant

logger = logging.getLogger(__name__)


class Outcome(enum.StrEnum):
    ATTACHED = "attached"
    UNMATCHED = "unmatched"
    DUPLICATE = "duplicate"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    UNKNOWN_ADDRESS = "unknown_address"
    MODULE_OFF = "module_off"


@dataclass(frozen=True)
class Received:
    outcome: Outcome
    id_community: int | None = None
    id_inbound_mail: int | None = None
    id_dossier: int | None = None


def enabled() -> bool:
    return bool(settings.INBOUND_ADDRESS_TEMPLATE and settings.INBOUND_IMAP_HOST)


def mailbox_from_settings() -> InboundMailbox:
    return ImapMailbox(
        host=settings.INBOUND_IMAP_HOST,
        port=settings.INBOUND_IMAP_PORT,
        user=settings.INBOUND_IMAP_USER,
        password=settings.INBOUND_IMAP_PASSWORD,
        folder=settings.INBOUND_IMAP_FOLDER,
        ssl=settings.INBOUND_IMAP_SSL,
    )


async def receive(
    local: AsyncSession,
    crm: AsyncSession,
    raw: bytes,
    *,
    registry: PluginRegistry,
    now: datetime.datetime,
) -> Received:
    """File one mail. Commits each session itself; raises only on a failure
    worth retrying (storage, database), so the caller leaves the mail unseen."""
    if len(raw) > settings.INBOUND_MAX_BYTES:
        logger.warning("inbound mail dropped: %d bytes is over the limit", len(raw))
        return Received(Outcome.TOO_LARGE)
    try:
        mail = parse(raw)
    except InboundMailError as exc:
        logger.warning("inbound mail dropped: %s", exc)
        return Received(Outcome.UNREADABLE)

    address = await _address_of(local, mail)
    if address is None:
        logger.info("inbound mail dropped: no live reception address in its recipients")
        return Received(Outcome.UNKNOWN_ADDRESS)
    id_community = address.id_community
    state = await SqlEngineCrmRead(crm).module_state(id_community)
    await crm.commit()  # its read
    plugin = plugin_for(registry, state.regulator) if state.active else None
    if not state.active:
        return Received(Outcome.MODULE_OFF, id_community)

    digest = hashlib.sha256(raw).hexdigest()
    already = await local.scalar(
        select(InboundMailModel.id).where(
            InboundMailModel.id_community == id_community,
            InboundMailModel.content_sha256 == digest,
        )
    )
    if already is not None:
        return Received(Outcome.DUPLICATE, id_community, int(already))

    with with_tenant(id_community):
        base = f"{STORAGE_KEY_PREFIX}/{id_community}/inbound/{digest}"
        file_ref = await _store(f"{base}/message.eml", raw, "message/rfc822")
        dossier = await _procedure_of(local, id_community, mail)
        row = InboundMailModel(
            id_community=id_community,
            id_inbound_address=address.id,
            message_id=mail.message_id,
            content_sha256=digest,
            file_ref=file_ref,
            byte_size=len(raw),
            from_address=mail.from_address,
            subject=mail.subject or None,
            sent_at=mail.sent_at,
            body_text=mail.body_text or None,
            received_at=now,
            status=int(InboundMailStatus.ATTACHED if dossier else InboundMailStatus.UNMATCHED),
            id_dossier=dossier[0].id if dossier else None,
            match_reason=dossier[1] if dossier else None,
            suggested_response=(
                await _suggestion(local, plugin, dossier[0], mail) if dossier and plugin else None
            ),
        )
        local.add(row)
        await local.flush()
        for position, attachment in enumerate(mail.attachments, start=1):
            attachment_digest = hashlib.sha256(attachment.content).hexdigest()
            local.add(
                InboundMailAttachmentModel(
                    id_inbound_mail=row.id,
                    id_community=id_community,
                    position=position,
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    byte_size=len(attachment.content),
                    content_sha256=attachment_digest,
                    file_ref=await _store(
                        f"{base}/{position}-{attachment_digest}",
                        attachment.content,
                        attachment.content_type,
                    ),
                )
            )
        received = Received(
            Outcome.ATTACHED if dossier else Outcome.UNMATCHED,
            id_community,
            row.id,
            row.id_dossier,
        )
        subject = (mail.subject or "")[:120]
        await local.commit()

        notify = NotificationService(crm)
        await notify.publish(
            type=NotificationTypes.ADMIN_CORRESPONDENCE_RECEIVED,
            target=CommunityTarget(community_id=id_community, roles=MANAGER_ROLES),
            category=NotificationCategory.INFORMATIONAL,
            channels=(Channel.INAPP,),
            data={
                "inbound_mail_id": received.id_inbound_mail,
                "dossier_id": received.id_dossier,
                "subject": subject,
                "from": mail.from_address,
            },
        )
        await crm.commit()
        await notify.flush_realtime()
        if received.id_dossier is not None:
            await emit(
                topic="admin.dossier.updated",
                audience=CommunityAudience(id_community, Tier.MANAGER),
                resource=("dossier", received.id_dossier),
                scope_community_id=id_community,
            )
    return received


async def _address_of(local: AsyncSession, mail: ParsedMail) -> InboundAddressModel | None:
    codes = tokens(mail, address_pattern(settings.INBOUND_ADDRESS_TEMPLATE))
    if not codes:
        return None
    address: InboundAddressModel | None = await local.scalar(
        select(InboundAddressModel)
        .where(InboundAddressModel.token.in_(codes), InboundAddressModel.revoked_at.is_(None))
        .order_by(InboundAddressModel.id)
        .limit(1)
    )
    return address


async def _procedure_of(
    local: AsyncSession, id_community: int, mail: ParsedMail
) -> tuple[DossierModel, str] | None:
    """The community's procedure the mail names: by ``[OPT-<id>]`` first (never
    another community's: the id is global), then by an external reference."""
    for reference in references(mail):
        dossier = await local.scalar(
            select(DossierModel).where(
                DossierModel.id == reference,
                DossierModel.id_community == id_community,
                DossierModel.id_procedure_definition.is_not(None),
            )
        )
        if dossier is not None:
            return dossier, "reference"
    text = f"{mail.subject}\n{mail.body_text}"
    for dossier in await local.scalars(
        select(DossierModel)
        .where(
            DossierModel.id_community == id_community,
            DossierModel.external_ref.is_not(None),
            DossierModel.id_procedure_definition.is_not(None),
        )
        .order_by(DossierModel.status.in_(FINISHED), DossierModel.id.desc())
    ):
        ref = (dossier.external_ref or "").strip()
        if len(ref) >= 4 and re.search(rf"(?<![\w-]){re.escape(ref)}(?![\w-])", text, re.I):
            return dossier, "external_ref"
    return None


async def _suggestion(
    local: AsyncSession, plugin, dossier: DossierModel, mail: ParsedMail
) -> str | None:
    """The plugin's guess, if the procedure's own catalogue version offers it
    from its current step."""
    code = plugin.suggest_response(mail.subject, mail.body_text)
    if code is None:
        return None
    ref = (
        await local.execute(
            select(ProcedureDefinitionModel.code, ProcedureDefinitionModel.version).where(
                ProcedureDefinitionModel.id == dossier.id_procedure_definition
            )
        )
    ).first()
    entry = plugin.catalogue().get(ref[0], ref[1]) if ref else None
    if entry is None:
        return None
    definition = entry.definition
    try:
        response = definition.response_type(code)
    except KeyError:
        return None
    if response.variants is not None and dossier.variant not in response.variants:
        return None
    target = response.to_step
    step = str(dossier.step)
    if target and target != step and not definition.allows(step, target, dossier.variant):
        return None
    return str(code)


async def _store(key: str, content: bytes, content_type: str | None) -> str:
    await upload(key, content, content_type=content_type, bucket=settings.OUTPUT_BUCKET)
    return build_s3_uri(settings.OUTPUT_BUCKET, key)


@dataclass
class PollReport:
    read: int = 0
    attached: int = 0
    unmatched: int = 0
    dropped: int = 0
    failed: int = 0


async def poll_inbound(
    mailbox: InboundMailbox,
    *,
    registry: PluginRegistry,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
    now: datetime.datetime | None = None,
) -> PollReport:
    """One pass over the mailbox. A mail that failed for a reason worth retrying
    stays unseen for the next pass; every other is marked seen."""
    report = PollReport()
    own_local, own_crm = local_session is None, crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    try:
        for raw in await mailbox.fetch(settings.INBOUND_POLL_BATCH):
            report.read += 1
            try:
                received = await receive(
                    local,
                    crm,
                    raw.content,
                    registry=registry,
                    now=now or datetime.datetime.now(datetime.UTC),
                )
            except Exception:
                logger.exception("inbound mail %s not filed; retried next pass", raw.uid)
                await local.rollback()
                await crm.rollback()
                report.failed += 1
                continue
            if received.outcome == Outcome.ATTACHED:
                report.attached += 1
            elif received.outcome == Outcome.UNMATCHED:
                report.unmatched += 1
            elif received.outcome != Outcome.DUPLICATE:
                report.dropped += 1
            await mailbox.done(raw.uid)
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()
    if report.read:
        logger.info("inbound mailbox: %s", report, extra={"operation": "worker:inbound"})
    return report


async def run_inbound_poller(
    shutdown: asyncio.Event,
    *,
    registry: PluginRegistry,
    mailbox: InboundMailbox | None = None,
) -> None:
    """Poll the mailbox every ``INBOUND_POLL_SECONDS`` until shutdown. Off when
    no address template or host is configured."""
    if mailbox is None and not enabled():
        logger.info("reception address off: no INBOUND_ADDRESS_TEMPLATE or INBOUND_IMAP_HOST")
        return
    box = mailbox or mailbox_from_settings()
    while not shutdown.is_set():
        try:
            await poll_inbound(box, registry=registry)
        except Exception:
            # The mailbox down, a login refused: the next pass retries.
            logger.exception("inbound mailbox poll failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=settings.INBOUND_POLL_SECONDS)
