"""Filing one change into one procedure: the part of the routing that the
baseline confirmation shares (steps 5 and 6 of ``engine.routing``).

- If the change's inverse still sits in an open draft (added then removed
  before sending), both are cancelled, and a system draft left with nothing is
  cancelled too.
- Otherwise the change joins the open draft of its family, created if there is
  none; a READY draft goes back to draft and its generated pieces go stale.
- The draft is then recomputed: the plugin's variant (a converted July draft
  keeps its own), the reference GRD, the pieces, the next action.

Where a change goes (``placements``): each procedure handling it; one per
operation for an operation procedure fed by a community change (a new
representative is notified for every operation); and for an operation, an
absorbing procedure's open draft instead of the operation procedures (a new
operation's first meters go into its authorisation request).
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.regions import region_for_regulator
from engine.catalogue import Catalogue, CatalogueEntry
from engine.contracts import (
    ChangeFact,
    PluginRegistry,
    ProcedureFacts,
    RegulatorPlugin,
    plugin_for,
)
from engine.events import INVERSE, UNDO_ONLY, DetectedChange
from engine.lifecycle import (
    LifecycleError,
    ProcedureContext,
    cancel_if_emptied,
    context_for,
    mark_generated_stale,
    open_procedure,
    refresh_next_action,
    supply_piece,
    sync_pieces,
    take_automatic_edge,
)
from shared.const import (
    ChangeStatus,
    ChangeSubjectType,
    DocumentKind,
    OpenedBy,
    ProcedureDocumentStatus,
    ProcedureScope,
    ProcedureStatus,
)
from shared.models.local_models import (
    ChangeModel,
    DocumentModel,
    DossierModel,
    ProcedureDefinitionModel,
)

logger = logging.getLogger(__name__)

OPEN = (int(ProcedureStatus.DRAFT), int(ProcedureStatus.READY))
# A procedure still carrying its changes: open, or sent and not yet decided.
LIVE_STATUSES = (*OPEN, int(ProcedureStatus.SENT))

RegionOf = Callable[[str], int | None]


async def placements(
    local: AsyncSession,
    catalogue: Catalogue,
    change: DetectedChange,
    *,
    id_community: int,
    day: datetime.date,
    operations: Sequence[int] = (),
) -> list[tuple[CatalogueEntry, DetectedChange]]:
    """The procedures a change is filed into, each with the change as it is
    filed there. ``operations`` are the community's operations a community
    change concerns (those holding meters): an operation procedure takes one
    such change per operation."""
    handlers = catalogue.handling(change.kind, day)
    absorbers = catalogue.absorbing(change.kind, day)
    placed = [
        (e, change) for e in handlers if e.definition.procedure_scope == ProcedureScope.COMMUNITY
    ]
    per_operation = [
        e for e in handlers if e.definition.procedure_scope == ProcedureScope.OPERATION
    ]
    if not per_operation and not absorbers:
        return placed
    if change.id_sharing_operation is not None:
        filed = [(change.id_sharing_operation, change)]
    else:
        filed = [
            (operation, dataclasses.replace(change, id_sharing_operation=operation))
            for operation in dict.fromkeys(operations)
        ]
    for operation, operation_change in filed:
        absorber = None
        for entry in absorbers:
            if await _open_draft(local, id_community, operation, entry.definition.family, None):
                absorber = entry
                break
        if absorber is not None:
            placed.append((absorber, operation_change))
        else:
            placed.extend((e, operation_change) for e in per_operation)
    return placed


async def file_change(
    local: AsyncSession,
    plugin: RegulatorPlugin,
    entry: CatalogueEntry,
    change: DetectedChange,
    *,
    id_community: int,
    day: datetime.date,
    at: datetime.datetime,
    source_event_id: uuid.UUID | None,
    region_of: RegionOf = region_for_regulator,
    context: dict[str, Any] | None = None,
) -> tuple[int, int] | None:
    """File one change into one target procedure (steps 5 and 6 above).
    Returns (procedure id, change id), or None when the target cannot take it.
    Shared by the router and the baseline confirmation."""
    definition = entry.definition
    operation_scope = definition.procedure_scope == ProcedureScope.OPERATION
    if operation_scope and change.id_sharing_operation is None:
        # ``placements`` gives a community change one operation per filing.
        logger.warning(
            "%s %s is per operation; community change %s not filed there",
            plugin.code,
            definition.code,
            source_event_id,
        )
        return None
    operation = change.id_sharing_operation if operation_scope else None
    period_key = str(day.year) if definition.opens_annually else None

    inverse = await _open_inverse(local, id_community, operation, change, definition.code)
    if inverse is None and change.kind in UNDO_ONLY:
        return None

    row = ChangeModel(
        id_community=id_community,
        id_sharing_operation=operation,
        kind=change.kind,
        subject_type=int(change.subject_type),
        subject_ref=change.subject_ref,
        payload_json=dict(change.payload),
        source_event_id=source_event_id,
        procedure_code=definition.code,
        status=int(ChangeStatus.PENDING),
        detected_at=at,
    )
    local.add(row)

    if inverse is not None:
        holding = await _dossier(local, inverse.id_dossier)
        inverse.status = int(ChangeStatus.CANCELLED)
        row.status = int(ChangeStatus.CANCELLED)
        row.id_dossier = holding.id
        await local.flush()
        ctx = await context_of_procedure(local, plugin, holding)
        await content_changed(local, ctx, holding, day)
        if not await cancel_if_emptied(local, ctx, holding, day):
            await recompute(local, ctx, holding)
        return holding.id, row.id

    dossier = await _open_draft(local, id_community, operation, definition.family, period_key)
    if dossier is None:
        ctx = await context_for(local, plugin, definition.code, definition.version)
        region = region_of(plugin.code)
        if region is None:
            raise LifecycleError(f"regulator {plugin.code} has no region in regulators.json")
        dossier = await open_procedure(
            local,
            ctx,
            id_community=id_community,
            id_sharing_operation=operation,
            region=int(region),
            variant=plugin.variant(definition, [change.fact()]),
            opened_by=OpenedBy.SYSTEM,
            day=day,
            period_key=period_key,
            context=context,
        )
    else:
        ctx = await context_of_procedure(local, plugin, dossier)

    row.status = int(ChangeStatus.ATTACHED)
    row.id_dossier = dossier.id
    await local.flush()
    if change.kind == "key_changed":
        await _supersede_keys(local, dossier, row)
    await content_changed(local, ctx, dossier, day)
    await recompute(local, ctx, dossier)
    return dossier.id, row.id


async def _supersede_keys(local: AsyncSession, dossier: DossierModel, latest: ChangeModel) -> None:
    """An operation has one key: a new key in the draft replaces the one the
    draft already held, which is notified to nobody."""
    for older in await local.scalars(
        select(ChangeModel).where(
            ChangeModel.id_community == dossier.id_community,
            ChangeModel.id_dossier == dossier.id,
            ChangeModel.kind == "key_changed",
            ChangeModel.status == int(ChangeStatus.ATTACHED),
            ChangeModel.id != latest.id,
        )
    ):
        older.status = int(ChangeStatus.CANCELLED)
        older.payload_json = {**(older.payload_json or {}), "superseded_by": latest.id}
    await local.flush()


async def _dossier(local: AsyncSession, dossier_id: int | None) -> DossierModel:
    dossier = await local.get(DossierModel, dossier_id) if dossier_id is not None else None
    if dossier is None:
        raise LifecycleError(f"change refers to missing procedure {dossier_id}")
    return dossier


async def context_of_procedure(
    local: AsyncSession, plugin: RegulatorPlugin, dossier: DossierModel
) -> ProcedureContext:
    """The context of the version the procedure was opened under, which may no
    longer be the one in force."""
    row = await local.get(ProcedureDefinitionModel, dossier.id_procedure_definition)
    if row is None:
        raise LifecycleError(f"procedure {dossier.id} has no definition")
    return await context_for(local, plugin, row.code, row.version)


async def _open_draft(
    local: AsyncSession,
    id_community: int,
    operation: int | None,
    family: str,
    period_key: str | None,
) -> DossierModel | None:
    draft: DossierModel | None = await local.scalar(
        select(DossierModel).where(
            DossierModel.id_community == id_community,
            DossierModel.family == family,
            DossierModel.status.in_(OPEN),
            DossierModel.id_sharing_operation.is_not_distinct_from(operation),
            DossierModel.period_key.is_not_distinct_from(period_key),
        )
    )
    return draft


async def _open_inverse(
    local: AsyncSession,
    id_community: int,
    operation: int | None,
    change: DetectedChange,
    procedure_code: str,
) -> ChangeModel | None:
    inverse_kind = INVERSE.get(change.kind)
    if inverse_kind is None:
        return None
    inverse: ChangeModel | None = await local.scalar(
        select(ChangeModel)
        .join(DossierModel, DossierModel.id == ChangeModel.id_dossier)
        .where(
            ChangeModel.id_community == id_community,
            ChangeModel.kind == inverse_kind,
            ChangeModel.subject_type == int(change.subject_type),
            ChangeModel.subject_ref == change.subject_ref,
            ChangeModel.procedure_code == procedure_code,
            ChangeModel.status == int(ChangeStatus.ATTACHED),
            ChangeModel.id_sharing_operation.is_not_distinct_from(operation),
            DossierModel.status.in_(OPEN),
        )
        .order_by(ChangeModel.id.desc())
        .limit(1)
    )
    return inverse


async def _active_changes(local: AsyncSession, dossier: DossierModel) -> list[ChangeModel]:
    return list(
        await local.scalars(
            select(ChangeModel)
            .where(
                ChangeModel.id_community == dossier.id_community,
                ChangeModel.id_dossier == dossier.id,
                ChangeModel.status == int(ChangeStatus.ATTACHED),
            )
            .order_by(ChangeModel.id)
        )
    )


async def content_changed(
    local: AsyncSession, ctx: ProcedureContext, dossier: DossierModel, day: datetime.date
) -> None:
    """What the draft holds changed: its generated pieces are outdated, and a
    READY draft goes back to draft (spec, "retour en Brouillon si nouveau
    changement")."""
    await mark_generated_stale(local, dossier)
    if dossier.status == int(ProcedureStatus.READY):
        await take_automatic_edge(
            local, ctx, dossier, ProcedureStatus.DRAFT, day=day, context={"reason": "new_change"}
        )


async def recompute(local: AsyncSession, ctx: ProcedureContext, dossier: DossierModel) -> None:
    """Step 6: the procedure's variant, reference GRD, pieces and next action,
    from its active changes."""
    changes = await _active_changes(local, dossier)
    facts = tuple(
        ChangeFact(
            kind=c.kind,
            subject_type=ChangeSubjectType(c.subject_type),
            subject_ref=c.subject_ref,
            payload=c.payload_json,
        )
        for c in changes
    )
    # A converted July draft keeps its variant: it has no change rows for what
    # July held, and migration 0003 chose the route that cannot under-notify.
    if dossier.opened_by != int(OpenedBy.MIGRATION):
        variant = ctx.plugin.variant(ctx.definition, facts)
        if variant is not None and variant not in ctx.definition.variants:
            raise LifecycleError(f"{ctx.plugin.code} chose unknown variant {variant!r}")
        dossier.variant = variant

    grds = {str(c.payload_json["grd"]) for c in changes if c.payload_json.get("grd")}
    if dossier.grd_ref is None and len(grds) == 1:
        dossier.grd_ref = grds.pop()

    requirements = ctx.plugin.required_documents(
        ProcedureFacts(
            definition=ctx.definition,
            variant=dossier.variant,
            id_community=dossier.id_community,
            id_sharing_operation=dossier.id_sharing_operation,
            changes=facts,
            requested_effective_date=dossier.requested_effective_date,
            effective_date=dossier.effective_date,
        )
    )
    await sync_pieces(local, dossier, requirements)
    await refresh_next_action(local, ctx, dossier)


async def refile_change(
    local: AsyncSession,
    plugin: RegulatorPlugin,
    change: ChangeModel,
    *,
    day: datetime.date,
    region_of: RegionOf = region_for_regulator,
) -> DossierModel:
    """Move a deferred change into the open draft of its family (created if
    none): "reporter à la prochaine démarche". The row moves, it is not copied,
    so it keeps its source event and stays unique per (event, procedure)."""
    entry = plugin.catalogue().in_force(change.procedure_code, day)
    if entry is None:
        raise LifecycleError(f"{plugin.code} has no {change.procedure_code} in force on {day}")
    definition = entry.definition
    period_key = str(day.year) if definition.opens_annually else None
    dossier = await _open_draft(
        local, change.id_community, change.id_sharing_operation, definition.family, period_key
    )
    if dossier is None:
        ctx = await context_for(local, plugin, definition.code, definition.version)
        region = region_of(plugin.code)
        if region is None:
            raise LifecycleError(f"regulator {plugin.code} has no region in regulators.json")
        detected = DetectedChange(
            kind=change.kind,
            subject_type=ChangeSubjectType(change.subject_type),
            subject_ref=change.subject_ref,
            id_sharing_operation=change.id_sharing_operation,
            payload=change.payload_json,
        )
        dossier = await open_procedure(
            local,
            ctx,
            id_community=change.id_community,
            id_sharing_operation=change.id_sharing_operation,
            region=int(region),
            variant=plugin.variant(definition, [detected.fact()]),
            opened_by=OpenedBy.SYSTEM,
            day=day,
            period_key=period_key,
            context={"reason": "deferred", "change": change.id},
        )
    else:
        ctx = await context_of_procedure(local, plugin, dossier)
    change.status = int(ChangeStatus.ATTACHED)
    change.id_dossier = dossier.id
    await local.flush()
    await content_changed(local, ctx, dossier, day)
    await recompute(local, ctx, dossier)
    return dossier


async def piece_rendered(
    local: AsyncSession, registry: PluginRegistry, piece: DocumentModel, *, day: datetime.date
) -> bool:
    """A generated piece of an engine procedure got its rendered version: it is
    ready, and the procedure's readiness follows. A July document (no ``kind``)
    is left alone: there a human marks it ready. Returns whether it moved."""
    if piece.kind != int(DocumentKind.GENERATED) or piece.status not in (
        int(ProcedureDocumentStatus.MISSING),
        int(ProcedureDocumentStatus.STALE),
    ):
        return False
    dossier = await local.get(DossierModel, piece.id_dossier)
    plugin = plugin_for(registry, dossier.regulator) if dossier is not None else None
    if dossier is None or plugin is None:
        return False
    ctx = await context_of_procedure(local, plugin, dossier)
    await supply_piece(
        local,
        ctx,
        dossier,
        piece,
        status=ProcedureDocumentStatus.READY,
        day=day,
        actor="system:docgen",
        context={"reason": "rendered"},
    )
    return True
