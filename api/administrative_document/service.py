"""Orchestration for dossiers, documents, versions, transitions and deadlines.

The centre of gravity is ``_transition``. Every status change in the system goes
through it, in this order:

1. validate the edge and its required context (pure, ``domain.statemachine``);
2. in ONE local transaction, append the journal entry **and** move the status
   cache, then derive any deadlines the transition implies;
3. commit;
4. only then publish the NATS event and write the CRM audit row.

Steps 2 and 3 are what make the journal authoritative: the database's deferred
constraint trigger aborts the commit if a status moved without a journal entry.
Step 4 is deliberately after the commit — a broker hiccup must never roll back a
recorded regulatory act.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from api.administrative_document.repository import AdministrativeDocumentRepository
from core import metrics
from core.audit_log.actions import AuditActions
from core.audit_log.dtos import AuditLogInput
from core.audit_log.service import AuditLogService
from core.config import settings
from core.context_vars import current_user_id
from core.errors.errors import ErrorException
from core.notifications import (
    MANAGER_ROLES,
    Channel,
    CommunityTarget,
    NotificationCategory,
    NotificationService,
    NotificationTypes,
)
from core.queue.helper import Event
from core.storage import build_s3_uri, download, parse_s3_uri, upload
from domain import deadlines as deadline_engine
from domain import prefill, statemachine
from domain.calendar import BelgianCalendar
from ports.crm_core import CrmCoreReadPort
from ports.events import EventPublisher
from shared.const import (
    EVENT_DEADLINE_CREATED,
    EVENT_DEADLINE_MISSED,
    EVENT_DOCUMENT_STATUS_CHANGED,
    EVENT_DOSSIER_STATUS_CHANGED,
    EVENT_GENERATE_REQUESTED,
    STORAGE_KEY_PREFIX,
    SUBJECT_DEADLINE_CREATED,
    SUBJECT_DEADLINE_MISSED,
    SUBJECT_DOCUMENT_STATUS_CHANGED,
    SUBJECT_DOSSIER_STATUS_CHANGED,
    SUBJECT_GENERATE_REQUESTED,
    UPLOAD_MAX_BODY_BYTES,
    AcknowledgementResult,
    DeadlineStatus,
    DeadlineType,
    DocOrigin,
    DocumentStatus,
    DossierStatus,
    DossierType,
    SubjectType,
    TriggerEvent,
)
from shared.custom_errors import errors
from shared.models.local_models import (
    DeadlineModel,
    DocumentModel,
    DocumentVersionModel,
    DossierModel,
)

if TYPE_CHECKING:
    # Annotation only — `from __future__ import annotations` keeps it a string,
    # and `_read_within_cap` only ever calls `await file.read(...)`. It must stay
    # under TYPE_CHECKING: the deadline sweep imports this module from
    # `worker/sweeps.py`, and the worker image has no fastapi (see
    # `Dockerfile.worker`). A runtime import here crash-loops that container.
    from fastapi import UploadFile

logger = logging.getLogger(__name__)


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


BRUSSELS = ZoneInfo("Europe/Brussels")

_READ_CHUNK_BYTES = 1024 * 1024


class AdministrativeDocumentService:
    def __init__(
        self,
        *,
        local_session: AsyncSession,
        crm_session: AsyncSession,
        repository: AdministrativeDocumentRepository,
        crm_read: CrmCoreReadPort,
        publisher: EventPublisher,
        calendar: BelgianCalendar | None = None,
    ) -> None:
        self._local = local_session
        self._crm = crm_session
        self._repo = repository
        self._crm_read = crm_read
        self._publisher = publisher
        self._calendar = calendar or BelgianCalendar()
        self._audit = AuditLogService(crm_session)
        self._notify = NotificationService(crm_session)

    # ------------------------------------------------------------------
    # Dossiers
    # ------------------------------------------------------------------

    async def create_dossier(
        self,
        *,
        id_community: int,
        dossier_type: DossierType,
        title: str | None,
        external_ref: str | None,
        id_sharing_operation: int,
        metadata: dict[str, Any],
    ) -> DossierModel:
        region = await self._resolve_region(id_community)
        # The operation lookup is scoped to the caller's community, so an id
        # belonging to another tenant reads as "not found" rather than attaching
        # this dossier to someone else's sharing operation.
        operation = await self._crm_read.get_sharing_operation(id_community, id_sharing_operation)
        if operation is None:
            raise ErrorException(errors.admin.SHARING_OPERATION_NOT_FOUND, status_code=422)

        if external_ref and await self._repo.external_ref_taken(
            dossier_type=int(dossier_type), external_ref=external_ref
        ):
            raise ErrorException(errors.admin.DUPLICATE_EXTERNAL_REF, status_code=409)

        dossier = self._repo.add_dossier(
            dossier_type=int(dossier_type),
            status=int(statemachine.INITIAL_DOSSIER_STATUS),
            region=int(region),
            title=title,
            external_ref=external_ref,
            id_sharing_operation=id_sharing_operation,
            metadata=metadata,
        )
        # Flush to obtain the SERIAL id, then journal the birth event in the same
        # transaction so the status cache is backed from the very first commit.
        await self._local.flush()
        self._repo.append_status_event(
            subject_type=SubjectType.DOSSIER,
            subject_id=dossier.id,
            from_status=None,
            to_status=int(statemachine.INITIAL_DOSSIER_STATUS),
            is_corrective=False,
            actor_id=current_user_id.get(),
            context={"created": True},
        )
        await self._local.commit()
        await self._local.refresh(dossier)

        metrics.dossiers_created.add(1, {"dossier_type": dossier_type.name})
        await self._audit_and_commit(
            AuditActions.DOSSIER_CREATED,
            entity_type="dossier",
            entity_id=str(dossier.id),
            payload={
                "dossier_type": int(dossier_type),
                "id_sharing_operation": id_sharing_operation,
                "external_ref": external_ref,
            },
        )
        return dossier

    async def get_dossier_or_404(self, dossier_id: int) -> DossierModel:
        dossier = await self._repo.get_dossier(dossier_id)
        if dossier is None:
            raise ErrorException(errors.admin.DOSSIER_NOT_FOUND, status_code=404)
        return dossier

    async def update_dossier(
        self,
        dossier_id: int,
        *,
        title: str | None,
        external_ref: str | None,
        metadata: dict[str, Any] | None,
    ) -> DossierModel:
        dossier = await self.get_dossier_or_404(dossier_id)

        if external_ref is not None and external_ref != dossier.external_ref:
            if await self._repo.external_ref_taken(
                dossier_type=dossier.dossier_type,
                external_ref=external_ref,
                exclude_id=dossier.id,
            ):
                raise ErrorException(errors.admin.DUPLICATE_EXTERNAL_REF, status_code=409)
            dossier.external_ref = external_ref
        if title is not None:
            dossier.title = title
        if metadata is not None:
            dossier.metadata_json = metadata

        await self._local.commit()
        await self._local.refresh(dossier)
        await self._audit_and_commit(
            AuditActions.DOSSIER_UPDATED,
            entity_type="dossier",
            entity_id=str(dossier.id),
            payload={"external_ref": dossier.external_ref},
        )
        return dossier

    async def _resolve_region(self, id_community: int):
        context = await self._crm_read.get_community_context(id_community)
        if context is None or context.region is None:
            raise ErrorException(errors.admin.REGION_NOT_RESOLVED, status_code=422)
        return context.region

    # ---- reads -------------------------------------------------------------

    async def list_dossiers(
        self,
        *,
        page: int,
        limit: int,
        status: int | None,
        dossier_type: int | None,
        id_sharing_operation: int | None,
        sort: str | None,
        order: str | None,
    ):
        return await self._repo.list_dossiers(
            page=page,
            limit=limit,
            status=status,
            dossier_type=dossier_type,
            id_sharing_operation=id_sharing_operation,
            sort=sort,
            order=order,
        )

    async def list_sharing_operations(self, id_community: int):
        """The community's sharing operations — the choices a dossier can target."""
        return await self._crm_read.list_sharing_operations(id_community)

    async def dossier_detail(self, dossier_id: int):
        dossier = await self.get_dossier_or_404(dossier_id)
        documents = await self._repo.list_documents(dossier_id)
        deadlines = await self._repo.list_deadlines_for_dossier(dossier_id)
        return dossier, documents, deadlines

    async def list_documents(self, dossier_id: int):
        await self.get_dossier_or_404(dossier_id)
        return await self._repo.list_documents(dossier_id)

    async def document_detail(self, document_id: int):
        document = await self.get_document_or_404(document_id)
        versions = await self._repo.list_versions(document_id)
        return document, versions

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

    async def dossier_deadlines(self, dossier_id: int) -> list[DeadlineModel]:
        await self.get_dossier_or_404(dossier_id)
        return await self._repo.list_deadlines_for_dossier(dossier_id)

    async def dossier_timeline(self, dossier_id: int):
        await self.get_dossier_or_404(dossier_id)
        documents = await self._repo.list_documents(dossier_id)
        return await self._repo.dossier_timeline(dossier_id, [doc.id for doc, _ in documents])

    async def list_deadlines(
        self,
        *,
        page: int,
        limit: int,
        status: int | None,
        deadline_type: str | None,
        dossier_id: int | None,
        id_sharing_operation: int | None,
        sort: str | None,
        order: str | None,
    ):
        return await self._repo.list_deadlines(
            page=page,
            limit=limit,
            status=status,
            deadline_type=deadline_type,
            dossier_id=dossier_id,
            id_sharing_operation=id_sharing_operation,
            sort=sort,
            order=order,
        )

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

    async def list_deadline_rules(self, *, region: int | None):
        return await self._repo.list_deadline_rules(region=region)

    async def create_deadline_rule(self, **values: Any):
        rule = self._repo.add_deadline_rule(**values)
        await self._local.commit()
        await self._local.refresh(rule)
        await self._audit_and_commit(
            AuditActions.DEADLINE_RULE_SAVED,
            entity_type="deadline_rule",
            entity_id=str(rule.id),
            payload={"trigger_event": rule.trigger_event, "deadline_type": rule.deadline_type},
        )
        return rule

    async def update_deadline_rule(self, rule_id: int, changes: dict[str, Any]):
        # Own override only — see update_template.
        rule = await self._repo.get_own_deadline_rule(rule_id)
        if rule is None:
            raise ErrorException(errors.admin.DEADLINE_RULE_NOT_FOUND, status_code=404)
        for field, value in changes.items():
            if value is not None:
                setattr(rule, field, value)
        await self._local.commit()
        await self._local.refresh(rule)
        await self._audit_and_commit(
            AuditActions.DEADLINE_RULE_SAVED,
            entity_type="deadline_rule",
            entity_id=str(rule.id),
            payload={"updated": sorted(k for k, v in changes.items() if v is not None)},
        )
        return rule

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    async def create_document(
        self, *, dossier_id: int, doc_type: str, title: str | None
    ) -> DocumentModel:
        await self.get_dossier_or_404(dossier_id)
        document = self._repo.add_document(
            id_dossier=dossier_id,
            doc_type=doc_type,
            origin=int(DocOrigin.UPLOADED),
            status=int(statemachine.INITIAL_DOCUMENT_STATUS),
            title=title,
        )
        await self._local.flush()
        self._repo.append_status_event(
            subject_type=SubjectType.DOCUMENT,
            subject_id=document.id,
            from_status=None,
            to_status=int(statemachine.INITIAL_DOCUMENT_STATUS),
            is_corrective=False,
            actor_id=current_user_id.get(),
            context={"created": True},
        )
        await self._local.commit()
        await self._local.refresh(document)

        await self._audit_and_commit(
            AuditActions.DOCUMENT_CREATED,
            entity_type="document",
            entity_id=str(document.id),
            payload={"id_dossier": dossier_id, "doc_type": doc_type},
        )
        return document

    async def get_document_or_404(self, document_id: int) -> DocumentModel:
        document = await self._repo.get_document(document_id)
        if document is None:
            raise ErrorException(errors.admin.DOCUMENT_NOT_FOUND, status_code=404)
        return document

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------

    async def add_version(self, *, document_id: int, file: UploadFile):
        """Store an uploaded file as the document's next immutable version."""
        document = await self.get_document_or_404(document_id)

        # Versions may only be added while the document is still editable. A sent
        # or acknowledged document must be rolled back first — which is a traced
        # corrective transition, not a silent overwrite.
        if document.status not in (int(DocumentStatus.DRAFT), int(DocumentStatus.READY)):
            raise ErrorException(errors.admin.VERSION_NOT_ALLOWED, status_code=409)

        content = await self._read_within_cap(file)
        if not content:
            raise ErrorException(errors.admin.INVALID_FILE, status_code=422)

        digest = hashlib.sha256(content).hexdigest()
        version_no = await self._repo.next_version_no(document_id)
        key = (
            f"{STORAGE_KEY_PREFIX}/{document.id_community}/{document_id}" f"/v{version_no}/{digest}"
        )
        try:
            await upload(
                key,
                content,
                content_type=file.content_type,
                bucket=settings.OUTPUT_BUCKET,
            )
        except Exception as exc:
            logger.exception("version upload failed document_id=%s", document_id)
            raise ErrorException(errors.admin.STORAGE_UPLOAD_FAILED, status_code=502) from exc

        version = self._repo.add_version(
            id_document=document_id,
            version_no=version_no,
            file_ref=build_s3_uri(settings.OUTPUT_BUCKET, key),
            content_sha256=digest,
            content_type=file.content_type,
            byte_size=len(content),
            original_filename=file.filename,
            generated_by=current_user_id.get(),
        )
        await self._local.flush()
        document.current_version_id = version.id
        # origin describes how the CURRENT version was produced — so an upload
        # over a previously generated document flips it back.
        document.origin = int(DocOrigin.UPLOADED)
        await self._local.commit()
        await self._local.refresh(version)

        metrics.document_versions_stored.add(1, {"origin": DocOrigin(document.origin).name})
        await self._audit_and_commit(
            AuditActions.DOCUMENT_VERSION_ADDED,
            entity_type="document_version",
            entity_id=str(version.id),
            payload={
                "id_document": document_id,
                "version_no": version_no,
                "sha256": digest,
            },
        )
        return version

    # ------------------------------------------------------------------
    # Generation (Phase 2)
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

        operation = await self._crm_read.get_sharing_operation(
            document.id_community, dossier.id_sharing_operation
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
        # the reviewer, not for the form. Three sources — the CRM read (a delivery
        # point nobody owns), the community identity header, and the member sheet
        # when this doc_type has one — merged and sorted so the panel is stable
        # across repeated prefills.
        warnings = prefill.sort_warnings(
            (
                *crm_data.warnings,
                *prefill.community_warnings(context),
                *prefill.participant_warnings(document.doc_type, crm_data.participants),
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

        # Same rule as add_version, for the same reason: a filed document must be
        # rolled back before it can be replaced.
        if document.status not in (int(DocumentStatus.DRAFT), int(DocumentStatus.READY)):
            raise ErrorException(errors.admin.GENERATION_NOT_ALLOWED, status_code=409)

        dossier = await self.get_dossier_or_404(document.id_dossier)
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

    async def _read_within_cap(self, file: UploadFile) -> bytes:
        """Read an upload in bounded chunks.

        The request-limits middleware screens Content-Length, but a chunked
        upload declares none — so the memory bound is enforced again here, where
        the bytes actually arrive.
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > UPLOAD_MAX_BODY_BYTES:
                raise ErrorException(errors.admin.FILE_TOO_LARGE, status_code=413)
            chunks.append(chunk)
        return b"".join(chunks)

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
    # Transitions
    # ------------------------------------------------------------------

    async def transition_dossier(
        self, dossier_id: int, *, to_status: int, context: dict[str, Any]
    ) -> DossierModel:
        dossier = await self.get_dossier_or_404(dossier_id)
        await self._transition(
            subject_type=SubjectType.DOSSIER,
            subject=dossier,
            dossier=dossier,
            to_status=to_status,
            context=context,
        )
        await self._local.refresh(dossier)
        return dossier

    async def transition_document(
        self, document_id: int, *, to_status: int, context: dict[str, Any]
    ) -> DocumentModel:
        document = await self.get_document_or_404(document_id)
        dossier = await self.get_dossier_or_404(document.id_dossier)
        await self._transition(
            subject_type=SubjectType.DOCUMENT,
            subject=document,
            dossier=dossier,
            to_status=to_status,
            context=context,
        )
        await self._local.refresh(document)
        return document

    async def _transition(
        self,
        *,
        subject_type: SubjectType,
        subject: DossierModel | DocumentModel,
        dossier: DossierModel,
        to_status: int,
        context: dict[str, Any],
    ) -> None:
        from_status = subject.status
        statemachine.assert_transition_allowed(subject_type, from_status, to_status)
        statemachine.assert_requirements(subject_type, from_status, to_status, context)
        corrective = statemachine.is_corrective(subject_type, from_status, to_status)

        clean_context = _jsonable(context)
        event = self._repo.append_status_event(
            subject_type=subject_type,
            subject_id=subject.id,
            from_status=from_status,
            to_status=to_status,
            is_corrective=corrective,
            actor_id=current_user_id.get(),
            context=clean_context,
        )
        subject.status = to_status
        if subject_type == SubjectType.DOSSIER and to_status == int(DossierStatus.SUBMITTED):
            dossier.submitted_at = datetime.datetime.now(datetime.UTC)
        await self._local.flush()

        created = await self._apply_deadline_effects(
            subject_type=subject_type,
            dossier=dossier,
            to_status=to_status,
            context=clean_context,
            event_id=event.id,
        )

        # The deferred constraint trigger validates cache-vs-journal here.
        await self._local.commit()

        metrics.status_transitions.add(
            1,
            {
                "subject_type": subject_type.name,
                "to_status": str(to_status),
                "corrective": str(corrective).lower(),
            },
        )
        await self._publish_transition(subject_type, subject.id, from_status, to_status, corrective)
        for deadline in created:
            await self._publish_deadline(SUBJECT_DEADLINE_CREATED, EVENT_DEADLINE_CREATED, deadline)
        await self._notify_acknowledged(subject_type, subject, dossier, to_status, clean_context)
        await self._audit_and_commit(
            AuditActions.DOSSIER_STATUS_CHANGED
            if subject_type == SubjectType.DOSSIER
            else AuditActions.DOCUMENT_STATUS_CHANGED,
            entity_type="dossier" if subject_type == SubjectType.DOSSIER else "document",
            entity_id=str(subject.id),
            payload={
                "from_status": from_status,
                "to_status": to_status,
                "is_corrective": corrective,
                "context": clean_context,
            },
        )

    # ------------------------------------------------------------------
    # Deadline derivation
    # ------------------------------------------------------------------

    def _triggers_for(
        self,
        *,
        subject_type: SubjectType,
        dossier: DossierModel,
        to_status: int,
        context: dict[str, Any],
    ) -> list[str]:
        """Which rule trigger keys this transition fires."""
        triggers: list[str] = []
        if subject_type == SubjectType.DOSSIER:
            if to_status == int(DossierStatus.SUBMITTED):
                triggers.append(TriggerEvent.DOSSIER_SUBMITTED)
                # A modification dossier reaching "submitted" is also the event
                # that starts the 15-business-day notification clock.
                if dossier.dossier_type == int(DossierType.MODIFICATION):
                    triggers.append(TriggerEvent.DOSSIER_MODIFICATION)
            elif to_status == int(DossierStatus.COMPLETE):
                triggers.append(TriggerEvent.DOSSIER_COMPLETE)
        elif (
            subject_type == SubjectType.DOCUMENT
            and to_status == int(DocumentStatus.ACKNOWLEDGED)
            and context.get("result") == AcknowledgementResult.INCOMPLETE.value
        ):
            # An incomplete acknowledgment starts the 6-month lapse clock on the
            # parent dossier.
            triggers.append(TriggerEvent.DOCUMENT_ACKNOWLEDGED_INCOMPLETE)
        return triggers

    async def _apply_deadline_effects(
        self,
        *,
        subject_type: SubjectType,
        dossier: DossierModel,
        to_status: int,
        context: dict[str, Any],
        event_id: int,
    ) -> list[DeadlineModel]:
        """Derive new deadlines and retire the ones this transition satisfies."""
        # Reaching "complete" answers the lapse threat; closing ends the
        # recurring reporting obligation. Both are cancellations, never deletes.
        if subject_type == SubjectType.DOSSIER:
            if to_status == int(DossierStatus.COMPLETE):
                await self._repo.cancel_open_deadlines(
                    id_dossier=dossier.id, deadline_type=DeadlineType.LAPSE
                )
            elif to_status in (int(DossierStatus.CLOSED), int(DossierStatus.LAPSED)):
                for deadline_type in DeadlineType:
                    await self._repo.cancel_open_deadlines(
                        id_dossier=dossier.id, deadline_type=deadline_type
                    )
                return []

        triggers = self._triggers_for(
            subject_type=subject_type, dossier=dossier, to_status=to_status, context=context
        )
        if not triggers:
            return []

        base_date = _base_date(context)
        before = {d.id for d in await self._repo.list_deadlines_for_dossier(dossier.id)}
        for trigger in triggers:
            rules = await self._repo.matching_rules(
                region=dossier.region,
                dossier_type=dossier.dossier_type,
                trigger_event=trigger,
            )
            derived = deadline_engine.evaluate(rules, base_date, self._calendar)
            if derived:
                await self._repo.create_derived_deadlines(
                    id_dossier=dossier.id, derived=derived, event_id=event_id
                )

        await self._local.flush()
        after = await self._repo.list_deadlines_for_dossier(dossier.id)
        created = [d for d in after if d.id not in before]
        for deadline in created:
            metrics.deadlines_created.add(1, {"deadline_type": deadline.deadline_type})
        return created

    # ------------------------------------------------------------------
    # Deadlines
    # ------------------------------------------------------------------

    async def resolve_deadline(self, deadline_id: int, *, status: DeadlineStatus) -> DeadlineModel:
        if status not in (DeadlineStatus.MET, DeadlineStatus.CANCELLED):
            raise ErrorException(errors.admin.UPDATE_DEADLINE, status_code=422)

        deadline = await self._repo.get_deadline(deadline_id)
        if deadline is None:
            raise ErrorException(errors.admin.DEADLINE_NOT_FOUND, status_code=404)
        if deadline.status != int(DeadlineStatus.OPEN):
            raise ErrorException(errors.admin.UPDATE_DEADLINE, status_code=409)

        deadline.status = int(status)
        deadline.resolved_at = datetime.datetime.now(datetime.UTC)

        # Resolving a recurring occurrence schedules the next one. The roll is
        # anchored on the previous DUE date so an annual obligation keeps its
        # calendar slot instead of drifting later each year.
        if deadline.recurring and status == DeadlineStatus.MET:
            # Flush the closure FIRST: "at most one open recurring occurrence"
            # is a partial unique index, and the unit of work is free to order
            # the INSERT before the UPDATE, which would transiently violate it.
            await self._local.flush()
            await self._roll_recurring(deadline)

        await self._local.commit()
        await self._local.refresh(deadline)
        await self._audit_and_commit(
            AuditActions.DEADLINE_RESOLVED,
            entity_type="deadline",
            entity_id=str(deadline.id),
            payload={"status": int(status), "deadline_type": deadline.deadline_type},
        )
        return deadline

    async def _roll_recurring(self, deadline: DeadlineModel) -> None:
        rule = (
            await self._repo.get_deadline_rule(deadline.id_deadline_rule)
            if deadline.id_deadline_rule
            else None
        )
        if rule is None or not rule.recur_months:
            logger.warning("recurring deadline %s has no usable rule; not rolling", deadline.id)
            return
        next_due = deadline_engine.next_occurrence(
            deadline.due_date, rule.recur_months, self._calendar
        )
        self._repo.add_recurring_occurrence(
            id_dossier=deadline.id_dossier,
            deadline_type=deadline.deadline_type,
            due_date=next_due,
            id_deadline_rule=rule.id,
        )

    async def sweep_deadlines(self, today: datetime.date | None = None) -> tuple[int, int]:
        """Flip past-due open deadlines to missed, roll recurring ones, and
        remind on the ones coming up.

        Time-based, so it is driven by a scheduler rather than a user action —
        the same shape as the platform's other periodic sweeps.

        The MISSED pass runs FIRST and the reminder query filters on
        ``due_date >= as_of``, so a deadline cannot collect a "missed" and a "due
        soon" message from the same sweep.
        """
        as_of = today or datetime.datetime.now(BRUSSELS).date()
        overdue = await self._repo.open_deadlines_past_due(as_of)
        rolled = 0
        for deadline in overdue:
            deadline.status = int(DeadlineStatus.MISSED)
            deadline.resolved_at = datetime.datetime.now(datetime.UTC)
            metrics.deadlines_missed.add(1, {"deadline_type": deadline.deadline_type})
            if deadline.recurring:
                # See resolve_deadline: close before opening the next occurrence.
                await self._local.flush()
                await self._roll_recurring(deadline)
                rolled += 1

        upcoming = await self._repo.open_deadlines_due_within(
            as_of, settings.DEADLINE_REMINDER_DAYS
        )
        for deadline in upcoming:
            # Stamped BEFORE the local commit, in the same transaction as the
            # MISSED writes, which makes the reminder at-most-once — exactly the
            # semantics `missed` already has. A reminder that a re-run sweep
            # re-sends is worse than no reminder.
            deadline.reminded_at = datetime.datetime.now(datetime.UTC)
            await self._notify.publish(
                type=NotificationTypes.ADMIN_DEADLINE_DUE_SOON,
                target=CommunityTarget(community_id=deadline.id_community, roles=MANAGER_ROLES),
                # INFORMATIONAL, unlike `missed`: a reminder is exactly the kind
                # of message a manager must be able to mute (§1.6), and it is the
                # only email in Phase 1 that consults notification_preference.
                category=NotificationCategory.INFORMATIONAL,
                channels=(Channel.INAPP, Channel.EMAIL),
                data={
                    "deadline_id": deadline.id,
                    "dossier_id": deadline.id_dossier,
                    "deadline_type": deadline.deadline_type,
                    "due_date": deadline.due_date.isoformat(),
                    "as_of": as_of.isoformat(),
                },
                # The only explicit dedupe key in the platform. This sweep does
                # NOT mutate what it reads (unlike the MISSED pass, which flips a
                # status), so the derived key — a hash of `data` — could not tell
                # two occurrences of the same deadline apart. The occurrence date
                # has to be in the key. `reminded_at` handles the in-app half.
                dedupe_key=(
                    f"{NotificationTypes.ADMIN_DEADLINE_DUE_SOON}:"
                    f"{deadline.id}:{deadline.due_date.isoformat()}"
                ),
            )

        if upcoming and not overdue:
            await self._stage_audit(
                AuditActions.DEADLINE_REMINDED,
                entity_type="deadline",
                entity_id=None,
                payload={"reminded": len(upcoming), "as_of": as_of.isoformat()},
            )
            await self._crm.commit()

        if overdue:
            # One notification per deadline so each carries its own dossier for
            # the frontend's per-row deep link. TRANSACTIONAL is load-bearing: a
            # missed regulatory deadline has legal consequences, so it is not
            # something a manager may silently opt out of.
            for deadline in overdue:
                await self._notify.publish(
                    type=NotificationTypes.ADMIN_DEADLINE_MISSED,
                    target=CommunityTarget(community_id=deadline.id_community, roles=MANAGER_ROLES),
                    category=NotificationCategory.TRANSACTIONAL,
                    channels=(Channel.INAPP, Channel.EMAIL),
                    data={
                        "deadline_id": deadline.id,
                        "dossier_id": deadline.id_dossier,
                        "deadline_type": deadline.deadline_type,
                        "due_date": deadline.due_date.isoformat(),
                    },
                )
            await self._stage_audit(
                AuditActions.DEADLINE_MISSED,
                entity_type="deadline",
                entity_id=None,
                payload={
                    "missed": len(overdue),
                    "rolled": rolled,
                    "reminded": len(upcoming),
                    "as_of": as_of.isoformat(),
                },
            )
            # CRM BEFORE local, and unguarded, and the ordering is load-bearing.
            # Once a deadline says MISSED no later sweep matches it again, so
            # committing locally first and then swallowing a CRM failure loses
            # the notification and its email permanently, with nothing left to
            # re-emit them. This way a crash between the two commits leaves the
            # deadlines still OPEN, the next sweep re-flips and re-publishes, and
            # `outbound_message.dedupe_key` collapses the duplicate email.
            #
            # Safe here specifically because `deadline` carries no DEFERRED
            # journal trigger — only `dossier` and `document` do — so the local
            # commit cannot fail a constraint check after the CRM side is
            # durable. Do not copy this ordering onto a journaled path.
            await self._crm.commit()
        await self._local.commit()

        for deadline in overdue:
            await self._publish_deadline(SUBJECT_DEADLINE_MISSED, EVENT_DEADLINE_MISSED, deadline)
        return len(overdue), rolled

    # ------------------------------------------------------------------
    # Events + audit
    # ------------------------------------------------------------------

    async def _publish_transition(
        self,
        subject_type: SubjectType,
        subject_id: int,
        from_status: int,
        to_status: int,
        corrective: bool,
    ) -> None:
        subject_is_dossier = subject_type == SubjectType.DOSSIER
        subject = (
            SUBJECT_DOSSIER_STATUS_CHANGED
            if subject_is_dossier
            else SUBJECT_DOCUMENT_STATUS_CHANGED
        )
        await self._safe_publish(
            subject,
            Event(
                type=EVENT_DOSSIER_STATUS_CHANGED
                if subject_is_dossier
                else EVENT_DOCUMENT_STATUS_CHANGED,
                data={
                    "subject_type": int(subject_type),
                    "subject_id": subject_id,
                    "from_status": from_status,
                    "to_status": to_status,
                    "is_corrective": corrective,
                },
            ),
        )

    async def _publish_deadline(
        self, subject: str, event_type: str, deadline: DeadlineModel
    ) -> None:
        await self._safe_publish(
            subject,
            Event(
                type=event_type,
                data={
                    "deadline_id": deadline.id,
                    "id_dossier": deadline.id_dossier,
                    "deadline_type": deadline.deadline_type,
                    "due_date": deadline.due_date.isoformat(),
                },
            ),
        )

    async def _safe_publish(self, subject: str, event: Event) -> None:
        """Publish without ever failing the caller.

        The transition is already committed and journaled; a broker outage must
        not turn a recorded regulatory act into an HTTP 500.
        """
        try:
            await self._publisher.publish(subject, event)
        except Exception:
            logger.exception("failed to publish %s on %s", event.type, subject)

    async def _notify_acknowledged(
        self,
        subject_type: SubjectType,
        subject: DossierModel | DocumentModel,
        dossier: DossierModel,
        to_status: int,
        context: dict[str, Any],
    ) -> None:
        """Tell the managers a document came back acknowledged by the regulator.

        ``_transition`` is the funnel for every status change, so this guards on
        the one edge that notifies and returns for all the others.

        Called after the local commit (the journal is durable and the deferred
        constraint trigger has passed) and before ``_audit_and_commit``, so the
        notification rides the same CRM transaction as the audit row. Do NOT move
        it earlier: the CRM is a different database, so there is no atomicity to
        win — only a wider window in which the deferred trigger aborts the local
        commit with CRM rows already staged.
        """
        if subject_type != SubjectType.DOCUMENT or to_status != int(DocumentStatus.ACKNOWLEDGED):
            return
        await self._notify.publish(
            type=NotificationTypes.ADMIN_DOSSIER_ACKNOWLEDGED,
            target=CommunityTarget(community_id=dossier.id_community, roles=MANAGER_ROLES),
            category=NotificationCategory.INFORMATIONAL,
            channels=(Channel.INAPP,),
            data={
                "document_id": subject.id,
                "dossier_id": dossier.id,
                "dossier_type": dossier.dossier_type,
                "result": context.get("result"),
            },
        )

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
        already durable and a CRM hiccup must not turn a successful transition
        into a 500. It is NOT right where the same transaction also carries a
        notification that nothing will ever re-emit — `sweep_deadlines` commits
        explicitly for exactly that reason.
        """
        await self._stage_audit(
            action, entity_type=entity_type, entity_id=entity_id, payload=payload
        )
        try:
            await self._crm.commit()
        except Exception:
            logger.exception("audit commit failed for %s %s", entity_type, entity_id)


def _jsonable(context: dict[str, Any]) -> dict[str, Any]:
    """Make a transition context safe for a JSONB column.

    Dates arrive as ``datetime.date`` from the typed request bodies; JSONB needs
    them as ISO strings, and storing them that way keeps the journal readable.
    """
    cleaned: dict[str, Any] = {}
    for key, value in (context or {}).items():
        if isinstance(value, datetime.datetime | datetime.date):
            cleaned[key] = value.isoformat()
        else:
            cleaned[key] = value
    return cleaned


def _base_date(context: dict[str, Any]) -> datetime.date:
    """The date a deadline clock starts from.

    Prefer what the actor declared (``submission_date`` /
    ``acknowledged_date``): legally the clock runs from the act, not from when
    somebody got around to recording it. Fall back to today in Brussels.
    """
    for key in ("submission_date", "acknowledged_date"):
        raw = context.get(key)
        if isinstance(raw, str):
            try:
                return datetime.date.fromisoformat(raw)
            except ValueError:
                logger.warning("unparseable %s in transition context: %r", key, raw)
        elif isinstance(raw, datetime.date):
            return raw
    return datetime.datetime.now(BRUSSELS).date()
