"""Attach a finished render to its document.

The result stream belongs to document-generation; we only add a durable consumer
to it. ``process_docgen_result`` is extracted from the subscription so tests can
drive it directly with a session.

**Correlation is by ``request_id``, never by ``metadata``.** ``metadata`` is
caller-supplied data that round-tripped through another service, so using it as
the lookup key would let a replayed or malformed message aim this handler at an
arbitrary document. ``document_render.docgen_request_id`` is UNIQUE in our own
database; that is the key. ``metadata`` is only ever a cross-check.

For the same reason the **tenant comes from the row we just found**, not from the
message. That forces the initial lookup to run unscoped — see
``_find_render_unscoped``, which is deliberately named so nobody "fixes" it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics
from core.audit_log import AuditActions, AuditLogInput, AuditLogService
from core.config import settings
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from core.queue.helper import Event, send_event
from shared.const import (
    CONTENT_TYPE_BY_FORMAT,
    EVENT_DOCUMENT_RENDERED,
    SUBJECT_DOCUMENT_RENDERED,
    DocOrigin,
    RenderState,
)
from shared.models.local_models import (
    DocumentModel,
    DocumentRenderModel,
    DocumentVersionModel,
)
from worker.context import with_tenant

logger = logging.getLogger(__name__)

_DOCGEN_DURABLE = "worker-administrative-document-docgen"
_ACK_WAIT_SECONDS = 60
_NAK_RETRY_DELAY_SECONDS = 30
# Bounded, unlike billing's equivalent consumer: without max_deliver a
# persistently failing result redelivers every 30 s forever with nowhere to land.
_MAX_DELIVER = 5

# Outcomes, mirroring the ack/nak decision the subscription makes.
ATTACHED = "attached"
RENDER_FAILED = "render_failed"
TRANSIENT = "transient"
DROP = "drop"


def _metadata(body: dict[str, Any]) -> dict[str, Any]:
    """The echoed metadata, or an empty dict.

    ``GenerationResult`` is ``extra="forbid"`` and declares no ``tenant_id``, so
    everything the caller sent about itself arrives here — but it is still data
    that round-tripped through another service, and is therefore only ever a
    cross-check, never a source of truth.
    """
    metadata = body.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


async def _find_render_unscoped(
    session: AsyncSession, request_id: str
) -> DocumentRenderModel | None:
    """Find the pending render by request id, WITHOUT a community filter.

    Deliberate, and the only unscoped read in the service: a result arrives
    carrying a request id and nothing trustworthy about the tenant, so the tenant
    has to be discovered here. ``docgen_request_id`` is UNIQUE, so this can match
    at most one row, and every read that follows is scoped to that row's
    community.
    """
    return (
        await session.execute(
            select(DocumentRenderModel).where(DocumentRenderModel.docgen_request_id == request_id)
        )
    ).scalar_one_or_none()


async def _already_attached(session: AsyncSession, request_id: str) -> bool:
    """Has this exact render already produced a version?

    ``document_version`` is append-only, so this survives the next regeneration
    reusing the render slot — which is what lets a duplicate delivery be told
    apart from a stale one instead of guessed at.
    """
    return (
        await session.execute(
            select(DocumentVersionModel.id).where(
                DocumentVersionModel.docgen_request_id == request_id
            )
        )
    ).first() is not None


async def process_docgen_result(
    body: dict[str, Any],
    *,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
) -> str:
    """Attach or fail-mark from one docgen result. Returns an outcome constant."""
    own_local = local_session is None
    own_crm = crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    try:
        request_id = body.get("request_id")
        if not request_id:
            logger.error("docgen result without request_id: %r", body)
            metrics.document_renders.add(1, {"outcome": "dropped"})
            return DROP

        render = await _find_render_unscoped(local, request_id)
        if render is None:
            if await _already_attached(local, request_id):
                return ATTACHED  # idempotent redelivery of a handled result
            logger.warning("docgen result for unknown/superseded request %s", request_id)
            metrics.document_renders.add(1, {"outcome": "dropped"})
            return DROP

        # Defence in depth: the message must agree with the row it claims to be
        # about. A mismatch means a replay or a bug, never something to act on.
        claimed_document = _metadata(body).get("document_id")
        if claimed_document is not None and int(claimed_document) != render.id_document:
            logger.error(
                "docgen result %s claims document %s but the render row is for %s; ignoring",
                request_id,
                claimed_document,
                render.id_document,
            )
            metrics.document_renders.add(1, {"outcome": "dropped"})
            return DROP

        with with_tenant(render.id_community):
            if body.get("status") == "success":
                return await _attach(local, crm, render, body, own_local=own_local, own_crm=own_crm)

            error = body.get("error") or {}
            if error.get("permanent"):
                render.render_state = int(RenderState.FAILED)
                render.render_error_json = {
                    "code": error.get("code"),
                    "message": error.get("message"),
                    "permanent": True,
                }
                if own_local:
                    await local.commit()
                await _audit(
                    crm,
                    AuditActions.DOCUMENT_RENDER_FAILED,
                    render.id_document,
                    {"error": error.get("code")},
                    id_community=render.id_community,
                    commit=own_crm,
                )
                metrics.document_renders.add(1, {"outcome": "failed"})
                return RENDER_FAILED

            # Transient: docgen will not retry for us (it already gave up on this
            # attempt), but a redelivery of the *result* may still succeed.
            return TRANSIENT
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()


async def _attach(
    local: AsyncSession,
    crm: AsyncSession,
    render: DocumentRenderModel,
    body: dict[str, Any],
    *,
    own_local: bool,
    own_crm: bool,
) -> str:
    """Record the artifact as the document's next immutable version."""
    artifact = _first_artifact(body)
    if artifact is None:
        logger.warning("docgen success without an artifact for %s", render.docgen_request_id)
        return TRANSIENT

    document = await local.get(DocumentModel, render.id_document)
    if document is None:  # deleted between request and result
        return DROP

    next_no = int(
        (
            await local.execute(
                select(func.coalesce(func.max(DocumentVersionModel.version_no), 0)).where(
                    DocumentVersionModel.id_document == render.id_document
                )
            )
        ).scalar_one()
        + 1
    )
    version = DocumentVersionModel(
        id_community=render.id_community,
        id_document=render.id_document,
        version_no=next_no,
        file_ref=artifact["uri"],
        content_sha256=artifact.get("sha256"),
        content_type=_content_type(artifact.get("format")),
        byte_size=artifact.get("size_bytes"),
        # The bundle already named this artifact (manifest.output_basename +
        # extension) and that name is the last segment of the URI. Recording it
        # is what lets the download arrive as "annexe6-notification.xlsx"
        # instead of an extensionless blob the OS cannot open.
        original_filename=_artifact_filename(artifact["uri"]),
        id_template=render.id_template,
        # The snapshot frozen at REQUEST time, not re-read now: that is what makes
        # "the filed version reflects the state at generation time" true.
        data_snapshot_json=render.data_snapshot_json,
        generated_by=render.requested_by or "system",
        docgen_request_id=render.docgen_request_id,
    )
    local.add(version)
    await local.flush()

    document.current_version_id = version.id
    # origin describes how the CURRENT version was produced, so it follows the
    # version that just became current. A document is created before anyone knows
    # whether it will be uploaded or generated, so this is the only place the
    # answer is actually known.
    document.origin = int(DocOrigin.GENERATED)
    # Success clears the in-flight slot: the version row is now the record. The
    # document's STATUS is untouched — a render is technical, and only a human
    # moves a document to READY.
    await local.delete(render)
    if own_local:
        await local.commit()

    metrics.document_versions_stored.add(1, {"origin": DocOrigin.GENERATED.name})
    metrics.document_renders.add(1, {"outcome": "attached"})
    await _audit(
        crm,
        AuditActions.DOCUMENT_RENDERED,
        render.id_document,
        {"uri": artifact["uri"], "version_no": next_no},
        id_community=render.id_community,
        commit=own_crm,
    )
    return ATTACHED


def _first_artifact(body: dict[str, Any]) -> dict[str, Any] | None:
    for artifact in body.get("artifacts") or []:
        if artifact.get("uri"):
            return artifact  # type: ignore[no-any-return]
    return None


def _content_type(fmt: str | None) -> str | None:
    return CONTENT_TYPE_BY_FORMAT.get(fmt or "")


def _artifact_filename(uri: str) -> str | None:
    """The artifact's own name — the last segment of its object key.

    document-generation names every artifact from its manifest
    (``output_basename`` + format extension), so this is a meaningful filename
    rather than a synthesised one. Returns None if the URI has no usable tail,
    in which case the download route falls back to a generated name.
    """
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


async def _audit(
    crm: AsyncSession,
    action: str,
    document_id: int,
    payload: dict[str, Any],
    *,
    id_community: int,
    commit: bool,
) -> None:
    """Write the CRM audit row. Never fails the caller: the local transaction is
    already committed, and losing an audit line must not cause a redelivery that
    would try to attach the same version twice."""
    try:
        await AuditLogService(crm).log(
            AuditLogInput(
                action=action,
                entity_type="document",
                entity_id=str(document_id),
                payload=payload,
            ),
            id_community=id_community,
        )
        if commit:
            await crm.commit()
    except Exception:
        logger.exception("audit write failed for document %s", document_id)


async def publish_rendered(js: JetStreamContext, document_id: int, version_id: int) -> None:
    """Notify observers. Best-effort: the version is already recorded."""
    try:
        await send_event(
            js,
            SUBJECT_DOCUMENT_RENDERED,
            Event(
                type=EVENT_DOCUMENT_RENDERED,
                data={"document_id": document_id, "version_id": version_id},
            ),
        )
    except Exception:
        logger.exception("failed to publish rendered event for document %s", document_id)


async def subscribe(js: JetStreamContext, *, on_dlq=None):
    """Bind the durable consumer to document-generation's results stream.

    We do NOT declare that stream — it is owned by document-generation and
    ``docgen.result.>`` already matches our reply subject.
    """

    async def _handle(msg: Msg) -> None:
        try:
            body = json.loads(msg.data)
        except Exception:
            logger.exception("undecodable docgen result; acking and dropping")
            await msg.ack()
            return
        try:
            outcome = await process_docgen_result(body)
        except Exception:
            logger.exception("docgen result handler crashed")
            outcome = TRANSIENT

        if outcome != TRANSIENT:
            await msg.ack()
            return

        # Bounded retry, then park it. Billing's equivalent consumer has neither,
        # so a poison result there redelivers forever.
        if msg.metadata.num_delivered >= _MAX_DELIVER:
            logger.error(
                "docgen result %s exhausted %d deliveries; routing to DLQ",
                body.get("request_id"),
                _MAX_DELIVER,
            )
            if on_dlq is not None:
                await on_dlq(msg)
            await msg.ack()
        else:
            await msg.nak(delay=_NAK_RETRY_DELAY_SECONDS)

    return await js.subscribe(
        subject=settings.DOCGEN_RESULT_SUBJECT,
        durable=_DOCGEN_DURABLE,
        queue=_DOCGEN_DURABLE,
        manual_ack=True,
        cb=_handle,
        config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS, max_deliver=_MAX_DELIVER + 1),
    )
