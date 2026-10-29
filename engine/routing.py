"""Filing a core event into procedures (spec, "Moteur événements → démarches").

For each event, in the caller's transaction:

1. Idempotency: an event already in ``processed_event`` raises
   ``DuplicateEventError`` and nothing is written.
2. Activation, read from the CRM on every event: module inactive, or the event
   older than the current period, and nothing is created.
3. The event becomes a change (``engine.events``). One the core already
   reflects (a meter added that is already tracked) creates nothing.
4. The community's regulator picks the plugin; its catalogue in force picks
   the procedures the change feeds. No plugin, or no procedure: nothing.
5. Per target procedure: the change is recorded; if its inverse still sits in
   an open draft (added then removed before sending) both are cancelled, and a
   system draft left empty is cancelled too. Otherwise it joins the open draft
   of that family (created if none; a READY one goes back to draft).
6. The draft is recomputed: variant (the plugin's), reference GRD, pieces,
   generated pieces marked stale, next action.
7. ``processed_event`` records the outcome, whatever it is.

The caller commits, then publishes the realtime hints for the procedures
returned: never before the commit.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.regions import region_for_regulator
from engine.activation import reconcile_activation
from engine.catalogue import CatalogueEntry
from engine.contracts import (
    ChangeFact,
    PluginRegistry,
    ProcedureFacts,
    RegulatorPlugin,
    plugin_for,
)
from engine.events import INVERSE, CoreEvent, DetectedChange, change_of
from engine.lifecycle import (
    LifecycleError,
    ProcedureContext,
    context_for,
    mark_generated_stale,
    open_procedure,
    refresh_next_action,
    sync_pieces,
    take_automatic_edge,
)
from ports.crm_engine import EngineCrmRead
from shared.const import (
    ChangeStatus,
    ChangeSubjectType,
    EventOutcome,
    OpenedBy,
    ProcedureScope,
    ProcedureStatus,
)
from shared.models.local_models import (
    ChangeModel,
    DocumentModel,
    DocumentVersionModel,
    DossierModel,
    ProcedureDefinitionModel,
    ProcessedEventModel,
)

logger = logging.getLogger(__name__)

_OPEN = (int(ProcedureStatus.DRAFT), int(ProcedureStatus.READY))

# The changes whose subject the core tracks, and how they name it.
_TRACKED_KINDS = {
    "meter_added": ChangeSubjectType.METER,
    "installation_added": ChangeSubjectType.METER,
    "key_changed": ChangeSubjectType.KEY,
}


class DuplicateEventError(Exception):
    """The event was already processed. Nothing was written."""


@dataclass(frozen=True)
class RoutingResult:
    outcome: EventOutcome
    dossiers: tuple[int, ...] = ()
    changes: tuple[int, ...] = ()


@dataclass
class _Filing:
    dossiers: list[int] = field(default_factory=list)
    changes: list[int] = field(default_factory=list)


RegionOf = Callable[[str], int | None]


async def route_event(
    event: CoreEvent,
    *,
    local: AsyncSession,
    crm: EngineCrmRead,
    registry: PluginRegistry,
    region_of: RegionOf = region_for_regulator,
    now: datetime.datetime | None = None,
) -> RoutingResult:
    if await local.get(ProcessedEventModel, event.event_id) is not None:
        raise DuplicateEventError(str(event.event_id))

    filing = _Filing()
    outcome = await _route(event, local, crm, registry, region_of, now, filing)

    recorded = await local.execute(
        pg_insert(ProcessedEventModel)
        .values(
            event_id=event.event_id,
            id_community=event.id_community,
            event_type=event.event_type,
            outcome=int(outcome),
        )
        .on_conflict_do_nothing()
    )
    if not recorded.rowcount:
        raise DuplicateEventError(str(event.event_id))  # processed concurrently
    return RoutingResult(
        outcome=outcome,
        dossiers=tuple(dict.fromkeys(filing.dossiers)),
        changes=tuple(filing.changes),
    )


async def _route(
    event: CoreEvent,
    local: AsyncSession,
    crm: EngineCrmRead,
    registry: PluginRegistry,
    region_of: RegionOf,
    now: datetime.datetime | None,
    filing: _Filing,
) -> EventOutcome:
    state = await crm.module_state(event.id_community)
    period = await reconcile_activation(local, event.id_community, state, now=now)
    if period is None:
        return EventOutcome.MODULE_DISABLED
    if event.occurred_at < period.enabled_at:
        return EventOutcome.BEFORE_ACTIVATION

    change = change_of(event)
    if change is None:
        logger.info("core event type %s files nothing here", event.event_type)
        return EventOutcome.NO_PROCEDURE
    if await _already_reflected(crm, event, change):
        return EventOutcome.ALREADY_REFLECTED

    plugin = plugin_for(registry, state.regulator)
    if plugin is None:
        return EventOutcome.REGULATOR_NOT_SUPPORTED
    targets = plugin.catalogue().handling(change.kind, event.day)
    for entry in targets:
        await _file(event, change, plugin, entry, local, region_of, filing)
    return EventOutcome.ROUTED if filing.changes else EventOutcome.NO_PROCEDURE


async def _already_reflected(crm: EngineCrmRead, event: CoreEvent, change: DetectedChange) -> bool:
    """Is this change already the authorised state? Only additions can be: a
    removal is never skipped, because before the baseline is confirmed the
    annexe cannot know the meter was absent (spec, "Rangement", step 2)."""
    subject_type = _TRACKED_KINDS.get(change.kind)
    if subject_type is None or change.id_sharing_operation is None:
        return False
    on = _effective_day(event)
    tracked = await crm.tracked_subjects(
        id_community=event.id_community,
        id_sharing_operation=change.id_sharing_operation,
        subject_type=int(subject_type),
        refs=[change.subject_ref],
        on=on,
    )
    return change.subject_ref in tracked


def _effective_day(event: CoreEvent) -> datetime.date:
    for key in ("effective_date", "start_date"):
        value = event.payload.get(key)
        if isinstance(value, str):
            try:
                return datetime.date.fromisoformat(value[:10])
            except ValueError:
                break
    return event.day


async def _file(
    event: CoreEvent,
    change: DetectedChange,
    plugin: RegulatorPlugin,
    entry: CatalogueEntry,
    local: AsyncSession,
    region_of: RegionOf,
    filing: _Filing,
) -> None:
    definition = entry.definition
    operation_scope = definition.procedure_scope == ProcedureScope.OPERATION
    if operation_scope and change.id_sharing_operation is None:
        # A community event feeding a per-operation procedure means one per
        # operation (a new representative). No such event has an emitter yet.
        logger.warning(
            "%s %s is per operation; community event %s not filed there",
            plugin.code,
            definition.code,
            event.event_id,
        )
        return
    operation = change.id_sharing_operation if operation_scope else None
    period_key = str(event.day.year) if definition.opens_annually else None

    row = ChangeModel(
        id_community=event.id_community,
        id_sharing_operation=operation,
        kind=change.kind,
        subject_type=int(change.subject_type),
        subject_ref=change.subject_ref,
        payload_json=dict(change.payload),
        source_event_id=event.event_id,
        procedure_code=definition.code,
        status=int(ChangeStatus.PENDING),
        detected_at=event.occurred_at,
    )
    local.add(row)

    inverse = await _open_inverse(local, event.id_community, operation, change, definition.code)
    if inverse is not None:
        holding = await _dossier(local, inverse.id_dossier)
        inverse.status = int(ChangeStatus.CANCELLED)
        row.status = int(ChangeStatus.CANCELLED)
        row.id_dossier = holding.id
        await local.flush()
        ctx = await _context_of(local, plugin, holding)
        await _content_changed(local, ctx, holding, event.day)
        if not await _cancel_if_emptied(local, ctx, holding, event.day):
            await _recompute(local, ctx, holding, event.day)
        filing.dossiers.append(holding.id)
        filing.changes.append(row.id)
        return

    dossier = await _open_draft(local, event.id_community, operation, definition.family, period_key)
    if dossier is None:
        ctx = await context_for(local, plugin, definition.code, definition.version)
        region = region_of(plugin.code)
        if region is None:
            raise LifecycleError(f"regulator {plugin.code} has no region in regulators.json")
        dossier = await open_procedure(
            local,
            ctx,
            id_community=event.id_community,
            id_sharing_operation=operation,
            region=int(region),
            variant=plugin.variant(definition, [change.fact()]),
            opened_by=OpenedBy.SYSTEM,
            day=event.day,
            period_key=period_key,
            context={"event_id": str(event.event_id)},
        )
    else:
        ctx = await _context_of(local, plugin, dossier)

    row.status = int(ChangeStatus.ATTACHED)
    row.id_dossier = dossier.id
    await local.flush()
    await _content_changed(local, ctx, dossier, event.day)
    await _recompute(local, ctx, dossier, event.day)
    filing.dossiers.append(dossier.id)
    filing.changes.append(row.id)


async def _dossier(local: AsyncSession, dossier_id: int | None) -> DossierModel:
    dossier = await local.get(DossierModel, dossier_id) if dossier_id is not None else None
    if dossier is None:
        raise LifecycleError(f"change refers to missing procedure {dossier_id}")
    return dossier


async def _context_of(
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
            DossierModel.status.in_(_OPEN),
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
            DossierModel.status.in_(_OPEN),
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


async def _content_changed(
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


async def _cancel_if_emptied(
    local: AsyncSession, ctx: ProcedureContext, dossier: DossierModel, day: datetime.date
) -> bool:
    """A draft the system opened, left with no change and no file, is cancelled
    (if the catalogue lets the engine take that edge). One a person opened, or
    worked on, stays."""
    if dossier.opened_by != int(OpenedBy.SYSTEM) or dossier.status != int(ProcedureStatus.DRAFT):
        return False
    if await _active_changes(local, dossier):
        return False
    versions = await local.scalar(
        select(func.count(DocumentVersionModel.id))
        .join(DocumentModel, DocumentModel.id == DocumentVersionModel.id_document)
        .where(DocumentModel.id_dossier == dossier.id)
    )
    if versions:
        return False
    return await take_automatic_edge(
        local, ctx, dossier, ProcedureStatus.CANCELLED, day=day, context={"reason": "compensated"}
    )


async def _recompute(
    local: AsyncSession, ctx: ProcedureContext, dossier: DossierModel, day: datetime.date
) -> None:
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
