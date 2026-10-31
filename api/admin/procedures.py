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
  draft once this one is sent or cancelled;
- after the commits, the core's badges follow (``engine.effects.sync``, which
  never fails the action) and the screens are told to refetch; a step into
  force goes through ``engine.effects.go_in_force``, the core first.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import re
import unicodedata
import zipfile
from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin import views
from api.administrative_document.service import AdministrativeDocumentService
from core.audit_log import AuditActions, AuditLogInput, AuditLogService
from core.config import settings
from core.errors.errors import ErrorException
from core.realtime import CommunityAudience, Tier, emit
from core.storage import build_s3_uri, download, parse_s3_uri, upload
from domain.regions import region_for_regulator
from engine.activation import reconcile_activation
from engine.baseline import brussels_today, ensure_baseline
from engine.calendar import WorkingCalendar
from engine.catalogue import ProcedureDefinition, response_event, step_event
from engine.contracts import REMOVAL_KINDS, PluginRegistry, RegulatorPlugin, plugin_for
from engine.deadlines import due_date
from engine.effects import InForceError, StepOverride, go_in_force, is_tracked, sync
from engine.effects import plan as plan_effects
from engine.events import BRUSSELS, DetectedChange
from engine.filing import (
    OPEN,
    RegionOf,
    cancel_if_emptied,
    content_changed,
    context_of_procedure,
    file_change,
    recompute,
    refile_change,
)
from engine.lifecycle import (
    UNRESOLVED,
    EffectsNotWrittenError,
    ProcedureContext,
    close_rules,
    context_for,
    effect_date,
    move_to_step,
    open_procedure,
    pieces_of,
    start_rules,
    supply_piece,
)
from engine.templates import template_in_force
from ports.core_effects import CoreEffectsPort, EffectConflictError
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from ports.crm_engine import EngineCrmRead, Episode
from shared.const import (
    STORAGE_KEY_PREFIX,
    UPLOAD_MAX_BODY_BYTES,
    ChangeStatus,
    ChangeSubjectType,
    Channel,
    CorrespondenceDirection,
    Counterparty,
    DeadlineKind,
    DocOrigin,
    DocumentKind,
    OpenedBy,
    ProcedureDocumentStatus,
    ProcedureStatus,
    SubjectType,
    VersionStage,
)
from shared.custom_errors import errors
from shared.models.local_models import (
    ChangeModel,
    CorrespondenceModel,
    DeadlineModel,
    DocumentModel,
    DocumentRenderModel,
    DocumentTemplateModel,
    DocumentVersionModel,
    DossierModel,
    ProcedureDefinitionModel,
    StatusEventModel,
    VersionCheckModel,
)

if TYPE_CHECKING:  # the worker image has no starlette; this module is API-only anyway
    from fastapi import UploadFile

_READ_CHUNK_BYTES = 1024 * 1024
# Entering a step of one of these releases the changes deferred out of it.
_RELEASES_DEFERRED = (int(ProcedureStatus.SENT), int(ProcedureStatus.CANCELLED))
# A piece the community has supplied: what goes out with the sending.
_SUPPLIED = (int(ProcedureDocumentStatus.READY), int(ProcedureDocumentStatus.SIGNED))
# What a refiled change leaves behind: its row, its life in the refused one.
_NOT_REFILED = (
    "episode",
    "baseline",
    "refiled",
    "refiled_from",
    "excluded",
    "deferred",
    "superseded_by",
)


# Fixed, not wall-clock: the same finals make the same archive.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_ZIP_UNIX = 3


def archive_name(position: int, filename: str, member: str | None) -> str:
    """``03-annexe8-declaration-sur-lhonneur-alice-dupont.pdf``: the piece's
    place in the procedure first, so the names sort as the pieces do and two
    participants' declarations (same file name) do not collide."""
    stem, dot, extension = filename.rpartition(".")
    if not dot:
        stem, extension = filename, ""
    if member:
        slug = re.sub(
            r"[^a-z0-9]+",
            "-",
            unicodedata.normalize("NFKD", member).encode("ascii", "ignore").decode().lower(),
        ).strip("-")
        stem = f"{stem}-{slug}" if slug else stem
    return f"{position:02d}-{stem}" + (f".{extension}" if extension else "")


def deterministic_zip(files: Sequence[tuple[str, bytes, str]]) -> bytes:
    """``(name, content, sha256)`` entries, then ``SHA256SUMS.txt``, with fixed
    dates and system: the same finals always zip to the same bytes."""
    buffer = io.BytesIO()
    sums = "".join(f"{digest}  {name}\n" for name, _content, digest in files)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in [*((n, c) for n, c, _ in files), ("SHA256SUMS.txt", sums.encode())]:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = _ZIP_UNIX
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return buffer.getvalue()


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
        effects: CoreEffectsPort,
        registry: PluginRegistry,
        generation: AdministrativeDocumentService | None = None,
        region_of: RegionOf = region_for_regulator,
    ) -> None:
        self._local = local_session
        self._crm = crm_session
        self._crm_read = crm_read
        self._effects = effects
        self._registry = registry
        self._generation = generation
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
        await self._audit_ids(action, dossier.id_community, dossier.id, payload)

    async def _audit_ids(
        self, action: str, id_community: int, dossier_id: int, payload: dict[str, Any]
    ) -> None:
        await AuditLogService(self._crm).log(
            AuditLogInput(
                action=action, entity_type="dossier", entity_id=str(dossier_id), payload=payload
            ),
            id_community=id_community,
        )
        await self._crm.commit()

    async def _after(self, id_community: int, dossier_ids: Sequence[int]) -> None:
        """After the commits: the core's badges for these procedures (never
        raising: a failure stays on them for the daily reconciliation), then
        the hint that makes their screens refetch."""
        ids = sorted(set(dossier_ids))
        await sync(
            self._local,
            self._crm,
            self._crm_read,
            self._effects,
            self._registry,
            id_community,
            ids,
            now=self._now(),
        )
        audience = CommunityAudience(id_community, Tier.MANAGER)
        for dossier_id in ids:
            await emit(
                topic="admin.dossier.updated",
                audience=audience,
                resource=("dossier", dossier_id),
                scope_community_id=id_community,
            )

    async def _episodes(
        self, id_community: int, changes: Sequence[ChangeModel]
    ) -> dict[int, Episode]:
        """The core row each change's pin names, by change id."""
        keys = {
            c.id: (int(pin["origin"]), c.subject_type, c.subject_ref)
            for c in changes
            if isinstance(pin := (c.payload_json or {}).get("episode"), dict) and "origin" in pin
        }
        rows = await self._crm_read.episode_rows(id_community, list(keys.values()))
        return {change_id: rows[key] for change_id, key in keys.items() if key in rows}

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
            facts=await self._piece_facts(dossier, pieces),
            has_entry_sheet=any(plugin.entry_sheet(p.requirement_code) for p in pieces),
            episodes=await self._episodes(id_community, changes),
            refusal=await self._refusal(dossier, ctx, changes),
        )

    async def _piece_facts(
        self, dossier: DossierModel, pieces: Sequence[DocumentModel]
    ) -> views.PieceFacts:
        facts = views.PieceFacts()
        current_ids = [p.current_version_id for p in pieces if p.current_version_id is not None]
        if current_ids:
            by_id = {
                v.id: v
                for v in await self._local.scalars(
                    select(DocumentVersionModel).where(DocumentVersionModel.id.in_(current_ids))
                )
            }
            facts.current = {
                p.id: by_id[p.current_version_id] for p in pieces if p.current_version_id in by_id
            }
            for check in await self._local.scalars(
                select(VersionCheckModel)
                .where(VersionCheckModel.id_document_version.in_(current_ids))
                .order_by(VersionCheckModel.id)
            ):
                facts.checks[check.id_document_version] = check  # the last one wins
        if pieces:
            facts.renders = {
                r.id_document: r
                for r in await self._local.scalars(
                    select(DocumentRenderModel).where(
                        DocumentRenderModel.id_document.in_([p.id for p in pieces])
                    )
                )
            }
        for piece in pieces:
            if piece.kind == int(DocumentKind.RECEIVED):
                continue
            in_force = await template_in_force(
                self._local,
                id_community=dossier.id_community,
                region=dossier.region,
                doc_type=piece.doc_type,
            )
            pinned = (
                await self._local.get(DocumentTemplateModel, piece.id_template)
                if piece.id_template is not None
                else in_force
            )
            if pinned is not None:
                superseded = in_force is not None and in_force.id != pinned.id
                facts.templates[piece.id] = (pinned, superseded)
        members = sorted({p.id_member for p in pieces if p.id_member is not None})
        if members:
            facts.member_names = await SqlAlchemyCrmCoreRead(self._crm).member_names(
                dossier.id_community, members
            )
        return facts

    async def entry_sheets(self, id_community: int, dossier_id: int) -> list[dict[str, Any]]:
        """The fiches de saisie of the procedure's form pieces, filled from each
        piece's current version: what was reviewed (and, once sent, filed)."""
        dossier, plugin, _ctx = await self._load(id_community, dossier_id)
        pieces = await pieces_of(self._local, dossier)
        facts = await self._piece_facts(dossier, pieces)
        sheets = []
        for piece in pieces:
            sheet = plugin.entry_sheet(piece.requirement_code)
            if sheet is None:
                continue
            version = facts.current.get(piece.id)
            sheets.append(
                {
                    "piece_id": piece.id,
                    "code": piece.requirement_code,
                    "labels": sheet.labels,
                    "version": views.version_out(version) if version is not None else None,
                    "sections": sheet.filled(
                        version.data_snapshot_json if version is not None else None
                    ),
                }
            )
        return sheets

    async def finals_zip(self, id_community: int, dossier_id: int) -> tuple[str, bytes]:
        """Every supplied piece's final, in one archive to attach: the files as
        stored (each checked against its sha256 on the way out), a
        ``SHA256SUMS.txt`` beside them. Refused while one is still a draft."""
        dossier, _plugin, _ctx = await self._load(id_community, dossier_id)
        pieces = await pieces_of(self._local, dossier)
        facts = await self._piece_facts(dossier, pieces)
        if not facts.finals_ready(pieces):
            raise _conflict(errors.admin.FINAL_VERSIONS_REQUIRED)
        files: list[tuple[str, bytes, str]] = []
        for position, piece in enumerate(pieces, start=1):
            version = facts.current.get(piece.id)
            if (
                version is None
                or piece.kind == int(DocumentKind.RECEIVED)
                or piece.status not in _SUPPLIED
            ):
                continue
            content = await self._stored(version)
            name = archive_name(
                position,
                version.original_filename or f"{piece.requirement_code}-v{version.version_no}",
                facts.member_names.get(piece.id_member) if piece.id_member else None,
            )
            files.append((name, content, hashlib.sha256(content).hexdigest()))
        return f"OPT-{dossier.id}-pieces.zip", deterministic_zip(files)

    async def _stored(self, version: DocumentVersionModel) -> bytes:
        try:
            bucket, key = parse_s3_uri(version.file_ref)
            content = await download(key, bucket=bucket)
        except Exception as exc:
            raise ErrorException(errors.admin.STORAGE_DOWNLOAD_FAILED, status_code=502) from exc
        if version.content_sha256 and hashlib.sha256(content).hexdigest() != version.content_sha256:
            # The bytes behind a version are evidence: never hand over a file
            # that is not the one recorded.
            raise ErrorException(errors.admin.STORED_FILE_ALTERED, status_code=502)
        return content

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
        await self._after(id_community, [dossier_id])
        return await self.procedure(id_community, dossier_id)

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
        await self._after(id_community, [dossier_id])
        return await self.procedure(id_community, dossier_id)

    async def _release_deferred(
        self, plugin: RegulatorPlugin, dossier: DossierModel, day: datetime.date
    ) -> list[int]:
        """The drafts the deferred changes went to."""
        if dossier.status not in _RELEASES_DEFERRED:
            return []
        deferred = list(
            await self._local.scalars(
                select(ChangeModel).where(
                    ChangeModel.id_dossier == dossier.id,
                    ChangeModel.status == int(ChangeStatus.DEFERRED),
                )
            )
        )
        drafts = []
        for change in deferred:
            draft = await refile_change(
                self._local, plugin, change, day=day, region_of=self._region_of
            )
            drafts.append(draft.id)
        return drafts

    # -- after a refusal --------------------------------------------------------------

    @staticmethod
    def _refilable(dossier: DossierModel, changes: Sequence[ChangeModel]) -> list[ChangeModel]:
        """A refused procedure's refused changes, or a lapsed one's still
        attached, about a subject the core tracks, not refiled yet."""
        wanted = {
            int(ProcedureStatus.REFUSED): int(ChangeStatus.REFUSED),
            int(ProcedureStatus.LAPSED): int(ChangeStatus.ATTACHED),
        }.get(dossier.status)
        if wanted is None:
            return []
        return [
            c
            for c in changes
            if c.status == wanted and is_tracked(c) and not (c.payload_json or {}).get("refiled")
        ]

    async def _present(
        self, dossier: DossierModel, changes: Sequence[ChangeModel], *, on: datetime.date
    ) -> dict[int, bool]:
        """Is the CRM still as each change says? An addition: the meter is in
        the operation, the key is its key. A removal: the meter is gone."""
        operation = dossier.id_sharing_operation
        if operation is None or not changes:
            return {}
        meters = await self._crm_read.meters_in_operation(
            dossier.id_community,
            operation,
            [c.subject_ref for c in changes if c.subject_type == int(ChangeSubjectType.METER)],
            on=on,
        )
        key = await self._crm_read.current_key(dossier.id_community, operation, on=on)
        present = {}
        for change in changes:
            if change.subject_type == int(ChangeSubjectType.KEY):
                present[change.id] = str(key) == change.subject_ref
            elif change.kind in REMOVAL_KINDS:
                present[change.id] = change.subject_ref not in meters
            else:
                present[change.id] = change.subject_ref in meters
        return present

    async def _refusal(
        self, dossier: DossierModel, ctx: ProcedureContext, changes: Sequence[ChangeModel]
    ) -> dict[str, Any] | None:
        refilable = self._refilable(dossier, changes)
        if not refilable:
            return None
        draft = await self._local.scalar(
            select(DossierModel.id).where(
                DossierModel.id_community == dossier.id_community,
                DossierModel.family == ctx.definition.family,
                DossierModel.status.in_(OPEN),
                DossierModel.id_sharing_operation.is_not_distinct_from(
                    dossier.id_sharing_operation
                ),
            )
        )
        return {
            "changes": [c.id for c in refilable],
            "id_sharing_operation": dossier.id_sharing_operation,
            "draft_id": draft,
            "present": await self._present(dossier, refilable, on=brussels_today(self._now())),
        }

    async def refile(
        self,
        id_community: int,
        dossier_id: int,
        *,
        change_ids: Sequence[int] | None,
        actor: str,
    ) -> dict[str, Any]:
        """« Corriger et renvoyer »: each refused change filed again, as a new
        change (no source event, so UNIQUE never bites; ``refiled_from`` says
        where it comes from), through the same filing as an event, into the
        family's open draft. The refused one is marked ``refiled`` and stays
        as it was, history."""
        dossier, plugin, ctx = await self._load(id_community, dossier_id)
        changes = list(
            await self._local.scalars(
                select(ChangeModel)
                .where(ChangeModel.id_dossier == dossier.id)
                .order_by(ChangeModel.id)
            )
        )
        refilable = {c.id: c for c in self._refilable(dossier, changes)}
        chosen = list(refilable.values()) if change_ids is None else []
        for change_id in change_ids or ():
            if change_id not in refilable:
                raise _conflict(errors.admin.NOT_REFILABLE)
            chosen.append(refilable[change_id])
        if not chosen:
            raise _conflict(errors.admin.NOT_REFILABLE)
        now = self._now()
        today = brussels_today(now)
        present = await self._present(dossier, chosen, on=today)
        if not all(present.get(c.id) for c in chosen):
            raise _conflict(errors.admin.SUBJECT_NOT_IN_OPERATION)

        drafts: list[int] = []
        new_changes: list[int] = []
        for change in chosen:
            entry = next(
                (
                    e
                    for e in plugin.catalogue().handling(change.kind, today)
                    if e.definition.family == ctx.definition.family
                ),
                None,
            )
            if entry is None:
                raise _conflict(errors.admin.NOT_REFILABLE)
            payload = {
                k: v for k, v in (change.payload_json or {}).items() if k not in _NOT_REFILED
            }
            try:
                filed = await file_change(
                    self._local,
                    plugin,
                    entry,
                    DetectedChange(
                        kind=change.kind,
                        subject_type=ChangeSubjectType(change.subject_type),
                        subject_ref=change.subject_ref,
                        id_sharing_operation=change.id_sharing_operation,
                        payload={**payload, "refiled_from": change.id},
                    ),
                    id_community=id_community,
                    day=today,
                    at=now,
                    source_event_id=None,
                    region_of=self._region_of,
                    context={"reason": "refiled", "from": dossier_id},
                )
            except IntegrityError:
                await self._local.rollback()
                raise _conflict(errors.admin.DRAFT_EXISTS) from None
            if filed is None:
                raise _conflict(errors.admin.NOT_REFILABLE)
            drafts.append(filed[0])
            new_changes.append(filed[1])
            change.payload_json = {
                **(change.payload_json or {}),
                "refiled": {
                    "into": filed[0],
                    "change": filed[1],
                    "by": actor,
                    "at": now.isoformat(),
                },
            }
        await self._local.commit()
        await self._audit_ids(
            AuditActions.DOSSIER_UPDATED,
            id_community,
            dossier_id,
            {"refiled": [c.id for c in chosen], "into": sorted(set(drafts))},
        )
        await self._after(id_community, [dossier_id, *drafts])
        return {"id_dossier": drafts[-1], "changes": new_changes}

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
        target = ctx.definition.step(to_step).common_status
        if target == ProcedureStatus.IN_FORCE:
            return await self._in_force(
                id_community,
                dossier_id,
                to_step=to_step,
                effective=on or dossier.effective_date or brussels_today(self._now()),
                reason=reason,
                actor=actor,
            )
        if target == ProcedureStatus.SENT:
            await self._require_finals(dossier)
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
        released = await self._release_deferred(plugin, dossier, day)
        await self._local.commit()
        await self._audit(
            AuditActions.DOSSIER_STATUS_CHANGED, dossier, {"to_step": to_step, "reason": reason}
        )
        await self._after(id_community, [dossier_id, *released])
        return await self.procedure(id_community, dossier_id)

    async def _in_force(
        self,
        id_community: int,
        dossier_id: int,
        *,
        to_step: str,
        effective: datetime.date,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        """By hand into force: the core first, as the effect job does. Raises
        ``EffectConflictError`` (the route's 409 lists the EANs) when the core
        refuses; the procedure then stays where it was."""
        try:
            refused = await go_in_force(
                self._local,
                self._crm,
                self._crm_read,
                self._effects,
                self._registry,
                dossier_id,
                effective_date=effective,
                now=self._now(),
                to_step=to_step,
                actor=actor,
                context={"reason": reason, "manual": True},
            )
        except InForceError:
            raise _conflict(errors.admin.TRANSITION_NOT_ALLOWED) from None
        if refused:
            raise EffectConflictError(refused)
        await self._audit_ids(
            AuditActions.DOSSIER_STATUS_CHANGED,
            id_community,
            dossier_id,
            {"to_step": to_step, "reason": reason, "effective_date": effective.isoformat()},
        )
        await self._after(id_community, [dossier_id])
        return await self.procedure(id_community, dossier_id)

    @staticmethod
    def _freeze_if_sent(dossier: DossierModel, day: datetime.date) -> None:
        if dossier.status == int(ProcedureStatus.SENT) and dossier.frozen_at is None:
            dossier.frozen_at = datetime.datetime.combine(day, datetime.time(), BRUSSELS)
            dossier.submitted_at = dossier.frozen_at

    async def _require_finals(self, dossier: DossierModel) -> None:
        """What is sent the first time must be final: every piece the community
        supplied has an unstamped current version, and no render is still in
        flight (it would land on a frozen piece). Uploads are final; a received
        piece comes later. A re-send changes no piece (they froze with the first
        sending), so it is not checked again."""
        if dossier.frozen_at is not None:
            return
        drafts = await self._local.scalar(
            select(func.count())
            .select_from(DocumentModel)
            .join(DocumentVersionModel, DocumentVersionModel.id == DocumentModel.current_version_id)
            .where(
                DocumentModel.id_dossier == dossier.id,
                DocumentModel.kind != int(DocumentKind.RECEIVED),
                DocumentModel.status.in_(_SUPPLIED),
                DocumentVersionModel.stage != int(VersionStage.FINAL),
            )
        )
        in_flight = await self._local.scalar(
            select(func.count())
            .select_from(DocumentRenderModel)
            .join(DocumentModel, DocumentModel.id == DocumentRenderModel.id_document)
            .where(DocumentModel.id_dossier == dossier.id)
        )
        if drafts or in_flight:
            raise _conflict(errors.admin.FINAL_VERSIONS_REQUIRED)

    # -- generation -----------------------------------------------------------------

    def _generator(self) -> AdministrativeDocumentService:
        if self._generation is None:  # wired by api.admin.deps; a test may omit it
            raise RuntimeError("ProcedureService was built without a generation service")
        return self._generation

    async def finalize(self, id_community: int, dossier_id: int) -> dict[str, list[int]]:
        """Render the finals of a procedure whose pieces are complete: every
        generated piece whose current version is a draft is rendered again from
        that draft's own snapshot and template, unstamped. What the send dialog
        offers as the pieces to attach."""
        dossier, _plugin, _ctx = await self._load(id_community, dossier_id)
        if dossier.frozen_at is not None or dossier.status != int(ProcedureStatus.READY):
            raise _conflict(errors.admin.FINALIZE_NOT_ALLOWED)
        requested, in_flight = [], []
        for piece in await pieces_of(self._local, dossier):
            if piece.kind != int(DocumentKind.GENERATED) or piece.status != int(
                ProcedureDocumentStatus.READY
            ):
                continue
            try:
                if await self._generator().finalize(piece) is not None:
                    requested.append(piece.id)
            except ErrorException as exc:
                if exc.error is not errors.admin.RENDER_ALREADY_IN_FLIGHT:
                    raise
                in_flight.append(piece.id)
        return {"requested": requested, "in_flight": in_flight}

    async def regenerate(
        self, id_community: int, dossier_id: int, document_ids: Sequence[int] | None
    ) -> dict[str, list[int]]:
        """One click for the stale pieces (or the ones named): each is regenerated
        from a fresh pre-fill with its reviewer's edits carried over. A piece
        whose current version cannot say what was typed needs the review form."""
        dossier, _plugin, _ctx = await self._load(id_community, dossier_id)
        if dossier.frozen_at is not None:
            raise _conflict(errors.admin.GENERATION_NOT_ALLOWED)
        pieces = await pieces_of(self._local, dossier)
        if document_ids is None:
            targets = [
                p
                for p in pieces
                if p.kind == int(DocumentKind.GENERATED)
                and p.status == int(ProcedureDocumentStatus.STALE)
            ]
        else:
            by_id = {p.id: p for p in pieces}
            if any(i not in by_id for i in document_ids):
                raise _not_found(errors.admin.PIECE_NOT_FOUND)
            targets = [by_id[i] for i in dict.fromkeys(document_ids)]
        outcome: dict[str, list[int]] = {"requested": [], "needs_review": [], "in_flight": []}
        for piece in targets:
            piece_id = piece.id
            try:
                await self._generator().regenerate(document_id=piece_id)
                outcome["requested"].append(piece_id)
            except ErrorException as exc:
                if exc.error is errors.admin.REVIEW_REQUIRED:
                    outcome["needs_review"].append(piece_id)
                elif exc.error is errors.admin.RENDER_ALREADY_IN_FLIGHT:
                    outcome["in_flight"].append(piece_id)
                else:
                    raise
        return outcome

    async def verify(
        self, id_community: int, dossier_id: int, document_id: int, version_id: int
    ) -> str:
        """Regenerate one version from what it froze and compare the bytes; the
        outcome lands in ``version_check``. Returns the docgen request id."""
        dossier, _plugin, _ctx = await self._load(id_community, dossier_id)
        piece = await self._local.scalar(
            select(DocumentModel).where(
                DocumentModel.id == document_id, DocumentModel.id_dossier == dossier.id
            )
        )
        if piece is None:
            raise _not_found(errors.admin.PIECE_NOT_FOUND)
        version = await self._local.scalar(
            select(DocumentVersionModel).where(
                DocumentVersionModel.id == version_id,
                DocumentVersionModel.id_document == piece.id,
            )
        )
        if version is None:
            raise _not_found(errors.admin.VERSION_NOT_FOUND)
        return await self._generator().verify(piece, version)

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
            # A file a person supplies is what they mean to send: there is no
            # such thing as an uploaded draft.
            stage=int(VersionStage.FINAL),
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
        await self._require_finals(dossier)
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
        released = await self._release_deferred(plugin, dossier, day)
        await self._local.commit()
        await self._audit(
            AuditActions.DOSSIER_STATUS_CHANGED, dossier, {"to_step": target, "sent_on": str(day)}
        )
        await self._after(id_community, [dossier_id, *released])
        return await self.procedure(id_community, dossier_id)

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
            return await self._proposal(dossier, ctx, response.code, target, allowed, day)
        if not allowed:
            raise _conflict(errors.admin.TRANSITION_NOT_ALLOWED)
        if target is not None and definition.step(target).common_status == ProcedureStatus.SENT:
            await self._require_finals(dossier)
        released: list[int] = []

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
            try:
                await move_to_step(
                    self._local,
                    ctx,
                    dossier,
                    target,
                    day=day,
                    actor=actor,
                    context={"reason": response.code, "correspondence": message.id},
                )
            except EffectsNotWrittenError:
                # Going in force is the effect date's (or a step change by
                # hand, core first), never a response's.
                await self._local.rollback()
                raise _conflict(errors.admin.TRANSITION_NOT_ALLOWED) from None
            self._freeze_if_sent(dossier, day)
            released = await self._release_deferred(plugin, dossier, day)
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
        await self._after(id_community, [dossier_id, *released])
        return await self.procedure(id_community, dossier_id)

    async def _proposal(
        self,
        dossier: DossierModel,
        ctx: ProcedureContext,
        code: str,
        target: str | None,
        allowed: bool,
        day: datetime.date,
    ) -> dict[str, Any]:
        """What ``respond`` would do, the CRM's badges included. Reads the
        catalogue and both databases, writes nothing (the effects are planned
        without saving their pins)."""
        definition = ctx.definition
        events = [response_event(code)]
        if target is not None and allowed:
            events.append(step_event(target))
        started = []
        met: list[str] = []
        effective = dossier.effective_date
        for event in events:
            met.extend(r.code for r in definition.rules_closed_by(event, dossier.variant))
            for rule in definition.rules_started_by(event, dossier.variant):
                due = due_date(rule, ctx.calendar, anchor=day)
                if rule.kind_of_deadline == DeadlineKind.EFFECT:
                    due = effective = effect_date(due, dossier.requested_effective_date)
                started.append(
                    {
                        "code": rule.code,
                        "labels": rule.labels,
                        "due_date": due,
                        "party": int(rule.deadline_party),
                        "kind": int(rule.kind_of_deadline),
                    }
                )
        response = definition.response_type(code)
        effects: list[dict[str, Any]] = []
        if allowed:
            writes = await plan_effects(
                self._local,
                self._crm_read,
                self._registry,
                dossier.id_community,
                [dossier.id],
                overrides={dossier.id: StepOverride(target or str(dossier.step), effective)},
                persist_pins=False,
            )
            effects = [
                {
                    "subject_type": w.subject_type,
                    "subject_ref": w.subject_ref,
                    "regulatory_status": int(w.regulatory_status),
                    "valid_from": w.valid_from,
                    "valid_to": w.valid_to,
                }
                for w in writes
            ]
        return {
            "type": code,
            "labels": response.labels,
            "direction": response.direction,
            "from_step": str(dossier.step),
            "to_step": target,
            "to_step_labels": definition.step(target).labels if target else None,
            "to_status": int(definition.step(target).common_status) if target else None,
            "transition_allowed": allowed,
            "deadlines_started": started,
            "deadlines_met": sorted(set(met)),
            "effective_date": effective if effective != dossier.effective_date else None,
            "effects": effects,
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
