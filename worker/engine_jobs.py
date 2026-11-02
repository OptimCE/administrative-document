"""The daily jobs, for every community where the module is active.

``engine/jobs.py`` holds what each job decides; this runs it community by
community and owns what the engine does not: the commits, the notifications and
the lock. It replaces the July sweep (``worker/sweeps.py``), which ignored the
subscription and whose code goes with the cleanup lot.

Per community, in its own transactions, so one community's failure costs only
that community's day (the next run retries; every step is idempotent):

1. Activation is read from the CRM, never cached, and the local period
   reconciled. Module off: nothing else, no notification (spec, désactivation).
   A regulator without a plugin: nothing either.
2. The baseline, if the period has none yet (the first run after an activation
   nobody has looked at), and this year's calendar procedures. Committed
   LOCALLY FIRST: these rows are journaled, and a deferred journal check failing
   at a local commit must not leave notifications behind in the CRM.
3. The deadline pass and its in-app notifications (one per rung of the
   reminder ladder, J-14, J-7, J-2, and one when missed), CRM FIRST. A deadline
   flipped to MISSED is never matched again, so a local commit followed by a
   lost CRM one would lose its notification for good; this way a crash in
   between leaves it OPEN and the next run re-sends. Safe because this
   transaction writes deadlines only, which carry no deferred check.
4. The effects due: each procedure whose effect date has come goes in force,
   the core first (``engine.effects.go_in_force``). One the core refuses (an
   EAN in another operation) stays where it is with the reason, and is tried
   again the next day; one the catalogue cannot put in force is logged.
5. The reconciliation: every procedure whose subjects may still move has its
   badges re-planned (``engine.effects.sync``): what a failed sync, or a crash
   between two commits, left behind is caught up here. Planning writes only
   what differs, so a quiet day writes nothing.

Then, once every community is done, the summary email (``worker/digest.py``):
at most one per manager per day, across communities (criterion 4.1). Its news
is what steps 3 and 4 told (the rungs, the deadlines missed, the third parties
late, the procedures put in force) and the day's answers, which it reads
itself: most are recorded through the API.

Realtime hints go out after the commits.

``python -m worker.engine_jobs`` runs the day once, now, under the same lock:
a catch-up after an outage, or a test that cannot wait for 06:00.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.audit_log.actions import AuditActions
from core.audit_log.dtos import AuditLogInput
from core.audit_log.service import AuditLogService
from core.config import settings
from core.database.database import (
    AsyncSessionCRMFactory,
    AsyncSessionLocalFactory,
    local_engine,
)
from core.notifications import (
    MANAGER_ROLES,
    Channel,
    CommunityTarget,
    NotificationCategory,
    NotificationService,
    NotificationTypes,
)
from core.realtime import CommunityAudience, Tier, emit
from domain.regions import region_for_regulator
from engine.activation import reconcile_activation
from engine.baseline import brussels_today, ensure_baseline
from engine.contracts import PluginRegistry, plugin_for
from engine.effects import InForceError, go_in_force, sync, tracked_dossiers
from engine.filing import RegionOf
from engine.jobs import Chase, chase_deadlines, due_effects, open_calendar_procedures
from ports.core_effects_sqlalchemy import SqlCoreEffects
from ports.crm_engine import SqlEngineCrmRead
from shared.const import DeadlineParty
from shared.models.local_models import (
    DeadlineModel,
    DossierModel,
    ModuleActivationModel,
    ProcedureDefinitionModel,
)
from shared.procedure_notifications import effect_applied
from worker.context import with_tenant
from worker.digest import CommunityDay, News, send_digests

logger = logging.getLogger(__name__)

# Distinct from the July sweep's 0x0AD3_0001, which still guards the
# maintenance route until the cleanup lot.
_JOBS_ADVISORY_LOCK_KEY = 0x0AD3_0004


@dataclass
class DailyReport:
    communities: int = 0
    opened: int = 0
    missed: int = 0
    reminded: int = 0
    late_answers: int = 0
    expired: int = 0
    effects_due: int = 0
    effects_applied: int = 0
    effects_blocked: int = 0
    reconciled: int = 0
    digests: int = 0
    failed: int = 0


async def _communities_unscoped(local: AsyncSession, crm: SqlEngineCrmRead) -> list[int]:
    """Every community the module is active for, plus those with a period still
    open here: their subscription was switched off since, and reconciling is
    what closes the period. Unscoped on purpose: a job has no tenant yet."""
    active = await crm.communities_with_module()
    still_open = await local.scalars(
        select(ModuleActivationModel.id_community)
        .where(ModuleActivationModel.disabled_at.is_(None))
        .distinct()
    )
    return sorted(set(active) | set(still_open))


async def run_daily_jobs(
    *,
    registry: PluginRegistry,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
    now: datetime.datetime | None = None,
    region_of: RegionOf = region_for_regulator,
) -> DailyReport:
    """Run the day for every community. Sessions are injectable, so a test
    drives this on its own sessions."""
    own_local = local_session is None
    own_crm = crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    now = now or datetime.datetime.now(datetime.UTC)
    report = DailyReport()
    days: list[CommunityDay] = []
    try:
        for id_community in await _communities_unscoped(local, SqlEngineCrmRead(crm)):
            report.communities += 1
            try:
                with with_tenant(id_community):
                    day = await _run_for(
                        id_community,
                        local=local,
                        crm=crm,
                        registry=registry,
                        now=now,
                        region_of=region_of,
                        report=report,
                    )
                if day is not None:
                    days.append(day)
            except Exception:
                logger.exception("daily jobs failed for community %s", id_community)
                await local.rollback()
                await crm.rollback()
                report.failed += 1
        report.digests = await _digests(local, crm, days, now=now)
        logger.info(
            "daily jobs: %s",
            report,
            extra={"operation": "worker:daily_jobs"},
        )
        return report
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()


async def _run_for(
    id_community: int,
    *,
    local: AsyncSession,
    crm: AsyncSession,
    registry: PluginRegistry,
    now: datetime.datetime,
    region_of: RegionOf,
    report: DailyReport,
) -> CommunityDay | None:
    """The community's day; what it told, for the summary email (None: the
    module is off, or the regulator has no plugin)."""
    today = brussels_today(now)
    crm_read = SqlEngineCrmRead(crm)
    state = await crm_read.module_state(id_community)
    period = await reconcile_activation(local, id_community, state, now=now)
    plugin = plugin_for(registry, state.regulator) if period is not None else None
    if period is None or plugin is None:
        await local.commit()
        await crm.commit()  # its reads
        return None

    # 2. Journaled writes, local first.
    await ensure_baseline(local, crm_read, plugin, period, now=now, region_of=region_of)
    opened = await open_calendar_procedures(
        local, plugin, id_community, today=today, region_of=region_of
    )
    await local.commit()
    report.opened += len(opened)

    # 3. Deadlines, CRM first; 4. the effects due, read in the same pass.
    chase = await chase_deadlines(
        local, id_community, today=today, rungs=settings.DEADLINE_REMINDER_RUNGS
    )
    notify = NotificationService(crm)
    await _notify(notify, local, crm, id_community, chase, today=today)
    effects = await due_effects(local, id_community, today=today)
    # Plain values: nothing after the commits reads ORM state, which a
    # rollback anywhere on these sessions would have expired.
    due = [d.id for d in (*chase.missed, *chase.reminded, *chase.late_answers)]
    day = CommunityDay(
        id_community=id_community,
        plugin=plugin,
        news=[
            *(News("missed", deadline_id=d.id) for d in chase.missed),
            *(
                News("reminder", deadline_id=d.id, rung=chase.rungs.get(d.id))
                for d in chase.reminded
            ),
            *(News("late", deadline_id=d.id) for d in chase.late_answers),
        ],
    )
    await crm.commit()
    await local.commit()
    report.missed += len(chase.missed)
    report.reminded += len(chase.reminded)
    report.late_answers += len(chase.late_answers)
    report.expired += chase.expired

    report.effects_due += len(effects)

    # 4. The effects due, core first, one procedure at a time.
    effect_dates = await _effect_dates(local, effects)
    applied = []
    for procedure_id, effective in effect_dates:
        try:
            refused = await go_in_force(
                local,
                crm,
                crm_read,
                SqlCoreEffects(crm),
                registry,
                procedure_id,
                effective_date=effective,
                now=now,
                context={"reason": "effect_date"},
            )
        except InForceError as exc:
            await local.rollback()
            logger.warning("effect due but not applicable: %s", exc)
            continue
        except Exception:
            logger.exception("effect of procedure %s not applied", procedure_id)
            await local.rollback()
            await crm.rollback()
            report.effects_blocked += 1
            continue
        if refused:
            logger.warning("effect of procedure %s refused by the core: %s", procedure_id, refused)
            report.effects_blocked += 1
        else:
            report.effects_applied += 1
            applied.append(procedure_id)
            day.news.append(News("in_force", dossier_id=procedure_id, effective_date=effective))
            await _told_in_force(local, crm, procedure_id, effective)

    # 5. The reconciliation of the badges, procedure by procedure.
    for procedure_id in await tracked_dossiers(local, id_community):
        if await sync(
            local,
            crm,
            crm_read,
            SqlCoreEffects(crm),
            registry,
            id_community,
            [procedure_id],
            now=now,
        ):
            report.reconciled += 1

    await notify.flush_realtime()
    await _hint(id_community, [*opened, *applied], due)
    return day


async def _told_in_force(
    local: AsyncSession, crm: AsyncSession, procedure_id: int, effective: datetime.date
) -> None:
    """In-app: the managers, and the members whose meters it made active.
    After the procedure's own commits; a failure costs the notice only."""
    notify = NotificationService(crm)
    try:
        await effect_applied(notify, local, crm, procedure_id, effective_date=effective)
        await crm.commit()
    except Exception:
        logger.exception("in-force notice of procedure %s not sent", procedure_id)
        await crm.rollback()
        return
    await notify.flush_realtime()


async def _digests(
    local: AsyncSession, crm: AsyncSession, days: list[CommunityDay], *, now: datetime.datetime
) -> int:
    """The summary emails, once every community is done. A failure costs the
    emails of the day only (the in-app notices are already out)."""
    notify = NotificationService(crm)
    try:
        sent = await send_digests(local, crm, notify, days, now=now)
        await crm.commit()
        await local.commit()  # its reads
    except Exception:
        logger.exception("daily summaries not sent")
        await crm.rollback()
        await local.rollback()
        return 0
    await notify.flush_realtime()
    return sent


async def _notify(
    notify: NotificationService,
    local: AsyncSession,
    crm: AsyncSession,
    id_community: int,
    chase: Chase,
    *,
    today: datetime.date,
) -> None:
    """Stage the in-app notifications and the audit row on the CRM session.
    In-app only: the email of the day is the summary (``worker/digest.py``),
    at most one per manager whatever the number of deadlines."""
    target = CommunityTarget(community_id=id_community, roles=MANAGER_ROLES)
    codes = await _procedure_codes(local, chase)
    for deadline in chase.missed:
        # TRANSACTIONAL: a missed regulatory deadline has legal consequences,
        # not something a manager may mute in-app.
        await notify.publish(
            type=NotificationTypes.ADMIN_DEADLINE_MISSED,
            target=target,
            category=NotificationCategory.TRANSACTIONAL,
            channels=(Channel.INAPP,),
            data=_data(deadline, codes),
        )
    for deadline in chase.reminded:
        await notify.publish(
            type=NotificationTypes.ADMIN_DEADLINE_DUE_SOON,
            target=target,
            # INFORMATIONAL: a reminder is what a manager may mute.
            category=NotificationCategory.INFORMATIONAL,
            channels=(Channel.INAPP,),
            data={
                **_data(deadline, codes),
                "as_of": today.isoformat(),
                "rung": chase.rungs.get(deadline.id),
                "days_left": (deadline.due_date - today).days,
            },
        )
    for deadline in chase.late_answers:
        # A third party is late: in-app only, for the manager to chase them
        # (spec, "Délai de réponse d'un tiers dépassé").
        await notify.publish(
            type=NotificationTypes.ADMIN_DEADLINE_MISSED,
            target=target,
            category=NotificationCategory.INFORMATIONAL,
            channels=(Channel.INAPP,),
            data=_data(deadline, codes),
        )
    if not (chase.missed or chase.reminded or chase.late_answers):
        return
    await AuditLogService(crm).log(
        AuditLogInput(
            action=(
                AuditActions.DEADLINE_MISSED if chase.missed else AuditActions.DEADLINE_REMINDED
            ),
            entity_type="deadline",
            entity_id=None,
            payload={
                "missed": len(chase.missed),
                "reminded": len(chase.reminded),
                "late_answers": len(chase.late_answers),
                "as_of": today.isoformat(),
            },
        )
    )


async def _effect_dates(
    local: AsyncSession, effects: list[tuple[int, int]]
) -> list[tuple[int, datetime.date]]:
    """(procedure id, the date it takes effect): the procedure's effect date,
    or its effect deadline's (a procedure from before phase 3 has no date)."""
    if not effects:
        return []
    rows = await local.execute(
        select(DeadlineModel.id_dossier, DossierModel.effective_date, DeadlineModel.due_date)
        .join(DossierModel, DossierModel.id == DeadlineModel.id_dossier)
        .where(DeadlineModel.id.in_([deadline for _procedure, deadline in effects]))
    )
    dates = {int(d): effective or due for d, effective, due in rows.tuples()}
    return [(procedure, dates[procedure]) for procedure, _d in effects if procedure in dates]


async def _procedure_codes(local: AsyncSession, chase: Chase) -> dict[int, str | None]:
    ids = {d.id_dossier for d in (*chase.missed, *chase.reminded, *chase.late_answers)}
    if not ids:
        return {}
    rows = await local.execute(
        select(DossierModel.id, ProcedureDefinitionModel.code)
        .join(
            ProcedureDefinitionModel,
            ProcedureDefinitionModel.id == DossierModel.id_procedure_definition,
        )
        .where(DossierModel.id.in_(ids))
    )
    return {int(i): code for i, code in rows.tuples()}


def _data(deadline: DeadlineModel, codes: dict[int, str | None]) -> dict[str, object]:
    return {
        "deadline_id": deadline.id,
        "dossier_id": deadline.id_dossier,
        "procedure_code": codes.get(deadline.id_dossier),
        "deadline_type": deadline.deadline_type,
        "party": DeadlineParty(deadline.party).name.lower() if deadline.party else "community",
        "kind": deadline.kind,
        "due_date": deadline.due_date.isoformat(),
    }


async def _hint(id_community: int, opened: list[int], due: list[int]) -> None:
    """Tell the managers to refetch. After the commits; never raises."""
    audience = CommunityAudience(id_community, Tier.MANAGER)
    for dossier_id in opened:
        await emit(
            topic="admin.dossier.updated",
            audience=audience,
            resource=("dossier", dossier_id),
            scope_community_id=id_community,
        )
    # No hint: a client branches on ``hint.status`` only, and its closed set
    # has no "missed"; the refetch says which it is.
    for deadline_id in due:
        await emit(
            topic="admin.deadline.due",
            audience=audience,
            resource=("deadline", deadline_id),
            scope_community_id=id_community,
        )


async def try_run_daily_jobs(
    registry: PluginRegistry, *, engine: AsyncEngine | None = None
) -> bool:
    """Take the lock and run the day. Returns False if another replica has it.

    ``pg_try_advisory_lock`` is session-scoped: held while this connection
    lives, released even if the process dies. The connection is held apart
    from the jobs' sessions, which commit many times: a session hands its
    connection back to the pool at each commit, and the unlock would then run
    on another connection while the lock stayed with the pooled one."""
    async with (engine or local_engine).connect() as lock:
        acquired = await lock.scalar(select(func.pg_try_advisory_lock(_JOBS_ADVISORY_LOCK_KEY)))
        await lock.commit()  # not idle in a transaction for the whole run
        if not acquired:
            logger.info("daily jobs skipped: another replica holds the lock")
            return False
        try:
            await run_daily_jobs(registry=registry)
            return True
        finally:
            await lock.execute(select(func.pg_advisory_unlock(_JOBS_ADVISORY_LOCK_KEY)))
            await lock.commit()


def main() -> int:
    """``python -m worker.engine_jobs``: the day, once, now. Exit 1 when
    another replica holds the lock."""
    from core.logging import configure_logging
    from regulators.registry import REGISTRY

    configure_logging()
    return 0 if asyncio.run(try_run_daily_jobs(REGISTRY)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
