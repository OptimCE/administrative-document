"""The reception address, on the manager's side (phase 4).

The worker files what arrives (``worker/inbound.py``); this is what a manager
does with it: read the community's address (and rotate its code, an admin),
see what was received, attach a mail no reference placed, discard one, and
download a mail or an attachment. Qualifying a mail (« Ajouter une réponse »
from it) and using an attachment as a piece go through ``ProcedureService``,
which owns those writes.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
import string
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors.errors import ErrorException
from core.storage import download, parse_s3_uri
from shared.const import InboundMailStatus
from shared.custom_errors import errors
from shared.models.local_models import (
    DossierModel,
    InboundAddressModel,
    InboundMailAttachmentModel,
    InboundMailModel,
)

_ALPHABET = string.ascii_lowercase + string.digits
_TOKEN_LENGTH = 12
_OPEN = (int(InboundMailStatus.UNMATCHED), int(InboundMailStatus.ATTACHED))
_LIST_LIMIT = 200


def enabled() -> bool:
    return bool(settings.INBOUND_ADDRESS_TEMPLATE and settings.INBOUND_IMAP_HOST)


def address_for(token: str) -> str:
    return settings.INBOUND_ADDRESS_TEMPLATE.replace("{token}", token)


def new_token() -> str:
    """12 characters of [a-z0-9]: about 62 bits, not guessable, and safe in
    every address syntax a provider accepts."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_LENGTH))


class InboundService:
    def __init__(self, *, local_session: AsyncSession) -> None:
        self._local = local_session

    # -- the address ------------------------------------------------------------

    async def address(self, id_community: int, *, actor: str) -> dict[str, Any]:
        """The community's address, its code made on first sight."""
        if not enabled():
            return {"enabled": False, "address": None}
        live = await self._live(id_community)
        if live is None:
            live = await self._issue(id_community, actor=actor)
        return {"enabled": True, "address": address_for(live.token)}

    async def rotate(self, id_community: int, *, actor: str) -> dict[str, Any]:
        """A new code; the old one reaches nobody from now on."""
        if not enabled():
            raise ErrorException(errors.admin.INBOUND_DISABLED, status_code=409)
        live = await self._live(id_community)
        if live is not None:
            live.revoked_at = datetime.datetime.now(datetime.UTC)
            await self._local.flush()
        fresh = await self._issue(id_community, actor=actor)
        return {"enabled": True, "address": address_for(fresh.token)}

    async def _live(self, id_community: int) -> InboundAddressModel | None:
        live: InboundAddressModel | None = await self._local.scalar(
            select(InboundAddressModel).where(
                InboundAddressModel.id_community == id_community,
                InboundAddressModel.revoked_at.is_(None),
            )
        )
        return live

    async def _issue(self, id_community: int, *, actor: str) -> InboundAddressModel:
        row = InboundAddressModel(id_community=id_community, token=new_token(), created_by=actor)
        self._local.add(row)
        await self._local.commit()
        return row

    # -- what was received ---------------------------------------------------------

    async def mails(
        self, id_community: int, *, status: str | None = None, dossier_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Newest first. ``status``: ``open`` (unmatched or to qualify, the
        default), ``unmatched``, ``attached``, ``recorded``, ``discarded``, ``all``."""
        stmt = select(InboundMailModel).where(InboundMailModel.id_community == id_community)
        statuses = _statuses(status)
        if statuses is not None:
            stmt = stmt.where(InboundMailModel.status.in_(statuses))
        if dossier_id is not None:
            stmt = stmt.where(InboundMailModel.id_dossier == dossier_id)
        rows = list(
            await self._local.scalars(
                stmt.order_by(
                    InboundMailModel.received_at.desc(), InboundMailModel.id.desc()
                ).limit(_LIST_LIMIT)
            )
        )
        return await mails_out(self._local, rows)

    async def attach(
        self, id_community: int, mail_id: int, dossier_id: int, *, actor: str
    ) -> dict[str, Any]:
        mail = await self._open_mail(id_community, mail_id)
        dossier = await self._local.scalar(
            select(DossierModel.id).where(
                DossierModel.id == dossier_id,
                DossierModel.id_community == id_community,
                DossierModel.id_procedure_definition.is_not(None),
            )
        )
        if dossier is None:
            raise ErrorException(errors.admin.DOSSIER_NOT_FOUND, status_code=404)
        mail.id_dossier = dossier_id
        mail.status = int(InboundMailStatus.ATTACHED)
        mail.match_reason = "manual"
        mail.handled_by = actor
        mail.handled_at = datetime.datetime.now(datetime.UTC)
        await self._local.commit()
        return (await mails_out(self._local, [mail]))[0]

    async def discard(self, id_community: int, mail_id: int, *, actor: str) -> dict[str, Any]:
        mail = await self._open_mail(id_community, mail_id)
        mail.status = int(InboundMailStatus.DISCARDED)
        mail.handled_by = actor
        mail.handled_at = datetime.datetime.now(datetime.UTC)
        await self._local.commit()
        return (await mails_out(self._local, [mail]))[0]

    async def _open_mail(self, id_community: int, mail_id: int) -> InboundMailModel:
        mail = await find_mail(self._local, id_community, mail_id)
        if mail.status not in _OPEN:
            raise ErrorException(errors.admin.INBOUND_MAIL_HANDLED, status_code=409)
        return mail

    # -- the files ------------------------------------------------------------------

    async def mail_file(self, id_community: int, mail_id: int) -> tuple[InboundMailModel, bytes]:
        mail = await find_mail(self._local, id_community, mail_id)
        return mail, await stored(mail.file_ref, mail.content_sha256)

    async def attachment_file(
        self, id_community: int, mail_id: int, attachment_id: int
    ) -> tuple[InboundMailAttachmentModel, bytes]:
        attachment = await find_attachment(self._local, id_community, mail_id, attachment_id)
        return attachment, await stored(attachment.file_ref, attachment.content_sha256)


def _statuses(status: str | None) -> tuple[int, ...] | None:
    named = {
        None: _OPEN,
        "open": _OPEN,
        "unmatched": (int(InboundMailStatus.UNMATCHED),),
        "attached": (int(InboundMailStatus.ATTACHED),),
        "recorded": (int(InboundMailStatus.RECORDED),),
        "discarded": (int(InboundMailStatus.DISCARDED),),
        "all": None,
    }
    if status not in named:
        raise ErrorException(errors.admin.INBOUND_MAIL_NOT_FOUND, status_code=422)
    return named[status]


async def find_mail(local: AsyncSession, id_community: int, mail_id: int) -> InboundMailModel:
    mail = await local.scalar(
        select(InboundMailModel).where(
            InboundMailModel.id == mail_id, InboundMailModel.id_community == id_community
        )
    )
    if mail is None:
        raise ErrorException(errors.admin.INBOUND_MAIL_NOT_FOUND, status_code=404)
    return mail


async def find_attachment(
    local: AsyncSession, id_community: int, mail_id: int, attachment_id: int
) -> InboundMailAttachmentModel:
    attachment = await local.scalar(
        select(InboundMailAttachmentModel).where(
            InboundMailAttachmentModel.id == attachment_id,
            InboundMailAttachmentModel.id_inbound_mail == mail_id,
            InboundMailAttachmentModel.id_community == id_community,
        )
    )
    if attachment is None:
        raise ErrorException(errors.admin.INBOUND_MAIL_NOT_FOUND, status_code=404)
    return attachment


async def stored(file_ref: str, sha256: str) -> bytes:
    """The bytes behind a received file, checked against what was recorded."""
    try:
        bucket, key = parse_s3_uri(file_ref)
        content = await download(key, bucket=bucket)
    except Exception as exc:
        raise ErrorException(errors.admin.STORAGE_DOWNLOAD_FAILED, status_code=502) from exc
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ErrorException(errors.admin.STORED_FILE_ALTERED, status_code=502)
    return content


async def mails_out(local: AsyncSession, rows: Sequence[InboundMailModel]) -> list[dict[str, Any]]:
    ids = [row.id for row in rows]
    attachments: dict[int, list[dict[str, Any]]] = {}
    if ids:
        for attachment in await local.scalars(
            select(InboundMailAttachmentModel)
            .where(InboundMailAttachmentModel.id_inbound_mail.in_(ids))
            .order_by(
                InboundMailAttachmentModel.id_inbound_mail, InboundMailAttachmentModel.position
            )
        ):
            attachments.setdefault(attachment.id_inbound_mail, []).append(
                {
                    "id": attachment.id,
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "byte_size": attachment.byte_size,
                    "content_sha256": attachment.content_sha256,
                }
            )
    return [
        {
            "id": row.id,
            "status": row.status,
            "from_address": row.from_address,
            "subject": row.subject,
            "sent_at": row.sent_at,
            "received_at": row.received_at,
            "body_text": row.body_text,
            "id_dossier": row.id_dossier,
            "match_reason": row.match_reason,
            "suggested_response": row.suggested_response,
            "id_correspondence": row.id_correspondence,
            "byte_size": row.byte_size,
            "attachments": attachments.get(row.id, []),
        }
        for row in rows
    ]
