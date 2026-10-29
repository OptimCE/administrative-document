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
3. The deadline pass and its notifications, CRM FIRST. A deadline flipped to
   MISSED is never matched again, so a local commit followed by a lost CRM one
   would lose its notification for good; this way a crash in between leaves it
   OPEN and the next run re-sends (``dedupe_key`` collapses the duplicate email).
   Safe because this transaction writes deadlines only, which carry no deferred
   check.
4. The effects due today, logged (phase 3 applies them).

Realtime hints go out after the commits.
"""

from __future__ import annotations

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
from engine.filing import RegionOf
from engine.jobs import Chase, chase_deadlines, due_effects, open_calendar_procedures
from ports.crm_engine import SqlEngineCrmRead
from shared.const import DeadlineParty, DeadlineStatus
from shared.models.local_models import (
    DeadlineModel,
    DossierModel,
    ModuleActivationModel,
    ProcedureDefinitionModel,
)
from worker.context import with_tenant

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
    try:
        for id_community in await _communities_unscoped(local, SqlEngineCrmRead(crm)):
            report.communities += 1
            try:
                with with_tenant(id_community):
                    await _run_for(
                        id_community,
                        local=local,
                        crm=crm,
                        registry=registry,
                        now=now,
                        region_of=region_of,
                        report=report,
                    )
            except Exception:
                logger.exception("daily jobs failed for community %s", id_community)
                await local.rollback()
                await crm.rollback()
                report.failed += 1
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
) -> None:
    today = brussels_today(now)
    crm_read = SqlEngineCrmRead(crm)
    state = await crm_read.module_state(id_community)
    period = await reconcile_activation(local, id_community, state, now=now)
    plugin = plugin_for(registry, state.regulator) if period is not None else None
    if period is None or plugin is None:
        await local.commit()
        await crm.commit()  # its reads
        return

    # 2. Journaled writes, local first.
    await ensure_baseline(local, crm_read, plugin, period, now=now, region_of=region_of)
    opened = await open_calendar_procedures(
        local, plugin, id_community, today=today, region_of=region_of
    )
    await local.commit()
    report.opened += len(opened)

    # 3. Deadlines, CRM first; 4. the effects due, read in the same pass.
    chase = await chase_deadlines(
        local, id_community, today=today, reminder_days=settings.DEADLINE_REMINDER_DAYS
    )
    notify = NotificationService(crm)
    await _notify(notify, local, crm, id_community, chase, today=today)
    effects = await due_effects(local, id_community, today=today)
    # Plain values: nothing after the commits reads ORM state, which a
    # rollback anywhere on these sessions would have expired.
    due = [
        (d.id, d.status == int(DeadlineStatus.MISSED))
        for d in (*chase.missed, *chase.reminded, *chase.late_answers)
    ]
    await crm.commit()
    await local.commit()
    report.missed += len(chase.missed)
    report.reminded += len(chase.reminded)
    report.late_answers += len(chase.late_answers)
    report.expired += chase.expired

    for procedure_id, deadline_id in effects:
        logger.info(
            "effect due: procedure %s, deadline %s (applied in phase 3)",
            procedure_id,
            deadline_id,
        )
    report.effects_due += len(effects)

    await notify.flush_realtime()
    await _hint(id_community, opened, due)


async def _notify(
    notify: NotificationService,
    local: AsyncSession,
    crm: AsyncSession,
    id_community: int,
    chase: Chase,
    *,
    today: datetime.date,
) -> None:
    """Stage the notifications and the audit row on the CRM session."""
    target = CommunityTarget(community_id=id_community, roles=MANAGER_ROLES)
    codes = await _procedure_codes(local, chase)
    for deadline in chase.missed:
        # TRANSACTIONAL: a missed regulatory deadline has legal consequences,
        # not something a manager may opt out of.
        await notify.publish(
            type=NotificationTypes.ADMIN_DEADLINE_MISSED,
            target=target,
            category=NotificationCategory.TRANSACTIONAL,
            channels=(Channel.INAPP, Channel.EMAIL),
            data=_data(deadline, codes),
        )
    for deadline in chase.reminded:
        await notify.publish(
            type=NotificationTypes.ADMIN_DEADLINE_DUE_SOON,
            target=target,
            # INFORMATIONAL: a reminder is what a manager may mute.
            category=NotificationCategory.INFORMATIONAL,
            channels=(Channel.INAPP, Channel.EMAIL),
            data={**_data(deadline, codes), "as_of": today.isoformat()},
            # The occurrence date is in the key: reminding does not change what
            # the derived key hashes, so it could not tell two occurrences apart.
            dedupe_key=(
                f"{NotificationTypes.ADMIN_DEADLINE_DUE_SOON}:"
                f"{deadline.id}:{deadline.due_date.isoformat()}"
            ),
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


async def _hint(id_community: int, opened: list[int], due: list[tuple[int, bool]]) -> None:
    """Tell the managers to refetch. After the commits; never raises."""
    audience = CommunityAudience(id_community, Tier.MANAGER)
    for dossier_id in opened:
        await emit(
            topic="admin.dossier.updated",
            audience=audience,
            resource=("dossier", dossier_id),
            scope_community_id=id_community,
        )
    for deadline_id, missed in due:
        await emit(
            topic="admin.deadline.due",
            audience=audience,
            resource=("deadline", deadline_id),
            hint={"missed": missed},
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
