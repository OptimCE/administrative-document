"""Turn a committed generation request into a docgen render request.

The API has already frozen the snapshot and claimed the render slot, so this
handler is a pure translation step: read the ``document_render`` row, resolve its
template, and publish the flat docgen body.

Idempotent by construction. A redelivery finds the same ``docgen_request_id`` and
therefore builds the same ``key_prefix``, so document-generation overwrites one
object instead of leaving an orphan behind — and if the render already landed,
the row is gone and there is nothing to do.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.database import AsyncSessionLocalFactory
from ports.document_generation import DocgenRequest, DocumentGenerationPort
from shared.const import STORAGE_KEY_PREFIX
from shared.models.local_models import (
    DocumentModel,
    DocumentRenderModel,
    DocumentTemplateModel,
)
from worker.context import with_tenant

logger = logging.getLogger(__name__)


class GenerateRequestError(Exception):
    """A non-retryable failure: the row or its template is unusable."""


async def process_generate(
    document_id: int,
    *,
    doc_port: DocumentGenerationPort,
    local_session: AsyncSession | None = None,
) -> bool:
    """Publish the docgen request for ``document_id``. False if there is nothing to do."""
    own_local = local_session is None
    local = local_session or AsyncSessionLocalFactory()
    try:
        render = (
            await local.execute(
                select(DocumentRenderModel).where(DocumentRenderModel.id_document == document_id)
            )
        ).scalar_one_or_none()
        if render is None:
            # Already rendered (the row is deleted on success), or superseded.
            logger.info("no pending render for document %s; nothing to request", document_id)
            return False

        with with_tenant(render.id_community):
            document = await local.get(DocumentModel, document_id)
            if document is None:
                raise GenerateRequestError(f"document {document_id} vanished")
            template = await local.get(DocumentTemplateModel, render.id_template)
            if template is None or not template.file_ref:
                raise GenerateRequestError(
                    f"template {render.id_template} has no bundle for document {document_id}"
                )

            await doc_port.request_render(
                DocgenRequest(
                    request_id=render.docgen_request_id,
                    tenant_id=str(render.id_community),
                    template_uri=template.file_ref,
                    output_format=template.output_format,
                    data=render.data_snapshot_json,
                    # Deterministic in the request id, so a redelivery overwrites
                    # rather than accumulating orphaned artifacts.
                    key_prefix=(
                        f"{STORAGE_KEY_PREFIX}/{render.id_community}/{document_id}"
                        f"/{render.docgen_request_id}/"
                    ),
                    reply_to=settings.DOCGEN_RESULT_SUBJECT,
                    presign_ttl=settings.DOCGEN_PRESIGN_TTL,
                    # tenant_id rides here because GenerationResult is
                    # extra="forbid" and echoes ONLY metadata. document_id is a
                    # cross-check, never the lookup key — see docgen_results.
                    metadata={
                        "tenant_id": str(render.id_community),
                        "document_id": document_id,
                        "doc_type": document.doc_type,
                    },
                )
            )
        return True
    finally:
        if own_local:
            await local.close()
