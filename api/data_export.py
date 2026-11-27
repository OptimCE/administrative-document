"""The administrative-document part of a data export (monorepo contracts/export/README.md, §7).

User scope — what has been filed about the requester, as the app tells them:
- ``filings.json`` is ``/filings/mine`` itself (``AdministrativeDocumentService.my_filings`` and
  its mapper), in every community where a member is linked to them, run under the context
  variables the gateway would set. A filing carries only their own rows out of the frozen
  snapshot (decision B4), never anybody else's.
- ``member-pieces.json``: the pieces addressed to one of their members (``document.id_member``,
  an annex 8...), and the file of such a piece once its current version is FINAL. A
  community-wide form is never handed over: its file names every member.

Community scope (an ADMIN, re-checked in crm_db by the kit): every row this annex stores for
the community, table by table (contract §6), the stored files by reference (versions,
correspondence, received mails and their attachments). Codes are written by name.

The module's subscription is not checked: an export is not a use of the module. A file whose
object is gone, or that lives in a bucket the export worker cannot read, stays described with
``file_missing: true`` and the ``files_missing`` counter instead of failing every export.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.administrative_document.mappers import to_my_filing_out
from api.administrative_document.repository import AdministrativeDocumentRepository
from api.administrative_document.service import AdministrativeDocumentService
from core.config import settings
from core.context_vars import current_internal_community_id, current_user_id
from core.export import (
    ExportHandler,
    ExportStorageSettings,
    PartContext,
    Requester,
    S3PartStore,
    SqlCrmLookup,
    resolve_requester,
    safe_file_name,
)
from core.storage import parse_s3_uri
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from ports.events import NoopEventPublisher
from regulators.registry import REGISTRY
from shared.const import (
    ChangeStatus,
    ChangeSubjectType,
    Channel,
    CheckOutcome,
    CorrespondenceDirection,
    Counterparty,
    DeadlineKind,
    DeadlineParty,
    DeadlineStatus,
    DigestCadence,
    DocOrigin,
    DocumentKind,
    InboundMailStatus,
    OpenedBy,
    PieceSignatureStatus,
    ProcedureDocumentStatus,
    ProcedureScope,
    ProcedureStatus,
    Region,
    SubjectType,
    VersionStage,
    extension_for_content_type,
)
from shared.models.local_models import (
    ChangeModel,
    CorrespondenceModel,
    DeadlineModel,
    DigestSettingModel,
    DocumentModel,
    DocumentVersionModel,
    DossierModel,
    InboundAddressModel,
    InboundMailAttachmentModel,
    InboundMailModel,
    PieceSignatureModel,
    ProcedureDefinitionModel,
    StatusEventModel,
    VersionCheckModel,
)

logger = logging.getLogger(__name__)

SERVICE = "administrative-document"
PAGE = 500
# /filings/mine caps a page at 200; the export takes them all.
ALL_FILINGS = 1_000_000

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
# The size of an object (bucket, key), None when there is none.
SizeOf = Callable[[str, str], Awaitable[int | None]]


# ---- what a community export holds -----------------------------------------------------------


def _version_path(row: Any) -> str:
    name = row.original_filename or (
        f"version-{row.version_no}{extension_for_content_type(row.content_type)}"
    )
    return f"files/versions/{row.id}-{safe_file_name(name)}"


def _correspondence_path(row: Any) -> str:
    name = row.original_filename or (
        f"correspondence{extension_for_content_type(row.content_type)}"
    )
    return f"files/correspondence/{row.id}-{safe_file_name(name)}"


def _mail_path(row: Any) -> str:
    return f"files/inbound-mails/{row.id}.eml"


def _attachment_path(row: Any) -> str:
    return (
        f"files/inbound-mails/{row.id_inbound_mail}/{row.position}-{safe_file_name(row.filename)}"
    )


@dataclass(frozen=True)
class FileSpec:
    """A stored file a row points to: its ``s3://`` column and its place in the archive."""

    column: str
    path: Callable[[Any], str]
    content_type: str | None = None


@dataclass(frozen=True)
class Table:
    """One exported table: its rows, minus ``id_community`` (the scope), codes by name."""

    entity: str
    model: type[Any]
    columns: tuple[str, ...]
    enums: Mapping[str, type[IntEnum]] = field(default_factory=dict)
    file: FileSpec | None = None
    # Computed from a reference table, never stored on the row (name -> JSON type).
    extras: Mapping[str, str] = field(default_factory=dict)
    key: str = "id"


TABLES: tuple[Table, ...] = (
    Table(
        "procedures",
        DossierModel,
        (
            "id",
            "id_sharing_operation",
            "status",
            "step",
            "region",
            "external_ref",
            "title",
            "submitted_at",
            "metadata_json",
            "id_procedure_definition",
            "regulator",
            "family",
            "scope",
            "grd_ref",
            "variant",
            "requested_effective_date",
            "effective_date",
            "opened_by",
            "frozen_at",
            "period_key",
            "next_action_code",
            "next_action_due",
            "effect_error_json",
            "created_at",
            "updated_at",
        ),
        {
            "status": ProcedureStatus,
            "region": Region,
            "scope": ProcedureScope,
            "opened_by": OpenedBy,
        },
        extras={"procedure_code": "string", "procedure_version": "integer"},
    ),
    Table(
        "documents",
        DocumentModel,
        (
            "id",
            "id_dossier",
            "doc_type",
            "origin",
            "status",
            "current_version_id",
            "title",
            "kind",
            "required",
            "requirement_code",
            "condition_json",
            "id_member",
            "id_template",
            "created_at",
            "updated_at",
        ),
        {"origin": DocOrigin, "status": ProcedureDocumentStatus, "kind": DocumentKind},
    ),
    Table(
        "document-versions",
        DocumentVersionModel,
        (
            "id",
            "id_document",
            "version_no",
            "content_sha256",
            "content_type",
            "byte_size",
            "original_filename",
            "id_template",
            "data_snapshot_json",
            "generated_by",
            "docgen_request_id",
            "stage",
            "watermark",
            "prefill_json",
            "template_version",
            "renderer_json",
            "created_at",
            "immutable_from",
        ),
        {"stage": VersionStage},
        file=FileSpec("file_ref", _version_path),
    ),
    Table(
        "version-checks",
        VersionCheckModel,
        (
            "id",
            "id_document_version",
            "docgen_request_id",
            "outcome",
            "content_sha256",
            "renderer_json",
            "error_json",
            "requested_by",
            "requested_at",
            "checked_at",
        ),
        {"outcome": CheckOutcome},
    ),
    Table(
        "journal",
        StatusEventModel,
        (
            "id",
            "subject_type",
            "subject_id",
            "from_status",
            "to_status",
            "from_step",
            "to_step",
            "is_corrective",
            "actor_id",
            "occurred_at",
            "context_json",
        ),
        # from/to_status stay codes: their meaning depends on subject_type, and the history
        # keeps codes retired since (July's).
        {"subject_type": SubjectType},
    ),
    Table(
        "deadlines",
        DeadlineModel,
        (
            "id",
            "id_dossier",
            "deadline_type",
            "due_date",
            "status",
            "recurring",
            "derived_from_event_id",
            "id_deadline_rule",
            "resolved_at",
            "reminded_at",
            "reminders_sent",
            "party",
            "kind",
            "derived_from_correspondence_id",
            "created_at",
            "updated_at",
        ),
        {"status": DeadlineStatus, "party": DeadlineParty, "kind": DeadlineKind},
    ),
    Table(
        "changes",
        ChangeModel,
        (
            "id",
            "id_sharing_operation",
            "kind",
            "subject_type",
            "subject_ref",
            "payload_json",
            "source_event_id",
            "procedure_code",
            "status",
            "id_dossier",
            "detected_at",
            "created_at",
            "updated_at",
        ),
        {"subject_type": ChangeSubjectType, "status": ChangeStatus},
    ),
    Table(
        "correspondence",
        CorrespondenceModel,
        (
            "id",
            "id_dossier",
            "direction",
            "counterparty",
            "channel",
            "type",
            "occurred_at",
            "subject",
            "body_text",
            "external_ref",
            "content_sha256",
            "content_type",
            "byte_size",
            "original_filename",
            "context_json",
            "recorded_by",
            "created_at",
        ),
        {
            "direction": CorrespondenceDirection,
            "counterparty": Counterparty,
            "channel": Channel,
        },
        file=FileSpec("file_ref", _correspondence_path),
    ),
    Table(
        "inbound-addresses",
        InboundAddressModel,
        ("id", "token", "created_at", "created_by", "revoked_at"),
    ),
    Table(
        "inbound-mails",
        InboundMailModel,
        (
            "id",
            "id_inbound_address",
            "message_id",
            "content_sha256",
            "byte_size",
            "from_address",
            "subject",
            "sent_at",
            "body_text",
            "received_at",
            "status",
            "id_dossier",
            "match_reason",
            "suggested_response",
            "id_correspondence",
            "handled_by",
            "handled_at",
            "updated_at",
        ),
        {"status": InboundMailStatus},
        file=FileSpec("file_ref", _mail_path, content_type="message/rfc822"),
    ),
    Table(
        "inbound-mail-attachments",
        InboundMailAttachmentModel,
        (
            "id",
            "id_inbound_mail",
            "position",
            "filename",
            "content_type",
            "byte_size",
            "content_sha256",
            "created_at",
        ),
        file=FileSpec("file_ref", _attachment_path),
    ),
    Table(
        "digest-settings",
        DigestSettingModel,
        (
            "enabled",
            "cadence",
            "weekday",
            "first_on",
            "recipient_ids",
            "enabled_at",
            "period_from",
            "last_due_on",
            "updated_at",
            "updated_by",
        ),
        {"cadence": DigestCadence},
        key="id_community",
    ),
    Table(
        "piece-signatures",
        PieceSignatureModel,
        (
            "id",
            "id_document",
            "id_version",
            "idempotency_key",
            "request_id",
            "status",
            "reason",
            "id_signed_version",
            "final_sha256",
            "requested_by",
            "requested_at",
            "updated_at",
        ),
        {"status": PieceSignatureStatus},
    ),
)

# The counters index.html shows, per entity written.
COMMUNITY_COUNTERS = {
    "procedures": "procedures",
    "documents": "documents",
    "document-versions": "versions",
    "correspondence": "correspondence",
    "inbound-mails": "inbound_mails",
}


def code(value: Any, enum: type[IntEnum] | None) -> Any:
    """A stored value as the archive writes it: a code by its name, a UUID as text."""
    if value is None:
        return None
    if enum is not None:
        try:
            return enum(int(value)).name
        except ValueError:
            # A code retired since keeps its number rather than failing the export.
            return int(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


# ---- the part ---------------------------------------------------------------------------------


class _Part:
    def __init__(
        self,
        ctx: PartContext,
        *,
        local: AsyncSession,
        crm: AsyncSession,
        size_of: SizeOf,
        buckets: frozenset[str],
    ) -> None:
        self.ctx = ctx
        self.local = local
        self.crm = crm
        self.size_of = size_of
        self.buckets = buckets
        self.files = 0
        self.missing = 0

    async def file(
        self, uri: str | None, path: str, content_type: str | None
    ) -> dict[str, Any] | None:
        """References a stored object, or describes it as missing. None when the row has none."""
        if not uri:
            return None
        self.ctx.checkpoint()
        try:
            bucket, key = parse_s3_uri(uri)
        except ValueError:
            bucket, key = "", ""
        size = await self.size_of(bucket, key) if bucket in self.buckets and key else None
        if size is None:
            self.missing += 1
            return {"path": None, "file_missing": True}
        self.ctx.add_reference(
            bucket=bucket, key=key, path=path, size=size, content_type=content_type
        )
        self.files += 1
        return {"path": f"{SERVICE}/{path}", "file_missing": False}

    def finish(self) -> None:
        self.ctx.count("files", self.files)
        if self.missing:
            self.ctx.count("files_missing", self.missing)

    # ---- community scope ---------------------------------------------------------------------

    async def community_export(self, id_community: int) -> None:
        definitions = {
            row.id: (row.code, row.version)
            for row in (await self.local.execute(select(ProcedureDefinitionModel))).scalars()
        }
        for table in TABLES:
            written = await self.ctx.write_json(
                table.entity, self._rows(table, id_community, definitions)
            )
            if table.entity in COMMUNITY_COUNTERS:
                self.ctx.count(COMMUNITY_COUNTERS[table.entity], written)
        self.finish()

    async def _rows(
        self,
        table: Table,
        id_community: int,
        definitions: Mapping[int, tuple[str, int]],
    ) -> AsyncIterator[dict[str, Any]]:
        key = getattr(table.model, table.key)
        last: Any = None
        while True:
            self.ctx.checkpoint()
            query = select(table.model).where(table.model.id_community == id_community)
            if last is not None:
                query = query.where(key > last)
            rows = list((await self.local.execute(query.order_by(key).limit(PAGE))).scalars())
            for row in rows:
                item = {
                    name: code(getattr(row, name), table.enums.get(name)) for name in table.columns
                }
                if table.extras:
                    procedure = definitions.get(row.id_procedure_definition)
                    item["procedure_code"] = procedure[0] if procedure else None
                    item["procedure_version"] = procedure[1] if procedure else None
                if table.file is not None:
                    content_type = table.file.content_type or getattr(row, "content_type", None)
                    item["file"] = await self.file(
                        getattr(row, table.file.column), table.file.path(row), content_type
                    )
                yield item
            if len(rows) < PAGE:
                return
            last = getattr(rows[-1], table.key)

    # ---- user scope --------------------------------------------------------------------------

    async def user_export(self, requester: Requester) -> None:
        member_ids = list(requester.member_ids)
        communities: dict[int, str | None] = {}
        if member_ids:
            result = await self.crm.execute(
                text(
                    "SELECT DISTINCT m.id_community, c.name FROM member m "
                    "JOIN community c ON c.id = m.id_community "
                    "WHERE m.id = ANY(:ids) ORDER BY m.id_community"
                ),
                {"ids": member_ids},
            )
            communities = {int(row[0]): row[1] for row in result}

        filings: list[dict[str, Any]] = []
        for id_community, name in communities.items():
            self.ctx.checkpoint()
            filings.extend(
                {"id_community": id_community, "community": name, **filing}
                for filing in await self._my_filings(requester.auth_user_id, id_community)
            )
        self.ctx.count("filings", await self.ctx.write_json("filings", filings))

        pieces = await self._member_pieces(member_ids, communities)
        self.ctx.count("member_pieces", await self.ctx.write_json("member-pieces", pieces))
        self.finish()

    async def _my_filings(self, auth_user_id: str, id_community: int) -> list[dict[str, Any]]:
        """``/filings/mine`` in one community, under the context variables the gateway sets."""
        user_token = current_user_id.set(auth_user_id)
        community_token = current_internal_community_id.set(id_community)
        try:
            service = AdministrativeDocumentService(
                local_session=self.local,
                crm_session=self.crm,
                repository=AdministrativeDocumentRepository(self.local),
                crm_read=SqlAlchemyCrmCoreRead(self.crm),
                publisher=NoopEventPublisher(),
                registry=REGISTRY,
            )
            found = await service.my_filings(id_community=id_community, limit=ALL_FILINGS)
            return [to_my_filing_out(filing).model_dump(mode="json") for filing in found]
        finally:
            current_internal_community_id.reset(community_token)
            current_user_id.reset(user_token)

    async def _member_pieces(
        self, member_ids: list[int], communities: Mapping[int, str | None]
    ) -> list[dict[str, Any]]:
        if not member_ids:
            return []
        rows = (
            await self.local.execute(
                select(DocumentModel, DossierModel, DocumentVersionModel)
                .join(DossierModel, DossierModel.id == DocumentModel.id_dossier)
                .outerjoin(
                    DocumentVersionModel,
                    DocumentVersionModel.id == DocumentModel.current_version_id,
                )
                .where(DocumentModel.id_member.in_(member_ids))
                .order_by(DocumentModel.id)
            )
        ).all()
        out: list[dict[str, Any]] = []
        for document, dossier, version in rows:
            final = version is not None and version.stage == VersionStage.FINAL
            out.append(
                {
                    "id_community": document.id_community,
                    "community": communities.get(document.id_community),
                    "id_member": document.id_member,
                    "dossier": {
                        "id": dossier.id,
                        "status": code(dossier.status, ProcedureStatus),
                        "family": dossier.family,
                        "step": dossier.step,
                        "title": dossier.title,
                        "external_ref": dossier.external_ref,
                        "submitted_at": _utc(dossier.frozen_at or dossier.submitted_at),
                    },
                    "document": {
                        "id": document.id,
                        "doc_type": document.doc_type,
                        "status": code(document.status, ProcedureDocumentStatus),
                        "title": document.title,
                    },
                    "version": (
                        {
                            "id": version.id,
                            "version_no": version.version_no,
                            "stage": code(version.stage, VersionStage),
                            "content_sha256": version.content_sha256,
                            "created_at": _utc(version.created_at),
                        }
                        if version is not None
                        else None
                    ),
                    # Only a FINAL version is the piece of record; a draft or a form to sign
                    # is not what was filed.
                    "file": (
                        await self.file(
                            version.file_ref, _version_path(version), version.content_type
                        )
                        if final and version is not None
                        else None
                    ),
                }
            )
        return out


def _utc(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


# ---- wiring -----------------------------------------------------------------------------------


def export_handler(
    *,
    local: SessionFactory,
    crm: SessionFactory,
    size_of: SizeOf,
    buckets: frozenset[str],
) -> ExportHandler:
    """The handler the kit calls for each part of this service."""

    async def handle(ctx: PartContext) -> None:
        async with crm() as crm_session, local() as local_session:
            requester = await resolve_requester(SqlCrmLookup(crm_session), ctx.request)
            part = _Part(
                ctx, local=local_session, crm=crm_session, size_of=size_of, buckets=buckets
            )
            if requester.community_id is None:
                await part.user_export(requester)
            else:
                await part.community_export(requester.community_id)

    return handle


async def _s3_size(bucket: str, key: str) -> int | None:
    from core.storage import size

    return await size(key, bucket=bucket)


def start_export_consumer(stop: asyncio.Event) -> asyncio.Task[None] | None:
    """Starts this service's export consumer, or logs why it cannot (and the API runs on).

    Needs the export account (EXPORT_STORAGE_*), never the service's usual storage account.
    """
    from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
    from core.export.consumer import run_export_consumer
    from core.queue.init import get_jetstream

    try:
        storage = ExportStorageSettings.from_env()
        js = get_jetstream()
    except RuntimeError as err:
        logger.warning(
            "Data export disabled: %s",
            err,
            extra={"operation": "export:consumer_disabled", "service": SERVICE},
        )
        return None
    handler = export_handler(
        local=AsyncSessionLocalFactory,
        crm=AsyncSessionCRMFactory,
        size_of=_s3_size,
        # The buckets the export worker may read (its EXPORT_DOCUMENT_BUCKETS).
        buckets=frozenset({settings.OUTPUT_BUCKET, settings.STORAGE_BUCKET}),
    )
    return asyncio.create_task(
        run_export_consumer(
            js,
            service=SERVICE,
            handler=handler,
            store=S3PartStore(storage),
            stop=stop,
            expected_bucket=storage.bucket,
        ),
        name="administrative-document-export-consumer",
    )


async def stop_export_consumer(
    task: asyncio.Task[None] | None, stop: asyncio.Event, grace: float = 10.0
) -> None:
    """Stops pulling; a part in progress is aborted and nak'ed (redelivered later)."""
    stop.set()
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=grace)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
