"""The scheduled deadline sweep.

`POST /maintenance/deadline-sweep` has existed since the deadline engine landed
and its docstring has said "scheduler-driven" aspirationally ever since —
nothing invoked it. So deadlines never chased anyone, and the reminder that
gives `admin_deadline.due_soon` its value could not fire at all.

This module is that scheduler. It calls the same `AdministrativeDocumentService`
the route calls, so there is exactly one implementation of the sweep.
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.administrative_document.repository import AdministrativeDocumentRepository
from api.administrative_document.service import AdministrativeDocumentService
from core.config import settings
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead

# `ports.providers`, NOT `api.administrative_document.deps`: it is the same
# provider object, but `deps` imports fastapi, which `requirements/worker.txt`
# does not install — so importing it here crash-loops the worker image on startup.
from ports.providers import get_event_publisher
from shared.const import DeadlineStatus
from shared.models.local_models import DeadlineModel
from worker.context import with_tenant

logger = logging.getLogger(__name__)

BRUSSELS = ZoneInfo("Europe/Brussels")

# One advisory lock for the whole sweep, so two worker replicas cannot both run
# it. `dedupe_key` would collapse the duplicate EMAIL, but nothing collapses the
# duplicate in-app notification — every manager would get two bell entries. The
# key is arbitrary but must stay stable; it is namespaced by service.
_SWEEP_ADVISORY_LOCK_KEY = 0x0AD3_0001


async def _communities_with_sweepable_deadlines_unscoped(
    local: AsyncSession, *, as_of: datetime.date, window_days: int
) -> list[int]:
    """Which communities have anything for the sweep to do.

    **Deliberately unscoped**, hence the name. Every other owned-DB read goes
    through `with_community_scope`, which filters on the
    `current_internal_community_id` ContextVar — but a scheduler has no request
    and therefore no tenant yet, and a scoped read here would return nothing at
    all with no error, which looks exactly like "no work to do". Mirrors the
    `_find_render_unscoped` precedent in the service.
    """
    stmt = (
        select(DeadlineModel.id_community)
        .where(
            DeadlineModel.status == int(DeadlineStatus.OPEN),
            DeadlineModel.due_date <= as_of + datetime.timedelta(days=window_days),
        )
        .distinct()
    )
    return list((await local.execute(stmt)).scalars().all())


async def sweep_deadlines_for_every_community(
    *,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
    today: datetime.date | None = None,
) -> tuple[int, int]:
    """Run the deadline sweep for every community that has work pending.

    Sessions are injectable, mirroring `worker/generate.py` and billing's
    `process_billing_run`, so a test can drive this on a rolled-back session
    without a container. Returns (missed, reminded-communities).
    """
    own_local = local_session is None
    own_crm = crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    as_of = today or datetime.datetime.now(BRUSSELS).date()
    try:
        community_ids = await _communities_with_sweepable_deadlines_unscoped(
            local, as_of=as_of, window_days=settings.DEADLINE_REMINDER_DAYS
        )
        if not community_ids:
            return 0, 0

        service = AdministrativeDocumentService(
            local_session=local,
            crm_session=crm,
            repository=AdministrativeDocumentRepository(local),
            crm_read=SqlAlchemyCrmCoreRead(crm),
            publisher=get_event_publisher(),
        )
        missed_total = 0
        swept = 0
        for id_community in community_ids:
            # Without this the sweep reads through `with_community_scope` with an
            # unset ContextVar and silently matches nothing.
            with with_tenant(id_community):
                missed, _rolled = await service.sweep_deadlines(today=as_of)
            missed_total += missed
            swept += 1
        logger.info(
            "deadline sweep: %s communit(ies), %s missed",
            swept,
            missed_total,
            extra={"operation": "worker:deadline_sweep"},
        )
        return missed_total, swept
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()


async def try_sweep_deadlines() -> bool:
    """Take the advisory lock and sweep. Returns False if another replica has it.

    `pg_try_advisory_lock` is session-scoped, so the lock is held for exactly as
    long as this connection lives and is released even if the process dies —
    which is the property a cron-style lock needs and a lock table does not have.
    """
    from sqlalchemy import func

    async with AsyncSessionLocalFactory() as local:
        acquired = await local.scalar(select(func.pg_try_advisory_lock(_SWEEP_ADVISORY_LOCK_KEY)))
        if not acquired:
            logger.info("deadline sweep skipped: another replica holds the lock")
            return False
        try:
            async with AsyncSessionCRMFactory() as crm:
                await sweep_deadlines_for_every_community(local_session=local, crm_session=crm)
            return True
        finally:
            await local.execute(select(func.pg_advisory_unlock(_SWEEP_ADVISORY_LOCK_KEY)))
