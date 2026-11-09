"""The module's activation periods, reconciled from the CRM.

The activation itself is the CRM's ``community_subscription``, shared by every
annexe and without history. ``module_activation`` dates the periods this
module saw: one is opened when the subscription is first seen active, closed
when it is first seen inactive. Reconciled lazily, wherever the state is read
(each event, the status and baseline routes, the daily job), so nothing in
the core has to tell the annexe.

The period starts at the subscription's ``updated_at``, the moment it was
switched on, not the moment the annexe noticed: events in flight at that
moment are filed, and the baseline comparison absorbs the ones it already
covers (spec, "Activation du module").
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ports.crm_engine import CommunityModuleState
from shared.models.local_models import ModuleActivationModel

# Serialises reconciliation per community, transaction-scoped: the worker and
# the API may both notice an activation at once, and only one may open its
# period and photograph its baseline. Namespaced like worker/sweeps.py's key.
_ACTIVATION_LOCK = 0x0AD3_0003


async def current_period(session: AsyncSession, id_community: int) -> ModuleActivationModel | None:
    period: ModuleActivationModel | None = await session.scalar(
        select(ModuleActivationModel).where(
            ModuleActivationModel.id_community == id_community,
            ModuleActivationModel.disabled_at.is_(None),
        )
    )
    return period


async def reconcile_activation(
    session: AsyncSession,
    id_community: int,
    state: CommunityModuleState,
    *,
    now: datetime.datetime | None = None,
) -> ModuleActivationModel | None:
    """Bring ``module_activation`` in line with ``state``; return the open
    period, or None when the module is inactive."""
    await session.execute(select(func.pg_advisory_xact_lock(_ACTIVATION_LOCK, id_community)))
    now = now or datetime.datetime.now(datetime.UTC)
    changed_at = min(state.changed_at, now) if state.changed_at is not None else now
    period = await current_period(session, id_community)
    if state.exists and state.active:
        if period is None:
            period = ModuleActivationModel(id_community=id_community, enabled_at=changed_at)
            session.add(period)
            await session.flush()
        return period
    if period is not None:
        period.disabled_at = max(period.enabled_at, changed_at)
        await session.flush()
    return None
