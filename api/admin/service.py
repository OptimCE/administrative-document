"""The ``/admin`` use cases, and the order their commits happen in.

The engine works in the caller's transactions; this is the caller. The
baseline confirmation is the one place where the order across the two
databases matters (decision Q3), so it is spelt out here:

1. the photo and the baseline procedure, if this is the first look (local);
2. the episodes in the core, audited in the same transaction (CRM);
3. the confirmation: changes, their procedures, the baseline in force (local);
4. the episodes pointed at the procedures that now drive them (CRM).

A crash between two steps leaves the next call to do the rest: step 2 writes
absolute values keyed by (procedure, subject), step 3 runs only while the
period is unconfirmed, step 4 changes only what differs.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from domain.regions import region_for_regulator
from engine.activation import reconcile_activation
from engine.baseline import (
    CHOICES,
    ChoiceIn,
    ItemChange,
    brussels_today,
    driving_refs,
    ensure_baseline,
    episode_writes,
    record_confirmation,
    resolve,
)
from engine.contracts import PluginRegistry, RegulatorPlugin, plugin_for
from engine.filing import RegionOf
from ports.core_effects import CoreEffectsPort
from ports.crm_engine import CommunityModuleState, EngineCrmRead
from shared.models.local_models import ModuleActivationModel


class ModuleInactiveError(Exception):
    """The module is not active for this community."""


class AdminService:
    def __init__(
        self,
        *,
        local_session: AsyncSession,
        crm_session: AsyncSession,
        crm_read: EngineCrmRead,
        effects: CoreEffectsPort,
        registry: PluginRegistry,
        region_of: RegionOf = region_for_regulator,
    ) -> None:
        self._local = local_session
        self._crm = crm_session
        self._crm_read = crm_read
        self._effects = effects
        self._registry = registry
        self._region_of = region_of

    async def _period(
        self, id_community: int, now: datetime.datetime
    ) -> tuple[CommunityModuleState, ModuleActivationModel, RegulatorPlugin | None]:
        state = await self._crm_read.module_state(id_community)
        period = await reconcile_activation(self._local, id_community, state, now=now)
        if period is None:
            await self._local.commit()
            raise ModuleInactiveError(str(id_community))
        return state, period, plugin_for(self._registry, state.regulator)

    async def baseline(self, id_community: int) -> dict[str, Any]:
        now = datetime.datetime.now(datetime.UTC)
        state, period, plugin = await self._period(id_community, now)
        if plugin is None:
            await self._local.commit()
            return {"status": "not_supported", "regulator": state.regulator}
        await ensure_baseline(
            self._local, self._crm_read, plugin, period, now=now, region_of=self._region_of
        )
        await self._local.commit()
        return _view(period)

    async def confirm_baseline(
        self, id_community: int, choices: Sequence[ChoiceIn], *, actor: str
    ) -> dict[str, Any]:
        """Raises ``BaselineError`` (choices that do not fit) and
        ``EffectConflictError`` (the core refused), both before anything is
        confirmed."""
        now = datetime.datetime.now(datetime.UTC)
        state, period, plugin = await self._period(id_community, now)
        if plugin is None:
            await self._local.commit()
            return {"status": "not_supported", "regulator": state.regulator}
        baseline = await ensure_baseline(
            self._local, self._crm_read, plugin, period, now=now, region_of=self._region_of
        )
        await self._local.commit()

        if period.baseline_confirmed_at is None:
            decisions = resolve(baseline, choices)
            writes = episode_writes(baseline, decisions, today=brussels_today(now))
            try:
                await self._effects.apply(
                    id_community,
                    writes,
                    audit_payload={
                        "reason": "baseline",
                        "period": period.id,
                        "id_dossier": baseline["id_dossier"],
                    },
                )
                await self._crm.commit()
            except Exception:
                await self._crm.rollback()
                raise
            await record_confirmation(
                self._local,
                plugin,
                period,
                decisions,
                actor=actor,
                now=now,
                region_of=self._region_of,
            )
            await self._local.commit()

        refs = await driving_refs(self._local, period)
        if refs:
            await self._effects.set_refs(id_community, refs)
            await self._crm.commit()
        return _view(period)


_SUBJECTS = {1: "meter", 2: "key"}


def _view(period: ModuleActivationModel) -> dict[str, Any]:
    baseline = period.baseline_json or {}
    confirmation = baseline.get("confirmation") or {}
    items = []
    for item in baseline.get("items", ()):
        change = ItemChange(item["change"])
        chosen = confirmation.get(item["key"]) or {}
        items.append(
            {
                "key": item["key"],
                "subject_type": _SUBJECTS.get(item["subject_type"], "procedure"),
                "subject_ref": item["subject_ref"],
                "id_sharing_operation": item["id_sharing_operation"],
                "operation_name": item.get("operation_name"),
                "change": str(change),
                "choices": [str(c) for c in CHOICES[change]],
                "default_choice": item["default"],
                "label": item.get("label"),
                "id_member": item.get("id_member"),
                "grd": item.get("grd"),
                "injection_status": item.get("injection_status"),
                "start_date": item.get("start_date"),
                "crm_status": item.get("crm_status"),
                "procedure_code": item.get("procedure_code"),
                "procedure_labels": item.get("procedure_labels"),
                "chosen": chosen.get("choice"),
                "effective_date": chosen.get("effective_date"),
                "sent_date": chosen.get("sent_date"),
            }
        )
    return {
        "status": "confirmed" if period.baseline_confirmed_at else "pending",
        "regulator": baseline.get("regulator"),
        "kind": baseline.get("kind"),
        "taken_at": baseline.get("taken_at"),
        "confirmed_at": period.baseline_confirmed_at,
        "confirmed_by": period.confirmed_by,
        "id_dossier": baseline.get("id_dossier"),
        "items": items,
    }
