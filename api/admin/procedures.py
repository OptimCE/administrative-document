"""The ``/admin`` procedure use cases: reading the dashboard, acting on one.

Each action loads the procedure within the caller's community, checks what the
spec forbids before touching anything, works through ``engine.lifecycle`` and
``engine.filing`` (the same operations the router uses), and commits once.

The rules enforced here:

- after ``frozen_at`` (the first sending) a procedure takes no change and no
  new version of a piece it was sent with; a received piece still arrives;
- a step change is one of the catalogue's edges from the current step, for
  the procedure's variant;
- ``dry_run`` on a response computes its consequences and writes nothing;
- a change deferred to "the next procedure" moves into the family's next
  draft once this one is sent or cancelled.
"""

from __future__ import annotations

import datetime
import hashlib
from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin import views
from core.audit_log import AuditActions, AuditLogInput, AuditLogService
from core.config import settings
from core.errors.errors import ErrorException
from core.storage import build_s3_uri, download, parse_s3_uri, upload
from domain.regions import region_for_regulator
from engine.activation import reconcile_activation
from engine.baseline import brussels_today, ensure_baseline
from engine.calendar import WorkingCalendar
from engine.catalogue import ProcedureDefinition, response_event, step_event
from engine.contracts import PluginRegistry, RegulatorPlugin, plugin_for
from engine.deadlines import due_date
from engine.events import BRUSSELS
from engine.filing import (
    OPEN,
    RegionOf,
    cancel_if_emptied,
    content_changed,
    context_of_procedure,
    recompute,
    refile_change,
)
from engine.lifecycle import (
    UNRESOLVED,
    ProcedureContext,
    close_rules,
    context_for,
    move_to_step,
    open_procedure,
    start_rules,
    supply_piece,
)
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from ports.crm_engine import EngineCrmRead
from shared.const import (
    STORAGE_KEY_PREFIX,
    UPLOAD_MAX_BODY_BYTES,
    ChangeStatus,
    Channel,
    CorrespondenceDirection,
    Counterparty,
    DocOrigin,
    DocumentKind,
    OpenedBy,
    ProcedureDocumentStatus,
    ProcedureStatus,
    SubjectType,
)
from shared.custom_errors import errors
from shared.models.local_models import (
    ChangeModel,
    CorrespondenceModel,
    DeadlineModel,
    DocumentModel,
    DocumentVersionModel,
    DossierModel,
    ProcedureDefinitionModel,
    StatusEventModel,
)

if TYPE_CHECKING:  # the worker image has no starlette; this module is API-only anyway
    from fastapi import UploadFile

_READ_CHUNK_BYTES = 1024 * 1024
# Entering a step of one of these releases the changes deferred out of it.
_RELEASES_DEFERRED = (int(ProcedureStatus.SENT), int(ProcedureStatus.CANCELLED))


def _not_found(error=errors.admin.PROCEDURE_NOT_FOUND) -> ErrorException:
    return ErrorException(error, status_code=404)


def _conflict(error) -> ErrorException:
    return ErrorException(error, status_code=409)


class ProcedureService:
    def __init__(
        self,
        *,
        local_session: AsyncSession,
        crm_session: AsyncSession,
        crm_read: EngineCrmRead,
        registry: PluginRegistry,
        region_of: RegionOf = region_for_regulator,
    ) -> None:
        self._local = local_session
        self._crm = crm_session
        self._crm_read = crm_read
        self._registry = registry
        self._region_of = region_of
        self._calendars: dict[str, WorkingCalendar] = {}

    # -- plumbing -------------------------------------------------------------

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    def _calendar(self, plugin: RegulatorPlugin) -> WorkingCalendar:
        if plugin.code not in self._calendars:
            self._calendars[plugin.code] = WorkingCalendar(plugin.calendar())
        return self._calendars[plugin.code]

    async def _operation_names(self, id_community: int) -> dict[int, str]:
        operations = await SqlAlchemyCrmCoreRead(self._crm).list_sharing_operations(id_community)
        return {op.id: op.name for op in operations}

    async def _plugin(self, id_community: int) -> tuple[RegulatorPlugin | None, str | None]:
        state = await self._crm_read.module_state(id_community)
        return plugin_for(self._registry, state.regulator), state.regulator

    async def _definitions(self, ids: Sequence[int]) -> dict[int, tuple[str, int]]:
        if not ids:
            return {}
        rows = await self._local.execute(
            select(
                ProcedureDefinitionModel.id,
                ProcedureDefinitionModel.code,
                ProcedureDefinitionModel.version,
            ).where(ProcedureDefinitionModel.id.in_(set(ids)))
        )
        return {i: (code, version) for i, code, version in rows.tuples()}

    def _definition(
        self, plugin: RegulatorPlugin, ref: tuple[str, int] | None
    ) -> ProcedureDefinition | None:
        if ref is None:
            return None
        entry = plugin.catalogue().get(*ref)
        return entry.definition if entry else None

    async def _load(
        self, id_community: int, dossier_id: int
    ) -> tuple[DossierModel, RegulatorPlugin, ProcedureContext]:
        dossier = await self._local.scalar(
            select(DossierModel).where(
                DossierModel.id == dossier_id,
                DossierModel.id_community == id_community,
                DossierModel.id_procedure_definition.is_not(None),
            )
        )
        if dossier is None:
            raise _not_found()
        plugin = plugin_for(self._registry, dossier.regulator)
        if plugin is None:
            raise _not_found()
        return dossier, plugin, await context_of_procedure(self._local, plugin, dossier)

    async def _audit(self, action: str, dossier: DossierModel, payload: dict[str, Any]) -> None:
        await AuditLogService(self._crm).log(
            AuditLogInput(
                action=action, entity_type="dossier", entity_id=str(dossier.id), payload=payload
            ),
            id_community=dossier.id_community,
        )
        await self._crm.commit()

    # -- reads ------------------------------------------------------------------

    async def status(self, id_community: int) -> dict[str, Any]:
        now = self._now()
        state = await self._crm_read.module_state(id_community)
        period = await reconcile_activation(self._local, id_community, state, now=now)
        plugin = plugin_for(self._registry, state.regulator)
        if period is not None and plugin is not None:
            await ensure_baseline(
                self._local, self._crm_read, plugin, period, now=now, region_of=self._region_of
            )
        await self._local.commit()
        baseline = (period.baseline_json or {}) if period is not None else {}
        return {
            "active": period is not None,
            "regulator": state.regulator,
            "supported": plugin is not None,
            "tracking_since": period.enabled_at if period is not None else None,
            "baseline": (
                None
                if not baseline
                else ("confirmed" if period and period.baseline_confirmed_at else "pending")
            ),
            "id_baseline_dossier": baseline.get("id_dossier"),
        }

    async def definitions(self, id_community: int) -> list[dict[str, Any]]:
        plugin, _ = await self._plugin(id_community)
        if plugin is None:
            return []
        today = brussels_today(self._now())
        return [views.definition_out(e, today=today) for e in plugin.catalogue().entries]

    async def inbox(
        self,
        id_community: int,
        *,
        operation: int | None = None,
        grd: str | None = None,
        bucket: str | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        today = brussels_today(now)
        plugin, _ = await self._plugin(id_community)
        groups: dict[str, list[dict[str, Any]]] = {b: [] for b in views.BUCKETS}
        if plugin is None:
            return {"today": today, "banner": [], "groups": groups}
        calendar = self._calendar(plugin)

        recent = now - datetime.timedelta(days=views.RECENT_DAYS)
        stmt = select(DossierModel).where(
            DossierModel.id_community == id_community,
            DossierModel.id_procedure_definition.is_not(None),
            DossierModel.regulator == plugin.code,
            (DossierModel.status.not_in(views.FINISHED)) | (DossierModel.updated_at >= recent),
        )
        if operation is not None:
            stmt = stmt.where(DossierModel.id_sharing_operation == operation)
        if grd is not None:
            stmt = stmt.where(DossierModel.grd_ref == grd)
        dossiers = list(await self._local.scalars(stmt.order_by(DossierModel.id)))
        ids = [d.id for d in dossiers]
        definitions = await self._definitions([d.id_procedure_definition or 0 for d in dossiers])
        names = await self._operation_names(id_community)
        changes = await self._active_change_counts(ids)
        pieces = await self._pieces_by_dossier(ids)

        by_id = {}
        for dossier in dossiers:
            definition = self._definition(
                plugin, definitions.get(dossier.id_procedure_definition or 0)
            )
            if definition is None:
                continue
            by_id[dossier.id] = (dossier, definition)
            row = views.item(
                dossier,
                definition,
                calendar,
                today=today,
                operation_names=names,
                changes_count=changes.get(dossier.id, 0),
                pieces=views.piece_counts(pieces.get(dossier.id, [])),
            )
            if bucket is None or row["bucket"] == bucket:
                groups[row["bucket"]].append(row)
        for rows in groups.values():
            rows.sort(key=lambda r: (r["next_action_due"] or datetime.date.max, r["id"]))

        open_ids = [i for i, (d, _) in by_id.items() if d.status not in views.FINISHED]
        upcoming = list(
            await self._local.scalars(
                select(DeadlineModel)
                .where(
                    DeadlineModel.id_community == id_community,
                    DeadlineModel.id_dossier.in_(open_ids or [0]),
                    DeadlineModel.status.in_(UNRESOLVED),
                )
                .order_by(DeadlineModel.due_date, DeadlineModel.id)
                .limit(3)
            )
        )
        banner = [
            views.deadline_item(
                d,
                by_id[d.id_dossier][0],
                by_id[d.id_dossier][1],
                calendar,
                today=today,
                operation_names=names,
            )
            for d in upcoming
        ]
        return {"today": today, "banner": banner, "groups": groups}

    async def procedures(
        self,
        id_community: int,
        *,
        page: int,
        limit: int,
        status: int | None = None,
        code: str | None = None,
        operation: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Every procedure of the community, newest first, finished ones
        included (the dashboard keeps those for RECENT_DAYS only)."""
        plugin, _ = await self._plugin(id_community)
        if plugin is None:
            return [], 0
        today = brussels_today(self._now())
        stmt = select(DossierModel).where(
            DossierModel.id_community == id_community,
            DossierModel.id_procedure_definition.is_not(None),
            DossierModel.regulator == plugin.code,
        )
        if status is not None:
            stmt = stmt.where(DossierModel.status == status)
        if code is not None:
            stmt = stmt.where(
                DossierModel.id_procedure_definition.in_(
                    select(ProcedureDefinitionModel.id).where(ProcedureDefinitionModel.code == code)
                )
            )
        if operation is not None:
            stmt = stmt.where(DossierModel.id_sharing_operation == operation)
        total = int(
            await self._local.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        )
        dossiers = list(
            await self._local.scalars(
                stmt.order_by(DossierModel.id.desc()).offset((page - 1) * limit).limit(limit)
            )
        )
        ids = [d.id for d in dossiers]
        definitions = await self._definitions([d.id_procedure_definition or 0 for d in dossiers])
        names = await self._operation_names(id_community)
        changes = await self._active_change_counts(ids)
        pieces = await self._pieces_by_dossier(ids)
        calendar = self._calendar(plugin)
        rows = []
        for dossier in dossiers:
            definition = self._definition(
                plugin, definitions.get(dossier.id_procedure_definition or 0)
            )
            if definition is not None:
                rows.append(
                    views.item(
                        dossier,
                        definition,
                        calendar,
                        today=today,
                        operation_names=names,
                        changes_count=changes.get(dossier.id, 0),
                        pieces=views.piece_counts(pieces.get(dossier.id, [])),
                    )
                )
        return rows, total

    async def _active_change_counts(self, ids: Sequence[int]) -> dict[int, int]:
        if not ids:
            return {}
        rows = await self._local.execute(
            select(ChangeModel.id_dossier, func.count())
            .where(
                ChangeModel.id_dossier.in_(ids),
                ChangeModel.status == int(ChangeStatus.ATTACHED),
            )
            .group_by(ChangeModel.id_dossier)
        )
        return {int(d): int(n) for d, n in rows.tuples() if d is not None}

    async def _pieces_by_dossier(self, ids: Sequence[int]) -> dict[int, list[DocumentModel]]:
        found: dict[int, list[DocumentModel]] = defaultdict(list)
        if ids:
            for piece in await self._local.scalars(
                select(DocumentModel).where(DocumentModel.id_dossier.in_(ids))
            ):
                found[piece.id_dossier].append(piece)
        return found

    async def deadlines(
        self,
        id_community: int,
        *,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
    ) -> list[dict[str, Any]]:
        plugin, _ = await self._plugin(id_community)
        if plugin is None:
            return []
        today = brussels_today(self._now())
        stmt = (
            select(DeadlineModel, DossierModel)
            .join(DossierModel, DossierModel.id == DeadlineModel.id_dossier)
            .where(
                DeadlineModel.id_community == id_community,
                DossierModel.id_procedure_definition.is_not(None),
                DossierModel.regulator == plugin.code,
            )
        )
        if start is None and end is None:
            stmt = stmt.where(DeadlineModel.status.in_(UNRESOLVED))
        if start is not None:
            stmt = stmt.where(DeadlineModel.due_date >= start)
        if end is not None:
            stmt = stmt.where(DeadlineModel.due_date <= end)
        rows = list((await self._local.execute(stmt.order_by(DeadlineModel.due_date))).tuples())
        definitions = await self._definitions([d.id_procedure_definition or 0 for _, d in rows])
        names = await self._operation_names(id_community)
        calendar = self._calendar(plugin)
        out = []
        for deadline, dossier in rows:
            definition = self._definition(
                plugin, definitions.get(dossier.id_procedure_definition or 0)
            )
            if definition is not None:
                out.append(
                    views.deadline_item(
                        deadline, dossier, definition, calendar, today=today, operation_names=names
                    )
                )
        return out

    async def procedure(self, id_community: int, dossier_id: int) -> dict[str, Any]:
        dossier, plugin, ctx = await self._load(id_community, dossier_id)
        await self._local.refresh(dossier)
        changes = list(
            await self._local.scalars(
                select(ChangeModel)
                .where(ChangeModel.id_dossier == dossier.id)
                .order_by(ChangeModel.id)
            )
        )
        pieces = list(
            await self._local.scalars(
                select(DocumentModel)
                .where(DocumentModel.id_dossier == dossier.id)
                .order_by(DocumentModel.id)
            )
        )
        version_rows = list(
            (
                await self._local.execute(
                    select(
                        DocumentVersionModel.id_document,
                        DocumentVersionModel.version_no,
                        DocumentVersionModel.created_at,
                        DocumentVersionModel.generated_by,
                    )
                    .join(DocumentModel, DocumentModel.id == DocumentVersionModel.id_document)
                    .where(DocumentModel.id_dossier == dossier.id)
                )
            ).tuples()
        )
        versions: dict[int, int] = defaultdict(int)
        for document_id, *_ in version_rows:
            versions[document_id] += 1
        deadlines = list(
            await self._local.scalars(
                select(DeadlineModel)
                .where(DeadlineModel.id_dossier == dossier.id)
                .order_by(DeadlineModel.due_date, DeadlineModel.id)
            )
        )
        journal = list(
            await self._local.scalars(
                select(StatusEventModel)
                .where(
                    StatusEventModel.id_community == id_community,
                    StatusEventModel.subject_type == int(SubjectType.DOSSIER),
                    StatusEventModel.subject_id == dossier.id,
                )
                .order_by(StatusEventModel.id)
            )
        )
        correspondence = list(
            await self._local.scalars(
                select(CorrespondenceModel)
                .where(CorrespondenceModel.id_dossier == dossier.id)
                .order_by(CorrespondenceModel.occurred_at, CorrespondenceModel.id)
            )
        )
        return views.detail(
            dossier,
            ctx.definition,
            self._calendar(plugin),
            today=brussels_today(self._now()),
            operation_names=await self._operation_names(id_community),
            changes=changes,
            pieces=pieces,
            versions=versions,
            deadlines=deadlines,
            journal=journal,
            correspondence=correspondence,
            version_rows=version_rows,
        )

    # -- creating -----------------------------------------------------------------

    async def create(
        self,
        id_community: int,
        *,
        code: str,
        id_sharing_operation: int | None,
        requested_effective_date: datetime.date | None,
        actor: str,
    ) -> dict[str, Any]:
        today = brussels_today(self._now())
        plugin, _ = await self._plugin(id_community)
        entry = plugin.catalogue().in_force(code, today) if plugin else None
        if plugin is None or entry is None or entry.definition.purpose != "filing":
            raise ErrorException(errors.admin.UNKNOWN_PROCEDURE_TYPE, status_code=422)
        definition = entry.definition
        operation = None
        if definition.scope == "operation":
            if id_sharing_operation is None or (
                await SqlAlchemyCrmCoreRead(self._crm).get_sharing_operation(
                    id_community, id_sharing_operation
                )
                is None
            ):
                raise ErrorException(errors.admin.SHARING_OPERATION_NOT_FOUND, status_code=404)
            operation = id_sharing_operation
        period_key = str(today.year) if definition.opens_annually else None
        existing = await self._local.scalar(
            select(DossierModel.id).where(
                DossierModel.id_community == id_community,
                DossierModel.family == definition.family,
                DossierModel.status.in_(OPEN),
                DossierModel.id_sharing_operation.is_not_distinct_from(operation),
                DossierModel.period_key.is_not_distinct_from(period_key),
            )
        )
        if existing is not None:
            raise _conflict(errors.admin.DRAFT_EXISTS)
        region = self._region_of(plugin.code)
        if region is None:
            raise ErrorException(errors.admin.REGION_NOT_RESOLVED, status_code=422)
        ctx = await context_for(self._local, plugin, definition.code, definition.version)
        try:
            dossier = await open_procedure(
                self._local,
                ctx,
                id_community=id_community,
                id_sharing_operation=operation,
                region=int(region),
                variant=plugin.variant(definition, []),
                opened_by=OpenedBy.USER,
                day=today,
                period_key=period_key,
                actor=actor,
                context={"reason": "created by hand"},
            )
            dossier.requested_effective_date = requested_effective_date
            await recompute(self._local, ctx, dossier)
            await self._local.commit()
        except IntegrityError:
            await self._local.rollback()
            raise _conflict(errors.admin.DRAFT_EXISTS) from None
        await self._audit(AuditActions.DOSSIER_CREATED, dossier, {"code": definition.code})
        return await self.procedure(id_community, dossier.id)

    # -- changes --------------------------------------------------------------------

    async def _change(self, dossier: DossierModel, change_id: int) -> ChangeModel:
        if dossier.frozen_at is not None:
            raise _conflict(errors.admin.PROCEDURE_FROZEN)
        change = await self._local.scalar(
            select(ChangeModel).where(
                ChangeModel.id == change_id,
                ChangeModel.id_dossier == dossier.id,
                ChangeModel.status == int(ChangeStatus.ATTACHED),
            )
        )
        if change is None:
            raise _not_found(errors.admin.CHANGE_NOT_FOUND)
        return change

    async def defer_change(
        self, id_community: int, dossier_id: int, change_id: int, *, actor: str
    ) -> dict[str, Any]:
        dossier, _plugin, ctx = await self._load(id_community, dossier_id)
        change = await self._change(dossier, change_id)
        change.status = int(ChangeStatus.DEFERRED)
        change.payload_json = {
            **change.payload_json,
            "deferred": {"by": actor, "at": self._now().isoformat()},
        }
        today = brussels_today(self._now())
        await self._local.flush()
        await content_changed(self._local, ctx, dossier, today)
        await recompute(self._local, ctx, dossier)
        await self._local.commit()
        await self._audit(AuditActions.CHANGE_DEFERRED, dossier, {"change": change.id})
        return await self.procedure(id_community, dossier.id)

    async def exclude_change(
        self, id_community: int, dossier_id: int, change_id: int, *, reason: str, actor: str
    ) -> dict[str, Any]:
        dossier, _plugin, ctx = await self._load(id_community, dossier_id)
        change = await self._change(dossier, change_id)
        change.status = int(ChangeStatus.EXCLUDED)
        change.payload_json = {
            **change.payload_json,
            "excluded": {"reason": reason, "by": actor, "at": self._now().isoformat()},
        }
        today = brussels_today(self._now())
        await self._local.flush()
        await content_changed(self._local, ctx, dossier, today)
        if not await cancel_if_emptied(self._local, ctx, dossier, today):
            await recompute(self._local, ctx, dossier)
        await self._local.commit()
        await self._audit(
            AuditActions.CHANGE_EXCLUDED, dossier, {"change": change.id, "reason": reason}
        )
        return await self.procedure(id_community, dossier.id)

    async def _release_deferred(
        self, plugin: RegulatorPlugin, dossier: DossierModel, day: datetime.date
    ) -> None:
        if dossier.status not in _RELEASES_DEFERRED:
            return
        deferred = list(
            await self._local.scalars(
                select(ChangeModel).where(
                    ChangeModel.id_dossier == dossier.id,
                    ChangeModel.status == int(ChangeStatus.DEFERRED),
                )
            )
        )
        for change in deferred:
            await refile_change(self._local, plugin, change, day=day, region_of=self._region_of)

    # -- steps ----------------------------------------------------------------------

    async def transition(
        self,
        id_community: int,
        dossier_id: int,
        *,
        to_step: str,
        reason: str,
        on: datetime.date | None,
        actor: str,
    ) -> dict[str, Any]:
        dossier, plugin, ctx = await self._load(id_community, dossier_id)
        if not ctx.definition.allows(str(dossier.step), to_step, dossier.variant):
            raise _conflict(errors.admin.TRANSITION_NOT_ALLOWED)
        day = on or brussels_today(self._now())
        await move_to_step(
            self._local,
            ctx,
            dossier,
            to_step,
            day=day,
            actor=actor,
            context={"reason": reason, "manual": True},
        )
        self._freeze_if_sent(dossier, day)
        await self._release_deferred(plugin, dossier, day)
        await self._local.commit()
        await self._audit(
            AuditActions.DOSSIER_STATUS_CHANGED, dossier, {"to_step": to_step, "reason": reason}
        )
        return await self.procedure(id_community, dossier.id)

    @staticmethod
    def _freeze_if_sent(dossier: DossierModel, day: datetime.date) -> None:
        if dossier.status == int(ProcedureStatus.SENT) and dossier.frozen_at is None:
            dossier.frozen_at = datetime.datetime.combine(day, datetime.time(), BRUSSELS)
            dossier.submitted_at = dossier.frozen_at

    # -- files ----------------------------------------------------------------------

    async def _read(self, file: UploadFile) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while chunk := await file.read(_READ_CHUNK_BYTES):
            total += len(chunk)
            if total > UPLOAD_MAX_BODY_BYTES:
                raise ErrorException(errors.admin.FILE_TOO_LARGE, status_code=413)
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content:
            raise ErrorException(errors.admin.INVALID_FILE, status_code=422)
        return content

    async def _store(self, key: str, content: bytes, content_type: str | None) -> str:
        try:
            await upload(key, content, content_type=content_type, bucket=settings.OUTPUT_BUCKET)
        except Exception as exc:
            raise ErrorException(errors.admin.STORAGE_UPLOAD_FAILED, status_code=502) from exc
        return build_s3_uri(settings.OUTPUT_BUCKET, key)

    async def upload_piece(
        self, id_community: int, dossier_id: int, document_id: int, file: UploadFile, *, actor: str
    ) -> dict[str, Any]:
        dossier, _plugin, ctx = await self._load(id_community, dossier_id)
        piece = await self._local.scalar(
            select(DocumentModel).where(
                DocumentModel.id == document_id,
                DocumentModel.id_dossier == dossier.id,
                DocumentModel.requirement_code.is_not(None),
            )
        )
        if piece is None:
            raise _not_found(errors.admin.PIECE_NOT_FOUND)
        if dossier.frozen_at is not None and piece.kind != int(DocumentKind.RECEIVED):
            raise _conflict(errors.admin.PROCEDURE_FROZEN)
        content = await self._read(file)
        digest = hashlib.sha256(content).hexdigest()
        version_no = (
            int(
                await self._local.scalar(
                    select(func.coalesce(func.max(DocumentVersionModel.version_no), 0)).where(
                        DocumentVersionModel.id_document == piece.id
                    )
                )
                or 0
            )
            + 1
        )
        key = f"{STORAGE_KEY_PREFIX}/{id_community}/{piece.id}/v{version_no}/{digest}"
        version = DocumentVersionModel(
            id_community=id_community,
            id_document=piece.id,
            version_no=version_no,
            file_ref=await self._store(key, content, file.content_type),
            content_sha256=digest,
            content_type=file.content_type,
            byte_size=len(content),
            original_filename=file.filename,
            generated_by=actor,
        )
        self._local.add(version)
        await self._local.flush()
        piece.current_version_id = version.id
        piece.origin = int(DocOrigin.UPLOADED)
        signed = piece.kind == int(DocumentKind.COLLECTED)
        await supply_piece(
            self._local,
            ctx,
            dossier,
            piece,
            status=ProcedureDocumentStatus.SIGNED if signed else ProcedureDocumentStatus.READY,
            day=brussels_today(self._now()),
            actor=actor,
            context={"version": version.id},
        )
        await self._local.commit()
        await self._audit(
            AuditActions.DOCUMENT_VERSION_ADDED,
            dossier,
            {"id_document": piece.id, "version_no": version_no, "sha256": digest},
        )
        return await self.procedure(id_community, dossier.id)

    # -- sending and answers ------------------------------------------------------

    async def _record(
        self,
        dossier: DossierModel,
        *,
        type_: str,
        direction: CorrespondenceDirection,
        counterparty: Counterparty,
        channel: int,
        occurred_on: datetime.date,
        subject: str | None,
        body_text: str | None,
        external_ref: str | None,
        file: UploadFile | None,
        actor: str,
    ) -> CorrespondenceModel:
        message = CorrespondenceModel(
            id_community=dossier.id_community,
            id_dossier=dossier.id,
            direction=int(direction),
            counterparty=int(counterparty),
            channel=channel,
            type=type_,
            occurred_at=datetime.datetime.combine(occurred_on, datetime.time(), BRUSSELS),
            subject=subject,
            body_text=body_text,
            external_ref=external_ref,
            recorded_by=actor,
            context_json={},
        )
        if file is not None:
            content = await self._read(file)
            digest = hashlib.sha256(content).hexdigest()
            key = (
                f"{STORAGE_KEY_PREFIX}/{dossier.id_community}/correspondence/{dossier.id}/{digest}"
            )
            message.file_ref = await self._store(key, content, file.content_type)
            message.content_sha256 = digest
            message.content_type = file.content_type
            message.byte_size = len(content)
            message.original_filename = file.filename
        self._local.add(message)
        await self._local.flush()
        return message

    async def send(
        self,
        id_community: int,
        dossier_id: int,
        *,
        on: datetime.date | None,
        channel: str | None,
        subject: str | None,
        body_text: str | None,
        file: UploadFile | None,
        actor: str,
    ) -> dict[str, Any]:
        """Mark as sent: the step change to the catalogue's "sent" step, the
        outgoing message (with the sent .eml or the form's receipt), the
        procedure frozen."""
        dossier, plugin, ctx = await self._load(id_community, dossier_id)
        definition = ctx.definition
        step = str(dossier.step)
        target = next(
            (
                e.target
                for e in definition.transitions
                if e.source == step
                and definition.allows(step, e.target, dossier.variant)
                and definition.step(e.target).common_status == ProcedureStatus.SENT
            ),
            None,
        )
        if target is None:
            raise _conflict(errors.admin.TRANSITION_NOT_ALLOWED)
        day = on or brussels_today(self._now())
        response = next(
            (
                r
                for r in definition.response_types
                if r.direction == "outbound" and r.to_step == target
            ),
            None,
        )
        message = await self._record(
            dossier,
            type_=response.code if response else "sent",
            direction=CorrespondenceDirection.OUTBOUND,
            counterparty=Counterparty.GRD
            if definition.recipient == "grd_ref"
            else Counterparty.REGULATOR,
            channel=int(Channel[channel.upper()]) if channel else int(definition.channel_code),
            occurred_on=day,
            subject=subject,
            body_text=body_text,
            external_ref=None,
            file=file,
            actor=actor,
        )
        await move_to_step(
            self._local,
            ctx,
            dossier,
            target,
            day=day,
            actor=actor,
            context={"reason": "sent", "correspondence": message.id},
        )
        if response is not None:
            event = response_event(response.code)
            await close_rules(self._local, ctx, dossier, event)
            await start_rules(
                self._local, ctx, dossier, event, anchor=day, correspondence_id=message.id
            )
        self._freeze_if_sent(dossier, day)
        await self._release_deferred(plugin, dossier, day)
        await self._local.commit()
        await self._audit(
            AuditActions.DOSSIER_STATUS_CHANGED, dossier, {"to_step": target, "sent_on": str(day)}
        )
        return await self.procedure(id_community, dossier.id)

    async def respond(
        self,
        id_community: int,
        dossier_id: int,
        *,
        type_: str,
        on: datetime.date | None,
        subject: str | None,
        body_text: str | None,
        external_ref: str | None,
        file: UploadFile | None,
        dry_run: bool,
        actor: str,
    ) -> dict[str, Any]:
        """Record a response (or say what recording it would do, ``dry_run``):
        the message, the deadlines it meets and starts, its step change."""
        dossier, plugin, ctx = await self._load(id_community, dossier_id)
        definition = ctx.definition
        try:
            response = definition.response_type(type_)
        except KeyError:
            raise ErrorException(errors.admin.UNKNOWN_RESPONSE_TYPE, status_code=422) from None
        if response.variants is not None and dossier.variant not in response.variants:
            raise ErrorException(errors.admin.UNKNOWN_RESPONSE_TYPE, status_code=422)
        day = on or brussels_today(self._now())
        step = str(dossier.step)
        target = response.to_step if response.to_step and response.to_step != step else None
        allowed = target is None or definition.allows(step, target, dossier.variant)

        if dry_run:
            return self._proposal(dossier, ctx, response.code, target, allowed, day)
        if not allowed:
            raise _conflict(errors.admin.TRANSITION_NOT_ALLOWED)

        message = await self._record(
            dossier,
            type_=response.code,
            direction=response.correspondence_direction,
            counterparty=response.counterparty_code,
            channel=int(Channel.UPLOAD if file is not None else definition.channel_code),
            occurred_on=day,
            subject=subject,
            body_text=body_text,
            external_ref=external_ref,
            file=file,
            actor=actor,
        )
        event = response_event(response.code)
        await close_rules(self._local, ctx, dossier, event)
        await start_rules(
            self._local, ctx, dossier, event, anchor=day, correspondence_id=message.id
        )
        if target is not None:
            await move_to_step(
                self._local,
                ctx,
                dossier,
                target,
                day=day,
                actor=actor,
                context={"reason": response.code, "correspondence": message.id},
            )
            self._freeze_if_sent(dossier, day)
            await self._release_deferred(plugin, dossier, day)
        if external_ref and dossier.external_ref is None:
            dossier.external_ref = external_ref
        try:
            await self._local.commit()
        except IntegrityError:
            await self._local.rollback()
            raise _conflict(errors.admin.DUPLICATE_EXTERNAL_REF) from None
        await self._audit(
            AuditActions.DOSSIER_UPDATED, dossier, {"response": response.code, "on": str(day)}
        )
        return await self.procedure(id_community, dossier.id)

    def _proposal(
        self,
        dossier: DossierModel,
        ctx: ProcedureContext,
        code: str,
        target: str | None,
        allowed: bool,
        day: datetime.date,
    ) -> dict[str, Any]:
        """What ``respond`` would do. Reads the catalogue, writes nothing."""
        definition = ctx.definition
        events = [response_event(code)]
        if target is not None and allowed:
            events.append(step_event(target))
        started = []
        met: list[str] = []
        for event in events:
            met.extend(r.code for r in definition.rules_closed_by(event, dossier.variant))
            for rule in definition.rules_started_by(event, dossier.variant):
                started.append(
                    {
                        "code": rule.code,
                        "labels": rule.labels,
                        "due_date": due_date(rule, ctx.calendar, anchor=day),
                        "party": int(rule.deadline_party),
                        "kind": int(rule.kind_of_deadline),
                    }
                )
        response = definition.response_type(code)
        return {
            "type": code,
            "labels": response.labels,
            "direction": response.direction,
            "from_step": str(dossier.step),
            "to_step": target,
            "to_step_labels": definition.step(target).labels if target else None,
            "transition_allowed": allowed,
            "deadlines_started": started,
            "deadlines_met": sorted(set(met)),
        }

    async def correspondence_file(
        self, id_community: int, dossier_id: int, correspondence_id: int
    ) -> tuple[CorrespondenceModel, bytes]:
        dossier, _plugin, _ctx = await self._load(id_community, dossier_id)
        message = await self._local.scalar(
            select(CorrespondenceModel).where(
                CorrespondenceModel.id == correspondence_id,
                CorrespondenceModel.id_dossier == dossier.id,
            )
        )
        if message is None or message.file_ref is None:
            raise _not_found(errors.admin.CORRESPONDENCE_NOT_FOUND)
        try:
            bucket, key = parse_s3_uri(message.file_ref)
            content = await download(key, bucket=bucket)
        except Exception as exc:
            raise ErrorException(errors.admin.STORAGE_DOWNLOAD_FAILED, status_code=502) from exc
        return message, content
