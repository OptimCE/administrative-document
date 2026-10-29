"""The administrative-document routes outside ``/admin``: what a procedure's
pieces need from the CRM and the renderer, and what a member may read.

- **Generation**: prefill (CRM -> form, nothing persisted), generate (freeze the
  snapshot, claim the render slot, commit, then publish), render status, and the
  download of a stored version. A render is technical, never journaled; a
  landed render makes the piece ready (``engine.filing.piece_rendered``, in the
  worker).
- **Filings**: what has been filed about the calling member, reduced to their
  own rows before the payload is built.
- **Templates**: the registry generation resolves against.

A procedure's lifecycle (steps, deadlines, uploads, sending, responses) is the
engine's, under ``/admin`` (``api/admin/procedures.py``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.administrative_document.repository import AdministrativeDocumentRepository
from core import metrics
from core.audit_log.actions import AuditActions
from core.audit_log.dtos import AuditLogInput
from core.audit_log.service import AuditLogService
from core.config import settings
from core.context_vars import current_user_id
from core.errors.errors import ErrorException
from core.queue.helper import Event
from core.storage import download, parse_s3_uri
from domain import prefill
from ports.crm_core import CrmCoreReadPort
from ports.events import EventPublisher
from shared.const import (
    EVENT_GENERATE_REQUESTED,
    SUBJECT_GENERATE_REQUESTED,
    ProcedureDocumentStatus,
)
from shared.custom_errors import errors
from shared.models.local_models import (
    DocumentModel,
    DocumentVersionModel,
    DossierModel,
)

logger = logging.getLogger(__name__)

# The statuses a piece may be (re)generated from.
_GENERATABLE_PIECE = (
    int(ProcedureDocumentStatus.MISSING),
    int(ProcedureDocumentStatus.STALE),
    int(ProcedureDocumentStatus.READY),
)


@dataclass(frozen=True, slots=True)
class MyFiling:
    """One filing of record, with only the caller's own rows out of its snapshot.

    A value object rather than a tuple because the mapper reads four different
    things off it and a positional tuple would make a reordering silent.
    """

    dossier: DossierModel
    document: DocumentModel
    version: DocumentVersionModel
    rows: dict[str, list[dict[str, Any]]]


class AdministrativeDocumentService:
    def __init__(
        self,
        *,
        local_session: AsyncSession,
        crm_session: AsyncSession,
        repository: AdministrativeDocumentRepository,
        crm_read: CrmCoreReadPort,
        publisher: EventPublisher,
    ) -> None:
        self._local = local_session
        self._crm = crm_session
        self._repo = repository
        self._crm_read = crm_read
        self._publisher = publisher
        self._audit = AuditLogService(crm_session)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_sharing_operations(self, id_community: int):
        """The community's sharing operations."""
        return await self._crm_read.list_sharing_operations(id_community)

    async def get_dossier_or_404(self, dossier_id: int) -> DossierModel:
        dossier = await self._repo.get_dossier(dossier_id)
        if dossier is None:
            raise ErrorException(errors.admin.DOSSIER_NOT_FOUND, status_code=404)
        return dossier

    async def get_document_or_404(self, document_id: int) -> DocumentModel:
        document = await self._repo.get_document(document_id)
        if document is None:
            raise ErrorException(errors.admin.DOCUMENT_NOT_FOUND, status_code=404)
        return document

    async def my_filings(self, *, id_community: int, limit: int) -> list[MyFiling]:
        """Every filing of record that names the caller, reduced to their own rows.

        The privacy boundary of decision B4, and it lives HERE rather than in the
        client: the filtering happens before the payload is built, so the rest of
        the community never crosses the wire. A frontend filter over the whole
        snapshot would be no protection at all — the data would already be in the
        browser's network log.

        Three properties worth stating, because each is a way this could quietly
        become wrong:

        * **Identity comes from the snapshot, not from the CRM.** The rows carry
          the member id they were frozen with, so a meter that changed hands
          after filing does not retroactively re-attribute anybody's row.
        * **A row with no `_id_member` is returned to nobody**, including the
          member whose name it carries. Filings frozen before that key existed
          therefore show as "nothing on record" rather than as a guess.
        * **An unlinked caller gets an empty list, not a 403.** Refusing would
          leak whether the caller represents a member at all, and "you appear in
          nothing" is a real answer to the question asked.
        """
        auth_user_id = current_user_id.get()
        if not auth_user_id:
            return []
        member_ids = set(
            await self._crm_read.member_ids_for_user(
                id_community=id_community, auth_user_id=auth_user_id
            )
        )
        if not member_ids:
            return []

        rows = await self._repo.list_current_versions_with_snapshot(limit=limit)
        filings: list[MyFiling] = []
        for version, document, dossier in rows:
            my_rows = prefill.rows_for_members(version.data_snapshot_json, member_ids)
            if not any(my_rows.values()):
                continue
            filings.append(
                MyFiling(dossier=dossier, document=document, version=version, rows=my_rows)
            )
        return filings

    # ---- reference registries ---------------------------------------------

    async def list_templates(self, *, region: int | None, doc_type: str | None):
        return await self._repo.list_templates(region=region, doc_type=doc_type)

    async def create_template(self, **values: Any):
        template = self._repo.add_template(**values)
        await self._local.commit()
        await self._local.refresh(template)
        await self._audit_and_commit(
            AuditActions.TEMPLATE_SAVED,
            entity_type="document_template",
            entity_id=str(template.id),
            payload={"doc_type": template.doc_type, "version": template.version},
        )
        return template

    async def update_template(self, template_id: int, changes: dict[str, Any]):
        # Own override only: a platform default is visible to this community but
        # belongs to every one of them, so it reads as "not found" here.
        template = await self._repo.get_own_template(template_id)
        if template is None:
            raise ErrorException(errors.admin.TEMPLATE_NOT_FOUND, status_code=404)
        for field, value in changes.items():
            if value is not None:
                setattr(template, field, value)
        await self._local.commit()
        await self._local.refresh(template)
        await self._audit_and_commit(
            AuditActions.TEMPLATE_SAVED,
            entity_type="document_template",
            entity_id=str(template.id),
            payload={"updated": sorted(k for k, v in changes.items() if v is not None)},
        )
        return template

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def build_prefill(self, *, document_id: int) -> dict[str, Any]:
        """Assemble the render payload from the CRM, WITHOUT persisting anything.

        This is what the review form renders. Nothing is written, so a user may
        open it as often as they like; the snapshot is only frozen when they
        actually submit a generation.
        """
        document = await self.get_document_or_404(document_id)
        dossier = await self.get_dossier_or_404(document.id_dossier)
        context = await self._crm_read.get_community_context(document.id_community)
        if context is None:
            raise ErrorException(errors.admin.REGION_NOT_RESOLVED, status_code=422)

        # A community-scope procedure has no operation: its forms are filled
        # from every participant of the community.
        operation = (
            await self._crm_read.get_sharing_operation(
                document.id_community, dossier.id_sharing_operation
            )
            if dossier.id_sharing_operation is not None
            else None
        )
        crm_data = await self._crm_read.get_operation_participants(
            document.id_community, dossier.id_sharing_operation
        )
        snapshot = prefill.build_snapshot(
            document.doc_type,
            community=context,
            operation_name=operation.name if operation else None,
            participants=crm_data.participants,
            production=crm_data.production,
            storage=crm_data.storage,
        )
        # Warnings ride alongside the payload rather than inside it: they are for
        # the reviewer, not for the form. Four sources — the CRM read (a delivery
        # point nobody owns), the community identity header, the member sheet when
        # this doc_type has one, and an EAN the form physically cannot print —
        # merged and sorted so the panel is stable across repeated prefills.
        warnings = prefill.sort_warnings(
            (
                *crm_data.warnings,
                *prefill.community_warnings(context),
                *prefill.participant_warnings(document.doc_type, crm_data.participants),
                *prefill.form_representation_warnings(document.doc_type, snapshot),
            )
        )
        return {"data": snapshot, "warnings": warnings}

    async def request_generation(
        self, *, document_id: int, data: dict[str, Any] | None
    ) -> tuple[DocumentModel, str]:
        """Freeze the snapshot, claim the render slot, commit, then publish.

        Returns ``(document, docgen_request_id)``.

        Ordering is the idempotency hinge: the request id is committed *before*
        the work message goes out, so a redelivery reuses it — and therefore the
        same deterministic object key — instead of littering the bucket with
        orphaned renders.
        """
        document = await self.get_document_or_404(document_id)
        dossier = await self.get_dossier_or_404(document.id_dossier)

        # A piece is (re)generated while it is missing, stale or ready, and never
        # once its procedure was sent: what was sent is frozen with it.
        if dossier.frozen_at is not None or document.status not in _GENERATABLE_PIECE:
            raise ErrorException(errors.admin.GENERATION_NOT_ALLOWED, status_code=409)
        template = await self._repo.get_current_template(
            region=dossier.region, doc_type=document.doc_type
        )
        if template is None or not template.file_ref:
            # Either no form is in force for this region/type, or one is
            # registered but has no bundle yet — both are configuration, not user
            # error, and both are fixed by a seed change rather than a retry.
            raise ErrorException(errors.admin.TEMPLATE_NOT_REGISTERED, status_code=422)

        prefilled = await self.build_prefill(document_id=document_id)
        snapshot = prefill.merge_overrides(prefilled["data"], data)
        # The comb boxes are a projection of the EAN, recomputed AFTER the
        # reviewer's corrections: merge_overrides is shallow, so an edited EAN
        # would otherwise leave stale digits in the snapshot that gets frozen,
        # filed and printed. An unrepresentable EAN removes the keys entirely
        # and the boxes stay blank and hand-fillable.
        snapshot = prefill.project_comb_fields(document.doc_type, snapshot)

        request_id = uuid.uuid4().hex
        claimed = await self._repo.claim_render(
            document_id=document_id,
            docgen_request_id=request_id,
            id_template=template.id,
            data_snapshot=snapshot,
            requested_by=current_user_id.get(),
            stale_after_seconds=settings.RENDER_STALE_AFTER_SECONDS,
        )
        if not claimed:
            raise ErrorException(errors.admin.RENDER_ALREADY_IN_FLIGHT, status_code=409)
        await self._local.commit()

        # Published after the commit. A broker outage leaves a PENDING render the
        # staleness window reclaims, rather than a committed-but-unrequested one.
        await self._safe_publish(
            SUBJECT_GENERATE_REQUESTED,
            Event(
                type=EVENT_GENERATE_REQUESTED,
                data={"document_id": document_id, "id_community": document.id_community},
            ),
        )
        metrics.document_generations_requested.add(1, {"doc_type": document.doc_type})
        await self._audit_and_commit(
            AuditActions.DOCUMENT_GENERATION_REQUESTED,
            entity_type="document",
            entity_id=str(document_id),
            payload={"doc_type": document.doc_type, "request_id": request_id},
        )
        return document, request_id

    async def get_render_status(self, *, document_id: int) -> dict[str, Any]:
        """Small poll target for the UI. No render row means idle or landed."""
        document = await self.get_document_or_404(document_id)
        render = await self._repo.get_render(document_id)
        return {
            "document_id": document_id,
            "render_state": render.render_state if render else None,
            "render_error": (render.render_error_json if render else None),
            "requested_at": render.requested_at if render else None,
            "current_version_id": document.current_version_id,
            "status": document.status,
        }

    async def read_version_bytes(self, *, document_id: int, version_id: int):
        version = await self._repo.get_version(document_id, version_id)
        if version is None:
            raise ErrorException(errors.admin.VERSION_NOT_FOUND, status_code=404)
        try:
            bucket, key = parse_s3_uri(version.file_ref)
            content = await download(key, bucket=bucket)
        except ErrorException:
            raise
        except Exception as exc:
            logger.exception("version download failed version_id=%s", version_id)
            raise ErrorException(errors.admin.STORAGE_DOWNLOAD_FAILED, status_code=502) from exc
        return version, content

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _safe_publish(self, subject: str, event: Event) -> None:
        """Publish without ever failing the caller.

        The render slot is already committed; a broker outage leaves a PENDING
        render the staleness window reclaims, never an HTTP 500.
        """
        try:
            await self._publisher.publish(subject, event)
        except Exception:
            logger.exception("failed to publish %s on %s", event.type, subject)

    async def _stage_audit(
        self, action: str, *, entity_type: str, entity_id: str | None, payload: dict[str, Any]
    ) -> None:
        """Stage an audit row on the CRM session. The caller owns the commit."""
        await self._audit.log(
            AuditLogInput(
                action=action, entity_type=entity_type, entity_id=entity_id, payload=payload
            )
        )

    async def _audit_and_commit(
        self, action: str, *, entity_type: str, entity_id: str | None, payload: dict[str, Any]
    ) -> None:
        """Stage an audit row and commit the CRM session, best-effort.

        The swallow is right for the paths that use it: the local write is
        already durable and a CRM hiccup must not turn a successful request into
        a 500. It is NOT right where the same transaction carries a notification
        nothing will ever re-emit (the daily jobs commit explicitly for that).
        """
        await self._stage_audit(
            action, entity_type=entity_type, entity_id=entity_id, payload=payload
        )
        try:
            await self._crm.commit()
        except Exception:
            logger.exception("audit commit failed for %s %s", entity_type, entity_id)
