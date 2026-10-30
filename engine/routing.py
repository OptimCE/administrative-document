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
from dataclasses import dataclass, field

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.regions import region_for_regulator
from engine.activation import reconcile_activation
from engine.baseline import ensure_baseline, photographed
from engine.contracts import REMOVAL_KINDS, PluginRegistry, plugin_for
from engine.events import CoreEvent, DetectedChange, change_of
from engine.filing import LIVE_STATUSES, RegionOf, file_change
from ports.crm_engine import EngineCrmRead
from shared.const import ChangeStatus, ChangeSubjectType, EventOutcome, ProcedureStatus
from shared.models.local_models import (
    ChangeModel,
    DossierModel,
    ModuleActivationModel,
    ProcessedEventModel,
)

logger = logging.getLogger(__name__)

# The changes whose subject the core tracks, and how they name it.
_TRACKED_KINDS = {
    "meter_added": ChangeSubjectType.METER,
    "installation_added": ChangeSubjectType.METER,
    "key_changed": ChangeSubjectType.KEY,
}
_METER_ADDITIONS = ("meter_added", "installation_added")


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
    plugin = plugin_for(registry, state.regulator)
    if plugin is not None:
        # The first event of a period may come before anyone opens the
        # dashboard: the photo is taken here as well.
        await ensure_baseline(
            local,
            crm,
            plugin,
            period,
            now=now or datetime.datetime.now(datetime.UTC),
            region_of=region_of,
        )
    if event.occurred_at < period.enabled_at:
        return EventOutcome.BEFORE_ACTIVATION

    change = change_of(event)
    if change is None:
        logger.info("core event type %s files nothing here", event.event_type)
        return EventOutcome.NO_PROCEDURE
    if await _already_reflected(local, crm, period, event, change):
        return EventOutcome.ALREADY_REFLECTED
    if plugin is None:
        return EventOutcome.REGULATOR_NOT_SUPPORTED
    targets = plugin.catalogue().handling(change.kind, event.day)
    for entry in targets:
        filed = await file_change(
            local,
            plugin,
            entry,
            change,
            id_community=event.id_community,
            day=event.day,
            at=event.occurred_at,
            source_event_id=event.event_id,
            region_of=region_of,
            context={"event_id": str(event.event_id)},
        )
        if filed is not None:
            filing.dossiers.append(filed[0])
            filing.changes.append(filed[1])
    return EventOutcome.ROUTED if filing.changes else EventOutcome.NO_PROCEDURE


async def _already_reflected(
    local: AsyncSession,
    crm: EngineCrmRead,
    period: ModuleActivationModel,
    event: CoreEvent,
    change: DetectedChange,
) -> bool:
    """Is this change already known? An addition is when the core tracks it,
    when it is on the baseline still awaiting confirmation (spec: events in
    flight are "comparés à la photo"), or when a live procedure already
    carries it (a refused, lapsed or cancelled one carries nothing).

    A removal is skipped only on positive evidence that nothing was ever
    authorised to leave: the meter's latest addition was refused (or its
    procedure lapsed) and the core holds no live episode of it. Short of that
    it is filed, because before the baseline is confirmed the annexe cannot
    know the meter was absent (spec, "Rangement", step 2)."""
    if change.kind in REMOVAL_KINDS and change.id_sharing_operation is not None:
        return await _removal_reflected(local, crm, event, change)
    subject_type = _TRACKED_KINDS.get(change.kind)
    if subject_type is None or change.id_sharing_operation is None:
        return False
    operation = change.id_sharing_operation
    if period.baseline_confirmed_at is None and photographed(
        period.baseline_json or {}, int(subject_type), operation, change.subject_ref
    ):
        return True
    filed = await local.scalar(
        select(ChangeModel.id)
        .outerjoin(DossierModel, DossierModel.id == ChangeModel.id_dossier)
        .where(
            ChangeModel.id_community == event.id_community,
            ChangeModel.id_sharing_operation == operation,
            ChangeModel.subject_type == int(subject_type),
            ChangeModel.subject_ref == change.subject_ref,
            ChangeModel.kind == change.kind,
            or_(
                and_(
                    ChangeModel.status == int(ChangeStatus.ATTACHED),
                    DossierModel.status.in_(LIVE_STATUSES),
                ),
                ChangeModel.status == int(ChangeStatus.DEFERRED),
            ),
        )
        .limit(1)
    )
    if filed is not None:
        return True
    on = _effective_day(event)
    tracked = await crm.tracked_subjects(
        id_community=event.id_community,
        id_sharing_operation=change.id_sharing_operation,
        subject_type=int(subject_type),
        refs=[change.subject_ref],
        on=on,
    )
    return change.subject_ref in tracked


async def _removal_reflected(
    local: AsyncSession, crm: EngineCrmRead, event: CoreEvent, change: DetectedChange
) -> bool:
    operation = int(change.id_sharing_operation or 0)
    latest = (
        await local.execute(
            select(ChangeModel.status, DossierModel.status)
            .outerjoin(DossierModel, DossierModel.id == ChangeModel.id_dossier)
            .where(
                ChangeModel.id_community == event.id_community,
                ChangeModel.id_sharing_operation == operation,
                ChangeModel.subject_type == int(ChangeSubjectType.METER),
                ChangeModel.subject_ref == change.subject_ref,
                ChangeModel.kind.in_(_METER_ADDITIONS),
            )
            .order_by(ChangeModel.id.desc())
            .limit(1)
        )
    ).first()
    if latest is None:
        return False
    change_status, procedure_status = latest
    never_authorised = change_status == int(ChangeStatus.REFUSED) or (
        change_status == int(ChangeStatus.ATTACHED)
        and procedure_status == int(ProcedureStatus.LAPSED)
    )
    if not never_authorised:
        return False
    live = await crm.live_episode(
        event.id_community,
        id_sharing_operation=operation,
        subject_type=int(ChangeSubjectType.METER),
        subject_ref=change.subject_ref,
    )
    return live is None


def _effective_day(event: CoreEvent) -> datetime.date:
    for key in ("effective_date", "start_date"):
        value = event.payload.get(key)
        if isinstance(value, str):
            try:
                return datetime.date.fromisoformat(value[:10])
            except ValueError:
                break
    return event.day
