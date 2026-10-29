"""A procedure's life: its journaled steps, its deadlines, its pieces.

Shared by the event router, the API and the daily jobs, so the three cannot
drift. Everything works in the caller's transaction and never commits.

The journal writes the status (``propagate_journal_head``): a step change is
an appended ``status_event`` row, then a refresh, never an assignment.
Entering a step is also an *event* for the catalogue (``step:<code>``): the
rules it closes are met, the rules it anchors start. One deadline per rule is
open at a time; re-entering a step does not restart a clock still running.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.calendar import WorkingCalendar
from engine.catalogue import ProcedureDefinition, step_event
from engine.contracts import DocumentRequirement, RegulatorPlugin
from engine.deadlines import due_date
from shared.const import (
    DeadlineStatus,
    DocOrigin,
    DocumentKind,
    OpenedBy,
    ProcedureDocumentStatus,
    ProcedureStatus,
    SubjectType,
)
from shared.models.local_models import (
    DeadlineModel,
    DeadlineRuleModel,
    DocumentModel,
    DossierModel,
    ProcedureDefinitionModel,
    StatusEventModel,
)

ENGINE_ACTOR = "system:engine"

# Entering one of these ends the procedure: its running clocks are cancelled.
# A refusal or a procedure in force keeps them (appeal periods, the effect).
_ENDS_THE_CLOCKS = (ProcedureStatus.CANCELLED, ProcedureStatus.LAPSED)

# A piece in one of these statuses counts as there.
_PIECE_THERE = (ProcedureDocumentStatus.READY, ProcedureDocumentStatus.SIGNED)


class LifecycleError(Exception):
    """A move the catalogue does not allow, or a catalogue the database lacks."""


@dataclass(frozen=True)
class ProcedureContext:
    """What every operation on a procedure of one definition needs."""

    plugin: RegulatorPlugin
    definition: ProcedureDefinition
    id_definition: int
    rule_ids: dict[str, int]
    calendar: WorkingCalendar


async def context_for(
    session: AsyncSession, plugin: RegulatorPlugin, code: str, version: int
) -> ProcedureContext:
    entry = plugin.catalogue().get(code, version)
    if entry is None:
        raise LifecycleError(f"{plugin.code} has no {code} v{version}")
    id_definition = await session.scalar(
        select(ProcedureDefinitionModel.id).where(
            ProcedureDefinitionModel.regulator == plugin.code,
            ProcedureDefinitionModel.code == code,
            ProcedureDefinitionModel.version == version,
            ProcedureDefinitionModel.content_sha256.is_not(None),
        )
    )
    if id_definition is None:
        raise LifecycleError(
            f"{plugin.code} {code} v{version} is not in procedure_definition: "
            f"the catalogue sync has not run"
        )
    rules = await session.execute(
        select(DeadlineRuleModel.code, DeadlineRuleModel.id).where(
            DeadlineRuleModel.id_procedure_definition == id_definition
        )
    )
    return ProcedureContext(
        plugin=plugin,
        definition=entry.definition,
        id_definition=id_definition,
        rule_ids={code: rule_id for code, rule_id in rules.tuples() if code is not None},
        calendar=WorkingCalendar(plugin.calendar()),
    )


# -- the journal ------------------------------------------------------------


async def _journal(
    session: AsyncSession,
    subject: DossierModel | DocumentModel,
    *,
    to_status: int,
    to_step: str | None = None,
    actor: str,
    context: dict[str, Any] | None = None,
    birth: bool = False,
) -> StatusEventModel:
    is_dossier = isinstance(subject, DossierModel)
    from_step = subject.step if isinstance(subject, DossierModel) and not birth else None
    event = StatusEventModel(
        id_community=subject.id_community,
        subject_type=int(SubjectType.DOSSIER if is_dossier else SubjectType.DOCUMENT),
        subject_id=subject.id,
        from_status=None if birth else subject.status,
        to_status=int(to_status),
        from_step=from_step,
        to_step=to_step,
        actor_id=actor,
        context_json=context or {},
    )
    session.add(event)
    await session.flush()
    await session.refresh(subject, attribute_names=["status", "step"] if is_dossier else ["status"])
    return event


# -- deadlines --------------------------------------------------------------


async def _open_deadlines(session: AsyncSession, dossier: DossierModel) -> dict[str, DeadlineModel]:
    rows = await session.scalars(
        select(DeadlineModel).where(
            DeadlineModel.id_community == dossier.id_community,
            DeadlineModel.id_dossier == dossier.id,
            DeadlineModel.status == int(DeadlineStatus.OPEN),
        )
    )
    return {row.deadline_type: row for row in rows}


async def start_rules(
    session: AsyncSession,
    ctx: ProcedureContext,
    dossier: DossierModel,
    event: str,
    *,
    anchor: datetime.date,
    journal_event_id: int | None = None,
    correspondence_id: int | None = None,
) -> list[DeadlineModel]:
    """Open a deadline for each rule ``event`` anchors, counted from ``anchor``."""
    running = await _open_deadlines(session, dossier)
    started = []
    for rule in ctx.definition.rules_started_by(event, dossier.variant):
        if rule.code in running:
            continue
        deadline = DeadlineModel(
            id_community=dossier.id_community,
            id_dossier=dossier.id,
            deadline_type=rule.code,
            due_date=due_date(rule, ctx.calendar, anchor=anchor),
            status=int(DeadlineStatus.OPEN),
            recurring=False,
            derived_from_event_id=journal_event_id,
            derived_from_correspondence_id=correspondence_id,
            id_deadline_rule=ctx.rule_ids.get(rule.code),
            party=int(rule.deadline_party),
            kind=int(rule.kind_of_deadline),
        )
        session.add(deadline)
        started.append(deadline)
    await session.flush()
    return started


async def start_annual_rules(
    session: AsyncSession,
    ctx: ProcedureContext,
    dossier: DossierModel,
    *,
    period_year: int,
    journal_event_id: int | None = None,
) -> list[DeadlineModel]:
    running = await _open_deadlines(session, dossier)
    started = []
    for rule in ctx.definition.annual_rules(dossier.variant):
        if rule.code in running:
            continue
        deadline = DeadlineModel(
            id_community=dossier.id_community,
            id_dossier=dossier.id,
            deadline_type=rule.code,
            due_date=due_date(rule, ctx.calendar, period_year=period_year),
            status=int(DeadlineStatus.OPEN),
            recurring=False,
            derived_from_event_id=journal_event_id,
            id_deadline_rule=ctx.rule_ids.get(rule.code),
            party=int(rule.deadline_party),
            kind=int(rule.kind_of_deadline),
        )
        session.add(deadline)
        started.append(deadline)
    await session.flush()
    return started


async def close_rules(
    session: AsyncSession, ctx: ProcedureContext, dossier: DossierModel, event: str
) -> list[DeadlineModel]:
    """Mark met the open deadlines ``event`` closes."""
    running = await _open_deadlines(session, dossier)
    now = datetime.datetime.now(datetime.UTC)
    met = []
    for rule in ctx.definition.rules_closed_by(event, dossier.variant):
        deadline = running.get(rule.code)
        if deadline is not None:
            deadline.status = int(DeadlineStatus.MET)
            deadline.resolved_at = now
            met.append(deadline)
    await session.flush()
    return met


async def _cancel_running(session: AsyncSession, dossier: DossierModel) -> None:
    now = datetime.datetime.now(datetime.UTC)
    for deadline in (await _open_deadlines(session, dossier)).values():
        deadline.status = int(DeadlineStatus.CANCELLED)
        deadline.resolved_at = now
    await session.flush()


async def refresh_next_action(
    session: AsyncSession, ctx: ProcedureContext, dossier: DossierModel
) -> None:
    """``next_action_code`` from the step, ``next_action_due`` from the
    soonest running deadline: what the dashboard shows without opening it."""
    step = ctx.definition.step(str(dossier.step))
    dossier.next_action_code = str(step.next_action.code)
    dossier.next_action_due = await session.scalar(
        select(func.min(DeadlineModel.due_date)).where(
            DeadlineModel.id_community == dossier.id_community,
            DeadlineModel.id_dossier == dossier.id,
            DeadlineModel.status == int(DeadlineStatus.OPEN),
        )
    )
    await session.flush()


# -- steps ------------------------------------------------------------------


async def open_procedure(
    session: AsyncSession,
    ctx: ProcedureContext,
    *,
    id_community: int,
    id_sharing_operation: int | None,
    region: int,
    variant: str | None,
    opened_by: OpenedBy,
    day: datetime.date,
    period_key: str | None = None,
    actor: str = ENGINE_ACTOR,
    context: dict[str, Any] | None = None,
    step: str | None = None,
) -> DossierModel:
    """A new procedure, born in the journal on the definition's initial step,
    or on ``step`` for one that enters OptimCE further along (already sent
    when the baseline was confirmed): it is never a draft, even for a moment,
    so it cannot collide with the family's open draft."""
    definition = ctx.definition
    if variant is not None and variant not in definition.variants:
        raise LifecycleError(f"{definition.code} has no variant {variant!r}")
    initial = definition.step(step or definition.initial_step)
    dossier = DossierModel(
        id_community=id_community,
        id_sharing_operation=id_sharing_operation,
        # Born with the journal head's values: the INSERT is not a direct
        # write (the guard is on UPDATE), and the deferred check compares them.
        status=int(initial.common_status),
        step=initial.code,
        region=region,
        id_procedure_definition=ctx.id_definition,
        regulator=ctx.plugin.code,
        family=definition.family,
        scope=int(definition.procedure_scope),
        variant=variant,
        opened_by=int(opened_by),
        period_key=period_key,
        metadata_json={},
    )
    session.add(dossier)
    await session.flush()
    event = await _journal(
        session,
        dossier,
        to_status=int(initial.common_status),
        to_step=initial.code,
        actor=actor,
        context=context,
        birth=True,
    )
    await start_rules(
        session, ctx, dossier, step_event(initial.code), anchor=day, journal_event_id=event.id
    )
    if period_key is not None and period_key.isdigit():
        await start_annual_rules(
            session, ctx, dossier, period_year=int(period_key), journal_event_id=event.id
        )
    await refresh_next_action(session, ctx, dossier)
    return dossier


async def move_to_step(
    session: AsyncSession,
    ctx: ProcedureContext,
    dossier: DossierModel,
    to_step: str,
    *,
    day: datetime.date,
    actor: str = ENGINE_ACTOR,
    context: dict[str, Any] | None = None,
) -> StatusEventModel:
    """Take one edge of the catalogue, journaled, with its deadlines."""
    definition = ctx.definition
    if not definition.allows(str(dossier.step), to_step, dossier.variant):
        raise LifecycleError(
            f"{definition.code} ({dossier.variant or 'no variant'}) has no edge "
            f"{dossier.step} -> {to_step}"
        )
    target = definition.step(to_step)
    event = await _journal(
        session,
        dossier,
        to_status=int(target.common_status),
        to_step=to_step,
        actor=actor,
        context=context,
    )
    await close_rules(session, ctx, dossier, step_event(to_step))
    if target.common_status in _ENDS_THE_CLOCKS:
        await _cancel_running(session, dossier)
    else:
        await start_rules(
            session, ctx, dossier, step_event(to_step), anchor=day, journal_event_id=event.id
        )
    await refresh_next_action(session, ctx, dossier)
    return event


def path_to(
    definition: ProcedureDefinition, source: str, status: ProcedureStatus, variant: str | None
) -> list[str]:
    """The shortest run of steps from ``source`` to the first step of common
    status ``status``, along the variant's edges (``source`` excluded). Empty
    when none leads there."""
    steps = {s.code: s for s in definition.steps}
    previous: dict[str, str] = {}
    queue = [source]
    while queue:
        current = queue.pop(0)
        if current != source and steps[current].common_status == status:
            path = [current]
            while previous[path[0]] != source:
                path.insert(0, previous[path[0]])
            return path
        for edge in definition.transitions:
            if (
                edge.source == current
                and edge.target not in previous
                and edge.target != source
                and definition.allows(current, edge.target, variant)
            ):
                previous[edge.target] = current
                queue.append(edge.target)
    return []


async def take_automatic_edge(
    session: AsyncSession,
    ctx: ProcedureContext,
    dossier: DossierModel,
    status: ProcedureStatus,
    *,
    day: datetime.date,
    context: dict[str, Any] | None = None,
) -> bool:
    """Move to a step of common status ``status`` if the catalogue lets the
    engine do it from here. False when it does not."""
    edge = ctx.definition.automatic_edge_to(str(dossier.step), status, dossier.variant)
    if edge is None:
        return False
    await move_to_step(session, ctx, dossier, edge.target, day=day, context=context)
    return True


# -- pieces -----------------------------------------------------------------


async def pieces_of(session: AsyncSession, dossier: DossierModel) -> list[DocumentModel]:
    return list(
        await session.scalars(
            select(DocumentModel)
            .where(
                DocumentModel.id_community == dossier.id_community,
                DocumentModel.id_dossier == dossier.id,
            )
            .order_by(DocumentModel.id)
        )
    )


async def sync_pieces(
    session: AsyncSession,
    dossier: DossierModel,
    requirements: list[DocumentRequirement],
    *,
    actor: str = ENGINE_ACTOR,
) -> list[DocumentModel]:
    """Make the procedure's pieces what the plugin requires. A piece no longer
    required is kept (it may hold versions) and marked not required. Returns
    the pieces created."""
    existing = {(d.requirement_code, d.id_member): d for d in await pieces_of(session, dossier)}
    created = []
    for requirement in requirements:
        condition = dict(requirement.condition) if requirement.condition else None
        piece = existing.pop((requirement.code, requirement.id_member), None)
        if piece is not None:
            if (piece.required, piece.condition_json) != (requirement.required, condition):
                piece.required = requirement.required
                piece.condition_json = condition
            continue
        piece = DocumentModel(
            id_community=dossier.id_community,
            id_dossier=dossier.id,
            doc_type=requirement.code,
            origin=int(
                DocOrigin.GENERATED
                if requirement.kind == DocumentKind.GENERATED
                else DocOrigin.UPLOADED
            ),
            status=int(ProcedureDocumentStatus.MISSING),
            kind=int(requirement.kind),
            required=requirement.required,
            requirement_code=requirement.code,
            condition_json=condition,
            id_member=requirement.id_member,
        )
        session.add(piece)
        await session.flush()
        await _journal(
            session,
            piece,
            to_status=int(ProcedureDocumentStatus.MISSING),
            actor=actor,
            birth=True,
        )
        created.append(piece)
    for leftover in existing.values():
        if leftover.required and leftover.requirement_code is not None:
            leftover.required = False
    await session.flush()
    return created


async def mark_generated_stale(
    session: AsyncSession, dossier: DossierModel, *, actor: str = ENGINE_ACTOR
) -> list[DocumentModel]:
    """A new change outdates every generated piece that was ready."""
    stale = []
    for piece in await pieces_of(session, dossier):
        if (
            piece.kind == int(DocumentKind.GENERATED)
            and piece.status == int(ProcedureDocumentStatus.READY)
            and piece.current_version_id is not None
        ):
            await _journal(
                session,
                piece,
                to_status=int(ProcedureDocumentStatus.STALE),
                actor=actor,
                context={"reason": "new_change"},
            )
            stale.append(piece)
    return stale


async def pieces_complete(session: AsyncSession, dossier: DossierModel) -> bool:
    """Every required piece the community supplies is there. A received piece
    comes after the sending and never holds a draft back."""
    return all(
        piece.status in _PIECE_THERE
        for piece in await pieces_of(session, dossier)
        if piece.required and piece.kind != int(DocumentKind.RECEIVED)
    )


async def settle_readiness(
    session: AsyncSession, ctx: ProcedureContext, dossier: DossierModel, *, day: datetime.date
) -> bool:
    """Draft -> ready when the pieces are complete, ready -> draft when they no
    longer are, by the catalogue's automatic edges. True when it moved."""
    complete = await pieces_complete(session, dossier)
    if dossier.status == int(ProcedureStatus.DRAFT) and complete:
        return await take_automatic_edge(
            session, ctx, dossier, ProcedureStatus.READY, day=day, context={"reason": "complete"}
        )
    if dossier.status == int(ProcedureStatus.READY) and not complete:
        return await take_automatic_edge(
            session,
            ctx,
            dossier,
            ProcedureStatus.DRAFT,
            day=day,
            context={"reason": "incomplete"},
        )
    return False


async def supply_piece(
    session: AsyncSession,
    ctx: ProcedureContext,
    dossier: DossierModel,
    piece: DocumentModel,
    *,
    status: ProcedureDocumentStatus,
    day: datetime.date,
    actor: str = ENGINE_ACTOR,
    context: dict[str, Any] | None = None,
) -> bool:
    """A piece got a version that makes it ``status`` (ready, signed): journal
    it, then let readiness follow. Returns whether the procedure moved."""
    if piece.status != int(status):
        await _journal(session, piece, to_status=int(status), actor=actor, context=context)
    moved = await settle_readiness(session, ctx, dossier, day=day)
    await refresh_next_action(session, ctx, dossier)
    return moved
