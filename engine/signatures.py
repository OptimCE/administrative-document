"""A collected piece signed in the app, through the signature annexe (2026-11-03).

A participant's per-member collected piece (the sworn declaration) used to be
printed, signed by hand and uploaded by a manager. When its regulator says
where it is signed (``RegulatorPlugin.signature_layout``), a manager can ask
the signature annexe instead: the piece's TO_SIGN version goes out on
``signature.request.create``, the participant signs it in the app, and the
sealed PDF comes back as the piece's FINAL version, through the same
``supply_piece`` an upload goes through (the piece turns SIGNED).

The signature annexe keeps the evidence (the event log, the sealed report);
``piece_signature`` only follows where the request stands. Its events are
correlated by the idempotency key we chose, never by their other fields.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.contracts import PluginRegistry, SignatureLayout, plugin_for
from engine.filing import context_of_procedure
from engine.lifecycle import supply_piece
from shared.const import (
    LIVE_SIGNATURE_STATUSES,
    SIGNATURE_SOURCE,
    STORAGE_KEY_PREFIX,
    DocOrigin,
    DocumentKind,
    PieceSignatureStatus,
    ProcedureDocumentStatus,
    VersionStage,
)
from shared.models.local_models import (
    DocumentModel,
    DocumentVersionModel,
    DossierModel,
    PieceSignatureModel,
)

logger = logging.getLogger(__name__)

#: Who wrote the FINAL version a signature produced (``document_version.generated_by``).
SIGNATURE_ACTOR = "signature"
PDF = "application/pdf"


def signable(
    dossier: DossierModel,
    piece: DocumentModel,
    version: DocumentVersionModel | None,
    layout: SignatureLayout | None,
) -> bool:
    """Whether ``piece`` can be sent to sign now (account and module aside)."""
    return (
        layout is not None
        and dossier.frozen_at is None
        and piece.kind == int(DocumentKind.COLLECTED)
        and piece.id_member is not None
        and version is not None
        and version.stage == int(VersionStage.TO_SIGN)
    )


def idempotency_key(piece: DocumentModel, version: DocumentVersionModel, attempt: int) -> str:
    """One key per request: a declined or expired one is followed by a new attempt."""
    return f"admin-doc-{piece.id}-v{version.version_no}-{attempt}"


def document_key(file_ref: str) -> str:
    """The bucket key of a version (``file_ref`` is ``s3://bucket/key``)."""
    remainder = file_ref.removeprefix("s3://")
    _bucket, _, key = remainder.partition("/")
    return key


def request_payload(
    *,
    dossier: DossierModel,
    piece: DocumentModel,
    version: DocumentVersionModel,
    layout: SignatureLayout,
    key: str,
    title: str,
    locale: str,
    requested_by: str | None,
    role_label: str,
) -> dict[str, Any]:
    """The ``signature.request.create`` body (the signature annexe's contract)."""
    return {
        "idempotency_key": key,
        "source_service": SIGNATURE_SOURCE,
        "source_ref": f"document:{piece.id}:v{version.version_no}",
        "community_id": dossier.id_community,
        "title": title[:255],
        "locale": locale if locale in ("fr", "en", "nl", "de") else "fr",
        "document_key": document_key(version.file_ref),
        "signing_order": "SEQUENTIAL",
        "requested_by": requested_by,
        "signers": [
            {
                "member_id": piece.id_member,
                "role_label": role_label,
                "slot": {"field": layout.signature_field},
                "fields": [
                    {
                        "name": f.name,
                        "type": f.type,
                        "label": f.labels.get(locale) or f.labels.get("fr"),
                        "required": f.required,
                        "slot": {"field": f.pdf_field},
                    }
                    for f in layout.fields
                ],
            }
        ],
    }


async def next_attempt(
    session: AsyncSession, piece: DocumentModel, version: DocumentVersionModel
) -> int:
    count = await session.scalar(
        select(func.count(PieceSignatureModel.id)).where(
            PieceSignatureModel.id_document == piece.id,
            PieceSignatureModel.id_version == version.id,
        )
    )
    return int(count or 0) + 1


async def live_request(session: AsyncSession, piece: DocumentModel) -> PieceSignatureModel | None:
    row: PieceSignatureModel | None = await session.scalar(
        select(PieceSignatureModel).where(
            PieceSignatureModel.id_document == piece.id,
            PieceSignatureModel.status.in_([int(s) for s in LIVE_SIGNATURE_STATUSES]),
        )
    )
    return row


async def latest_by_piece(
    session: AsyncSession, piece_ids: list[int]
) -> dict[int, PieceSignatureModel]:
    """The latest request of each piece, for the procedure's detail."""
    if not piece_ids:
        return {}
    rows = await session.scalars(
        select(PieceSignatureModel)
        .where(PieceSignatureModel.id_document.in_(piece_ids))
        .order_by(PieceSignatureModel.id_document, PieceSignatureModel.id.desc())
    )
    latest: dict[int, PieceSignatureModel] = {}
    for row in rows:
        latest.setdefault(row.id_document, row)
    return latest


def signature_out(row: PieceSignatureModel | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "status": PieceSignatureStatus(row.status).name.lower(),
        "request_id": str(row.request_id) if row.request_id else None,
        "reason": row.reason,
        "updated_at": row.updated_at,
    }


# ---------------------------------------------------------------------------
# The service's events
# ---------------------------------------------------------------------------

_SETTLES = {
    "declined": PieceSignatureStatus.DECLINED,
    "expired": PieceSignatureStatus.EXPIRED,
    "cancelled": PieceSignatureStatus.CANCELLED,
    "failed": PieceSignatureStatus.FAILED,
}

Fetch = Callable[[str], Awaitable[bytes]]
Store = Callable[[str, bytes, str], Awaitable[str]]  # key, content, type -> file_ref


@dataclass(frozen=True)
class Applied:
    outcome: str  # updated | signed | signed_after_sending | unknown | stale
    id_community: int | None = None
    id_dossier: int | None = None


async def apply_event(
    session: AsyncSession,
    *,
    event: str,
    data: dict[str, Any],
    registry: PluginRegistry,
    fetch: Fetch,
    store: Store,
    now: datetime.datetime,
    today: datetime.date,
) -> Applied:
    """Follow one of the service's events; commits. The caller hints after."""
    key = data.get("idempotency_key")
    if not isinstance(key, str):
        return Applied("unknown")
    row = await session.scalar(
        select(PieceSignatureModel)
        .where(PieceSignatureModel.idempotency_key == key)
        .with_for_update()
    )
    if row is None:
        return Applied("unknown")
    # Read before any rollback: a rollback expires the row, and an async lazy
    # load of it raises MissingGreenlet.
    id_community = row.id_community
    if row.status not in (int(s) for s in LIVE_SIGNATURE_STATUSES):
        await session.rollback()
        return Applied("stale", id_community)
    piece = await session.get(DocumentModel, row.id_document)
    dossier = await session.get(DossierModel, piece.id_dossier) if piece else None
    if piece is None or dossier is None:
        await session.rollback()
        return Applied("unknown", id_community)
    id_dossier = dossier.id
    if data.get("request_id") and row.request_id is None:
        row.request_id = data["request_id"]
    outcome = "updated"
    if event in ("accepted",):
        pass
    elif event in ("prepared", "signer_signed"):
        row.status = int(PieceSignatureStatus.OPEN)
    elif event in _SETTLES:
        row.status = int(_SETTLES[event])
        row.reason = (
            str(data.get("reason") or data.get("detail") or data.get("code") or "")[:2000] or None
        )
    elif event == "completed":
        outcome = await _completed(
            session,
            row,
            dossier,
            piece,
            data,
            registry=registry,
            fetch=fetch,
            store=store,
            today=today,
        )
    else:
        await session.rollback()
        return Applied("unknown", id_community, id_dossier)
    row.updated_at = now
    await session.commit()
    return Applied(outcome, id_community, id_dossier)


async def _completed(
    session: AsyncSession,
    row: PieceSignatureModel,
    dossier: DossierModel,
    piece: DocumentModel,
    data: dict[str, Any],
    *,
    registry: PluginRegistry,
    fetch: Fetch,
    store: Store,
    today: datetime.date,
) -> str:
    final_key = str(data.get("final_key") or "")
    final_sha = str(data.get("final_sha256") or "")
    content = await fetch(final_key)
    if hashlib.sha256(content).hexdigest() != final_sha:
        raise ValueError(f"the signed PDF at {final_key} does not match its digest")
    row.status = int(PieceSignatureStatus.SIGNED)
    row.final_key = final_key
    row.final_sha256 = final_sha
    row.evidence_pdf_key = data.get("evidence_pdf_key")
    row.evidence_json_key = data.get("evidence_json_key")
    if dossier.frozen_at is not None:
        # A sent piece takes no new version (migration 0005): the signature
        # stays with the service, and the manager sees it signed.
        logger.warning("piece %s was signed after its procedure was sent", piece.id)
        return "signed_after_sending"
    plugin = plugin_for(registry, dossier.regulator)
    if plugin is None:
        raise LookupError(f"no plugin for {dossier.regulator}")
    ctx = await context_of_procedure(session, plugin, dossier)
    version_no = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(DocumentVersionModel.version_no), 0)).where(
                    DocumentVersionModel.id_document == piece.id
                )
            )
            or 0
        )
        + 1
    )
    key = f"{STORAGE_KEY_PREFIX}/{dossier.id_community}/{piece.id}/v{version_no}/{final_sha}"
    version = DocumentVersionModel(
        id_community=dossier.id_community,
        id_document=piece.id,
        version_no=version_no,
        file_ref=await store(key, content, PDF),
        content_sha256=final_sha,
        content_type=PDF,
        byte_size=len(content),
        original_filename=final_key.rsplit("/", 1)[-1],
        generated_by=SIGNATURE_ACTOR,
        stage=int(VersionStage.FINAL),
    )
    session.add(version)
    await session.flush()
    piece.current_version_id = version.id
    piece.origin = int(DocOrigin.UPLOADED)
    row.id_signed_version = version.id
    await supply_piece(
        session,
        ctx,
        dossier,
        piece,
        status=ProcedureDocumentStatus.SIGNED,
        day=today,
        actor=SIGNATURE_ACTOR,
        context={"version": version.id, "signature_request": str(row.request_id)},
    )
    return "signed"
