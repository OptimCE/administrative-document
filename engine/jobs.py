"""The engine's daily jobs (spec, "Deux jobs quotidiens"), for one community.

``worker/engine_jobs.py`` runs them every morning for each community where the
module is active, and owns the commits and the notifications; everything here
works in the caller's transaction.

1. **Calendar procedures** (``open_calendar_procedures``). For each annual type
   in force, this year's procedure opens between its opening day
   (``opens_annually``) and its annual due date, once per community, family and
   year, whatever became of it. Outside that window nothing opens: a community
   activated after the due date files that year by hand. A converted July
   deadline the new procedure takes over (unlinked, same rule code, party and
   kind) is cancelled.
2. **Deadlines** (``chase_deadlines``). A community's deadline past due becomes
   MISSED, once, and is notified; it stays unresolved (``lifecycle``). One
   coming is reminded at each rung of the ladder it reaches (J-14, J-7, J-2),
   each rung once (``reminders_sent``). A third
   party's deadline past due stays OPEN (the answer is still awaited) and the
   community is told once, ``reminded_at`` marking it. An effect date is not a
   deadline to miss: it is the effect job's. A finished procedure's deadline
   (the informative appeal window a refusal opens) is neither reminded nor
   missed: once past, it closes as CANCELLED, silently. MISSED would keep it
   overdue on the agenda with nothing left to do.
3. **Effects** (``due_effects``). Phase 3 writes the core's effects of a
   procedure whose effect date has come, then moves it in force. Phase 1 only
   lists them: moving one in force without its effects would leave the core
   behind with nothing to catch it up.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.catalogue import ProcedureDefinition
from engine.contracts import RegulatorPlugin
from engine.filing import RegionOf, context_of_procedure, recompute
from engine.lifecycle import (
    LifecycleError,
    context_for,
    open_procedure,
    refresh_next_action,
)
from shared.const import (
    DeadlineKind,
    DeadlineParty,
    DeadlineStatus,
    OpenedBy,
    ProcedureStatus,
)
from shared.models.local_models import DeadlineModel, DossierModel

logger = logging.getLogger(__name__)

FINISHED = (
    int(ProcedureStatus.IN_FORCE),
    int(ProcedureStatus.REFUSED),
    int(ProcedureStatus.CANCELLED),
    int(ProcedureStatus.LAPSED),
)


# -- 1. calendar procedures ---------------------------------------------------


async def open_calendar_procedures(
    local: AsyncSession,
    plugin: RegulatorPlugin,
    id_community: int,
    *,
    today: datetime.date,
    region_of: RegionOf,
) -> list[int]:
    """Open this year's calendar procedures that are due to open. Returns their ids."""
    opened = []
    for entry in plugin.catalogue().entries:
        definition = entry.definition
        if (
            definition.purpose != "filing"
            or definition.opens_annually is None
            or not entry.in_force_on(today)
        ):
            continue
        if definition.scope != "community":
            logger.warning(
                "%s %s: a yearly procedure per operation is not supported",
                plugin.code,
                definition.code,
            )
            continue
        opens = definition.opens_annually.in_year(today.year)
        due = [r.annual.in_year(today.year) for r in definition.annual_rules(None) if r.annual]
        closes = max(due) if due else datetime.date(today.year, 12, 31)
        if not opens <= today <= closes:
            continue
        period_key = str(today.year)
        exists = await local.scalar(
            select(DossierModel.id).where(
                DossierModel.id_community == id_community,
                DossierModel.family == definition.family,
                DossierModel.period_key == period_key,
            )
        )
        if exists is not None:
            continue
        region = region_of(plugin.code)
        if region is None:
            raise LifecycleError(f"regulator {plugin.code} has no region in regulators.json")
        ctx = await context_for(local, plugin, definition.code, definition.version)
        dossier = await open_procedure(
            local,
            ctx,
            id_community=id_community,
            id_sharing_operation=None,
            region=int(region),
            variant=plugin.variant(definition, []),
            opened_by=OpenedBy.SYSTEM,
            day=today,
            period_key=period_key,
            context={"reason": "calendar", "period": period_key},
        )
        await recompute(local, ctx, dossier)
        await _take_over_converted(local, plugin, dossier, ctx.definition)
        opened.append(dossier.id)
    return opened


async def _take_over_converted(
    local: AsyncSession,
    plugin: RegulatorPlugin,
    opened: DossierModel,
    definition: ProcedureDefinition,
) -> int:
    """Cancel the converted July deadlines the new procedure takes over: open,
    on a converted dossier, named like one of its annual rules with the same
    party and kind, and an orphan there. Migration 0003 renamed July types to
    rule codes and the sync linked each to its own procedure's rule; one left
    unlinked has none (a July annual report hung on the notification). The
    own-rule check is the same fact read from the catalogue, in case the link
    is missing: a deadline its procedure has a rule for is never taken."""
    cancelled = 0
    touched: dict[int, DossierModel] = {}
    own_rules: dict[int, set[str]] = {}
    for rule in definition.annual_rules(opened.variant):
        rows = await local.execute(
            select(DeadlineModel, DossierModel)
            .join(DossierModel, DossierModel.id == DeadlineModel.id_dossier)
            .where(
                DeadlineModel.id_community == opened.id_community,
                DeadlineModel.id_deadline_rule.is_(None),
                DeadlineModel.deadline_type == rule.code,
                DeadlineModel.party == int(rule.deadline_party),
                DeadlineModel.kind == int(rule.kind_of_deadline),
                DeadlineModel.status.in_((int(DeadlineStatus.OPEN), int(DeadlineStatus.MISSED))),
                DossierModel.opened_by == int(OpenedBy.MIGRATION),
            )
        )
        for deadline, dossier in rows.tuples():
            if dossier.id not in own_rules:
                ctx = await context_of_procedure(local, plugin, dossier)
                own_rules[dossier.id] = {r.code for r in ctx.definition.deadline_rules}
            if deadline.deadline_type in own_rules[dossier.id]:
                continue
            deadline.status = int(DeadlineStatus.CANCELLED)
            deadline.resolved_at = datetime.datetime.now(datetime.UTC)
            touched[dossier.id] = dossier
            cancelled += 1
    await local.flush()
    for dossier in touched.values():
        ctx = await context_of_procedure(local, plugin, dossier)
        await refresh_next_action(local, ctx, dossier)
    if cancelled:
        logger.info("procedure %s took over %d converted July deadline(s)", opened.id, cancelled)
    return cancelled


# -- 2. deadlines -------------------------------------------------------------


@dataclass
class Chase:
    """What the deadline pass changed, for the caller to notify."""

    missed: list[DeadlineModel] = field(default_factory=list)  # the community's, now MISSED
    reminded: list[DeadlineModel] = field(default_factory=list)  # due soon: a rung reached
    late_answers: list[DeadlineModel] = field(default_factory=list)  # a third party's, past due
    expired: int = 0  # of a finished procedure, silently
    # deadline id -> the rung its reminder is for (days before the due date)
    rungs: dict[int, int] = field(default_factory=dict)


def reached_rung(rungs: Sequence[int], days_left: int) -> tuple[int, list[int]] | None:
    """The tightest rung ``days_left`` has reached, and every rung reached:
    a deadline born 5 days out is told once, at the J-7 rung, and J-14 is
    marked told with it. None before the widest rung."""
    reached = sorted((r for r in rungs if days_left <= r), reverse=True)
    return (reached[-1], reached) if reached else None


async def chase_deadlines(
    local: AsyncSession, id_community: int, *, today: datetime.date, rungs: Sequence[int]
) -> Chase:
    """Flip and mark what the day changes. The marks are the idempotency: a
    second run the same day finds nothing (MISSED is no longer OPEN, a rung
    told is in ``reminders_sent``, a late answer has ``reminded_at``).

    The reminder ladder (spec, Notifications: J-14, J-7, J-2, then overdue):
    each rung of a deadline of the community is told once."""
    chase = Chase()
    now = datetime.datetime.now(datetime.UTC)
    window = max(rungs, default=0)
    rows = await local.execute(
        select(DeadlineModel, DossierModel)
        .join(DossierModel, DossierModel.id == DeadlineModel.id_dossier)
        .where(
            DeadlineModel.id_community == id_community,
            DeadlineModel.status == int(DeadlineStatus.OPEN),
            DeadlineModel.due_date <= today + datetime.timedelta(days=window),
            DossierModel.id_procedure_definition.is_not(None),
        )
        .order_by(DeadlineModel.due_date, DeadlineModel.id)
    )
    for deadline, dossier in rows.tuples():
        if deadline.kind == int(DeadlineKind.EFFECT):
            continue  # the effect job's
        past_due = deadline.due_date < today
        if dossier.status in FINISHED:
            if past_due:
                deadline.status = int(DeadlineStatus.CANCELLED)
                deadline.resolved_at = now
                chase.expired += 1
            continue
        ours = deadline.party in (None, int(DeadlineParty.COMMUNITY))
        if ours and past_due:
            deadline.status = int(DeadlineStatus.MISSED)
            deadline.resolved_at = now
            chase.missed.append(deadline)
        elif ours:
            rung = reached_rung(rungs, (deadline.due_date - today).days)
            told = set(deadline.reminders_sent or ())
            if rung is None or rung[0] in told:
                continue
            deadline.reminders_sent = sorted(told | set(rung[1]), reverse=True)
            if deadline.reminded_at is None:
                deadline.reminded_at = now
            chase.reminded.append(deadline)
            chase.rungs[deadline.id] = rung[0]
        elif not ours and past_due and deadline.reminded_at is None:
            deadline.reminded_at = now
            chase.late_answers.append(deadline)
    await local.flush()
    return chase


# -- 3. effects ---------------------------------------------------------------


async def due_effects(
    local: AsyncSession, id_community: int, *, today: datetime.date
) -> list[tuple[int, int]]:
    """(procedure id, deadline id) of every effect date that has come. Phase 3
    applies them (core effects, then in force); phase 1 lists them."""
    rows = await local.execute(
        select(DeadlineModel.id_dossier, DeadlineModel.id)
        .join(DossierModel, DossierModel.id == DeadlineModel.id_dossier)
        .where(
            DeadlineModel.id_community == id_community,
            DeadlineModel.status == int(DeadlineStatus.OPEN),
            DeadlineModel.kind == int(DeadlineKind.EFFECT),
            DeadlineModel.due_date <= today,
            DossierModel.status.not_in(FINISHED),
        )
        .order_by(DeadlineModel.due_date)
    )
    return [(int(d), int(i)) for d, i in rows.tuples()]
