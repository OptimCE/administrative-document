"""The baseline: what the GRD and the regulator already know when tracking starts.

Spec, "Première activation : l'état de départ" and "Réactivation". The events
before the module was switched on were never filed, so the annexe does not
know the authorised state. When a period opens for a community whose
regulator has a plugin, ``ensure_baseline`` photographs its operations,
current meters and approved keys, and opens the plugin's baseline procedure
(``purpose: baseline``): the dashboard's one task until it is confirmed.

- **First activation**: every meter and key is listed, ``present``. For each the
  admin says ``active`` (the default, with its effective date if known),
  ``not_notified`` (a change in a draft) or ``in_progress`` (already sent,
  with its sending date: a procedure in step "sent").
- **Reactivation**: only the difference with what is still known (the core's
  episodes, the changes already filed) is listed: ``added`` (the same three
  choices, ``not_notified`` by default) or ``removed`` (``ended`` when the GRD
  already took it out, else ``not_notified`` or ``in_progress``).
- Nothing to list: confirmed at once, no task.

Confirmation spans two databases, in the order of decision Q3: the core's
episodes first (``episode_writes``, applied by ``ports.core_effects``), then
the local record (``record_confirmation``), then the episodes'
``regulatory_ref`` (``driving_refs``). Each is idempotent, so a replay after
a crash between two of them completes the rest. The caller commits each.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.contracts import RegulatorPlugin
from engine.events import BRUSSELS, DetectedChange
from engine.filing import RegionOf, file_change, recompute
from engine.lifecycle import (
    ENGINE_ACTOR,
    LifecycleError,
    context_for,
    open_procedure,
    path_to,
    take_automatic_edge,
)
from ports.core_effects import EpisodeWrite
from ports.crm_engine import TRACKED_STATUSES, CommunityPhoto, EngineCrmRead, Episode
from shared.const import (
    ChangeStatus,
    ChangeSubjectType,
    OpenedBy,
    ProcedureStatus,
    RegulatoryStatus,
)
from shared.models.local_models import ChangeModel, DossierModel, ModuleActivationModel

_METER = int(ChangeSubjectType.METER)
_KEY = int(ChangeSubjectType.KEY)
_ADD_KINDS = ("meter_added", "installation_added", "key_changed")


class ItemChange(StrEnum):
    PRESENT = "present"  # first activation: there now
    ADDED = "added"  # reactivation: appeared while the module was off
    REMOVED = "removed"  # reactivation: gone while the module was off


class Choice(StrEnum):
    ACTIVE = "active"
    NOT_NOTIFIED = "not_notified"
    IN_PROGRESS = "in_progress"
    ENDED = "ended"


CHOICES: dict[ItemChange, tuple[Choice, ...]] = {
    ItemChange.PRESENT: (Choice.ACTIVE, Choice.NOT_NOTIFIED, Choice.IN_PROGRESS),
    ItemChange.ADDED: (Choice.ACTIVE, Choice.NOT_NOTIFIED, Choice.IN_PROGRESS),
    ItemChange.REMOVED: (Choice.ENDED, Choice.NOT_NOTIFIED, Choice.IN_PROGRESS),
}
DEFAULT: dict[ItemChange, Choice] = {
    ItemChange.PRESENT: Choice.ACTIVE,
    # What changed while the module was off was notified by nobody until the
    # admin says otherwise: filing it is the choice that cannot under-notify.
    ItemChange.ADDED: Choice.NOT_NOTIFIED,
    ItemChange.REMOVED: Choice.NOT_NOTIFIED,
}

# The effect each choice writes in the core.
_STATUS: dict[tuple[ItemChange, Choice], RegulatoryStatus] = {
    (ItemChange.PRESENT, Choice.ACTIVE): RegulatoryStatus.ACTIVE,
    (ItemChange.PRESENT, Choice.NOT_NOTIFIED): RegulatoryStatus.TO_NOTIFY,
    (ItemChange.PRESENT, Choice.IN_PROGRESS): RegulatoryStatus.PENDING_GRD,
    (ItemChange.ADDED, Choice.ACTIVE): RegulatoryStatus.ACTIVE,
    (ItemChange.ADDED, Choice.NOT_NOTIFIED): RegulatoryStatus.TO_NOTIFY,
    (ItemChange.ADDED, Choice.IN_PROGRESS): RegulatoryStatus.PENDING_GRD,
    (ItemChange.REMOVED, Choice.ENDED): RegulatoryStatus.ENDED,
    (ItemChange.REMOVED, Choice.NOT_NOTIFIED): RegulatoryStatus.LEAVING,
    (ItemChange.REMOVED, Choice.IN_PROGRESS): RegulatoryStatus.LEAVING,
}


class BaselineError(Exception):
    """A confirmation that does not fit the baseline it confirms."""


@dataclass(frozen=True)
class ChoiceIn:
    key: str
    choice: Choice
    effective_date: datetime.date | None = None
    sent_date: datetime.date | None = None


@dataclass(frozen=True)
class Decision:
    item: Mapping[str, Any]
    choice: Choice
    effective_date: datetime.date | None
    sent_date: datetime.date | None

    @property
    def change(self) -> ItemChange:
        return ItemChange(self.item["change"])

    @property
    def kind(self) -> str:
        if self.change == ItemChange.REMOVED:
            return "meter_removed"
        return "meter_added" if self.item["subject_type"] == _METER else "key_changed"


def item_key(subject_type: int, id_sharing_operation: int, subject_ref: str) -> str:
    return f"{subject_type}:{id_sharing_operation}:{subject_ref}"


def brussels_today(now: datetime.datetime) -> datetime.date:
    return now.astimezone(BRUSSELS).date()


# -- the photo ----------------------------------------------------------------


def _iso(day: datetime.date | None) -> str | None:
    return day.isoformat() if day else None


def _meter_items(photo: CommunityPhoto, change: ItemChange, keep) -> list[dict[str, Any]]:
    return [
        {
            "key": item_key(_METER, m.id_sharing_operation, m.ean),
            "subject_type": _METER,
            "subject_ref": m.ean,
            "id_sharing_operation": m.id_sharing_operation,
            "operation_name": photo.operations.get(m.id_sharing_operation),
            "change": str(change),
            "default": str(DEFAULT[change]),
            "id_member": m.id_member,
            "grd": m.grd,
            "injection_status": m.injection_status,
            "start_date": _iso(m.start_date),
        }
        for m in photo.meters
        if keep(_METER, m.id_sharing_operation, m.ean)
    ]


def _key_items(photo: CommunityPhoto, change: ItemChange, keep) -> list[dict[str, Any]]:
    return [
        {
            "key": item_key(_KEY, k.id_sharing_operation, str(k.id_key)),
            "subject_type": _KEY,
            "subject_ref": str(k.id_key),
            "id_sharing_operation": k.id_sharing_operation,
            "operation_name": photo.operations.get(k.id_sharing_operation),
            "change": str(change),
            "default": str(DEFAULT[change]),
            "label": k.name,
            "start_date": _iso(k.start_date),
        }
        for k in photo.keys
        if keep(_KEY, k.id_sharing_operation, str(k.id_key))
    ]


def _first_items(photo: CommunityPhoto) -> list[dict[str, Any]]:
    def everything(*_subject: object) -> bool:
        return True

    return _meter_items(photo, ItemChange.PRESENT, everything) + _key_items(
        photo, ItemChange.PRESENT, everything
    )


def _difference(
    photo: CommunityPhoto, episodes: Sequence[Episode], filed: set[tuple[int, int, str]]
) -> list[dict[str, Any]]:
    tracked = {(e.subject_type, e.id_sharing_operation, e.subject_ref) for e in episodes}
    known = tracked | filed

    def unknown(subject_type: int, operation: int, ref: str) -> bool:
        return (subject_type, operation, ref) not in known

    present = {(_METER, m.id_sharing_operation, m.ean) for m in photo.meters}
    removed = [
        {
            "key": item_key(e.subject_type, e.id_sharing_operation, e.subject_ref),
            "subject_type": e.subject_type,
            "subject_ref": e.subject_ref,
            "id_sharing_operation": e.id_sharing_operation,
            "operation_name": photo.operations.get(e.id_sharing_operation),
            "change": str(ItemChange.REMOVED),
            "default": str(DEFAULT[ItemChange.REMOVED]),
            "episode_id": e.id,
            "episode_origin": e.id_dossier_origin,
            "valid_from": _iso(e.valid_from),
        }
        for e in episodes
        if e.subject_type == _METER
        and (e.subject_type, e.id_sharing_operation, e.subject_ref) not in present
    ]
    return (
        _meter_items(photo, ItemChange.ADDED, unknown)
        + _key_items(photo, ItemChange.ADDED, unknown)
        + removed
    )


async def _filed_subjects(local: AsyncSession, id_community: int) -> set[tuple[int, int, str]]:
    """Subjects whose addition is already a change in a procedure."""
    rows = await local.execute(
        select(
            ChangeModel.subject_type, ChangeModel.id_sharing_operation, ChangeModel.subject_ref
        ).where(
            ChangeModel.id_community == id_community,
            ChangeModel.status.in_((int(ChangeStatus.ATTACHED), int(ChangeStatus.APPLIED))),
            ChangeModel.kind.in_(_ADD_KINDS),
            ChangeModel.id_sharing_operation.is_not(None),
        )
    )
    return {(int(t), int(o), str(r)) for t, o, r in rows.tuples() if o is not None}


async def _confirmed_before(local: AsyncSession, period: ModuleActivationModel) -> bool:
    earlier = await local.scalar(
        select(ModuleActivationModel.id).where(
            ModuleActivationModel.id_community == period.id_community,
            ModuleActivationModel.id != period.id,
            ModuleActivationModel.baseline_confirmed_at.is_not(None),
        )
    )
    return earlier is not None


async def ensure_baseline(
    local: AsyncSession,
    crm: EngineCrmRead,
    plugin: RegulatorPlugin,
    period: ModuleActivationModel,
    *,
    now: datetime.datetime,
    region_of: RegionOf,
) -> dict[str, Any]:
    """The period's baseline, photographed and opened if it is not yet."""
    if period.baseline_json is not None:
        return dict(period.baseline_json)
    today = brussels_today(now)
    entry = plugin.catalogue().baseline(today)
    if entry is None:
        raise LifecycleError(f"{plugin.code} declares no baseline procedure")

    photo = await crm.community_photo(period.id_community, on=today)
    if await _confirmed_before(local, period):
        kind = "reactivation"
        episodes = await crm.episodes(
            period.id_community, statuses=[int(s) for s in TRACKED_STATUSES]
        )
        items = _difference(photo, episodes, await _filed_subjects(local, period.id_community))
    else:
        kind = "first"
        items = _first_items(photo)

    ctx = await context_for(local, plugin, entry.definition.code, entry.definition.version)
    region = region_of(plugin.code)
    if region is None:
        raise LifecycleError(f"regulator {plugin.code} has no region in regulators.json")
    dossier = await open_procedure(
        local,
        ctx,
        id_community=period.id_community,
        id_sharing_operation=None,
        region=int(region),
        variant=None,
        opened_by=OpenedBy.SYSTEM,
        day=today,
        context={"reason": "baseline", "period": period.id},
    )
    baseline: dict[str, Any] = {
        "taken_at": now.isoformat(),
        "on": today.isoformat(),
        "kind": kind,
        "regulator": plugin.code,
        "id_dossier": dossier.id,
        "items": items,
    }
    period.baseline_json = baseline
    if not items:
        await take_automatic_edge(
            local, ctx, dossier, ProcedureStatus.IN_FORCE, day=today, context={"reason": "empty"}
        )
        period.baseline_confirmed_at = now
        period.confirmed_by = ENGINE_ACTOR
        period.baseline_json = {**baseline, "confirmation": {}}
    await local.flush()
    return dict(period.baseline_json)


def photographed(baseline: Mapping[str, Any], subject_type: int, operation: int, ref: str) -> bool:
    """Is this subject on the baseline as there (present or added)? While the
    baseline awaits confirmation, an addition of it is already covered."""
    key = item_key(subject_type, operation, ref)
    return any(
        item["key"] == key and item["change"] != ItemChange.REMOVED
        for item in baseline.get("items", ())
    )


# -- confirming ---------------------------------------------------------------


def resolve(baseline: Mapping[str, Any], submitted: Sequence[ChoiceIn]) -> list[Decision]:
    """One decision per item: the admin's, or the item's default."""
    items = {item["key"]: item for item in baseline.get("items", ())}
    given: dict[str, ChoiceIn] = {}
    for choice in submitted:
        if choice.key not in items:
            raise BaselineError(f"{choice.key} is not on the baseline")
        if choice.key in given:
            raise BaselineError(f"{choice.key} is chosen twice")
        change = ItemChange(items[choice.key]["change"])
        if choice.choice not in CHOICES[change]:
            raise BaselineError(f"{choice.key} is {change}: {choice.choice} is not an option")
        if choice.choice == Choice.IN_PROGRESS and choice.sent_date is None:
            raise BaselineError(f"{choice.key}: in progress needs the date it was sent")
        given[choice.key] = choice
    decisions = []
    for key, item in items.items():
        picked = given.get(key)
        decisions.append(
            Decision(
                item=item,
                choice=picked.choice if picked else Choice(item["default"]),
                effective_date=picked.effective_date if picked else None,
                sent_date=picked.sent_date if picked else None,
            )
        )
    return decisions


def episode_writes(
    baseline: Mapping[str, Any], decisions: Sequence[Decision], *, today: datetime.date
) -> list[EpisodeWrite]:
    """The core's side of the confirmation, in absolute values. New episodes
    originate in the baseline procedure; a removal moves the episode that
    already exists."""
    origin = int(baseline["id_dossier"])
    writes = []
    for decision in decisions:
        item = decision.item
        status = _STATUS[(decision.change, decision.choice)]
        if decision.change == ItemChange.REMOVED:
            valid_from = item.get("valid_from")
            writes.append(
                EpisodeWrite(
                    id_sharing_operation=int(item["id_sharing_operation"]),
                    subject_type=int(item["subject_type"]),
                    subject_ref=str(item["subject_ref"]),
                    id_dossier_origin=int(item["episode_origin"]),
                    regulatory_status=status,
                    valid_from=datetime.date.fromisoformat(valid_from) if valid_from else None,
                    valid_to=(decision.effective_date or today)
                    if status == RegulatoryStatus.ENDED
                    else None,
                    regulatory_ref=str(origin),
                    episode_id=int(item["episode_id"]),
                )
            )
            continue
        writes.append(
            EpisodeWrite(
                id_sharing_operation=int(item["id_sharing_operation"]),
                subject_type=int(item["subject_type"]),
                subject_ref=str(item["subject_ref"]),
                id_dossier_origin=origin,
                regulatory_status=status,
                valid_from=decision.effective_date if decision.choice == Choice.ACTIVE else None,
                regulatory_ref=str(origin),
            )
        )
    return writes


def _detected(decision: Decision, period_id: int) -> DetectedChange:
    item = decision.item
    return DetectedChange(
        kind=decision.kind,
        subject_type=ChangeSubjectType(int(item["subject_type"])),
        subject_ref=str(item["subject_ref"]),
        id_sharing_operation=int(item["id_sharing_operation"]),
        payload={
            **{k: v for k, v in item.items() if k not in ("key", "change", "default")},
            "baseline": {"period": period_id, "item": item["key"]},
            "effective_date": _iso(decision.effective_date),
        },
    )


async def record_confirmation(
    local: AsyncSession,
    plugin: RegulatorPlugin,
    period: ModuleActivationModel,
    decisions: Sequence[Decision],
    *,
    actor: str,
    now: datetime.datetime,
    region_of: RegionOf,
) -> None:
    """The annexe's side: the changes, their procedures, the baseline in force."""
    baseline = dict(period.baseline_json or {})
    today = brussels_today(now)
    catalogue = plugin.catalogue()
    baseline_dossier = await local.get(DossierModel, int(baseline["id_dossier"]))
    if baseline_dossier is None:
        raise LifecycleError(f"baseline procedure {baseline['id_dossier']} is gone")
    baseline_entry = catalogue.baseline(today)
    if baseline_entry is None:
        raise LifecycleError(f"{plugin.code} declares no baseline procedure")

    sent: dict[tuple[int, str, datetime.date], list[Decision]] = defaultdict(list)
    for decision in decisions:
        change = _detected(decision, period.id)
        if decision.choice in (Choice.ACTIVE, Choice.ENDED):
            local.add(
                ChangeModel(
                    id_community=period.id_community,
                    id_sharing_operation=change.id_sharing_operation,
                    kind=change.kind,
                    subject_type=int(change.subject_type),
                    subject_ref=change.subject_ref,
                    payload_json=dict(change.payload),
                    source_event_id=None,
                    procedure_code=baseline_entry.definition.code,
                    status=int(ChangeStatus.APPLIED),
                    id_dossier=baseline_dossier.id,
                    detected_at=now,
                )
            )
            continue
        targets = catalogue.handling(change.kind, today)
        if not targets:
            raise LifecycleError(f"{plugin.code} files no {change.kind}: cannot record it")
        if decision.choice == Choice.NOT_NOTIFIED:
            for entry in targets:
                await file_change(
                    local,
                    plugin,
                    entry,
                    change,
                    id_community=period.id_community,
                    day=today,
                    at=now,
                    source_event_id=None,
                    region_of=region_of,
                    context={"reason": "baseline", "period": period.id},
                )
            continue
        if decision.sent_date is None:  # resolve() refuses it; kept for the type
            raise BaselineError(f"{decision.item['key']}: in progress needs its sending date")
        sent[
            (int(change.id_sharing_operation or 0), targets[0].definition.code, decision.sent_date)
        ].append(decision)

    for (operation, code, sent_date), group in sorted(sent.items()):
        await _already_sent(
            local,
            plugin,
            period,
            code,
            operation,
            sent_date,
            group,
            actor=actor,
            region_of=region_of,
        )

    ctx = await context_for(
        local, plugin, baseline_entry.definition.code, baseline_entry.definition.version
    )
    await take_automatic_edge(
        local,
        ctx,
        baseline_dossier,
        ProcedureStatus.IN_FORCE,
        day=today,
        context={"reason": "confirmed"},
    )
    period.baseline_confirmed_at = now
    period.confirmed_by = actor
    period.baseline_json = {
        **baseline,
        "confirmation": {
            d.item["key"]: {
                "choice": str(d.choice),
                "effective_date": _iso(d.effective_date),
                "sent_date": _iso(d.sent_date),
            }
            for d in decisions
        },
    }
    await local.flush()


async def _already_sent(
    local: AsyncSession,
    plugin: RegulatorPlugin,
    period: ModuleActivationModel,
    code: str,
    operation: int,
    sent_date: datetime.date,
    group: Sequence[Decision],
    *,
    actor: str,
    region_of: RegionOf,
) -> None:
    """One procedure in its "sent" step for what was already sent together."""
    entry = plugin.catalogue().in_force(code, sent_date) or plugin.catalogue().in_force(
        code, brussels_today(datetime.datetime.now(datetime.UTC))
    )
    if entry is None:
        raise LifecycleError(f"{plugin.code} has no {code} in force on {sent_date}")
    definition = entry.definition
    changes = [_detected(d, period.id) for d in group]
    ctx = await context_for(local, plugin, definition.code, definition.version)
    region = region_of(plugin.code)
    if region is None:
        raise LifecycleError(f"regulator {plugin.code} has no region in regulators.json")
    variant = plugin.variant(definition, [c.fact() for c in changes])
    path = path_to(definition, definition.initial_step, ProcedureStatus.SENT, variant)
    if not path:
        raise LifecycleError(f"{definition.code} has no way to a sent step")
    dossier = await open_procedure(
        local,
        ctx,
        id_community=period.id_community,
        id_sharing_operation=operation if definition.scope == "operation" else None,
        region=int(region),
        variant=variant,
        opened_by=OpenedBy.USER,
        day=sent_date,
        actor=actor,
        context={"reason": "baseline", "period": period.id, "sent_on": sent_date.isoformat()},
        step=path[-1],
    )
    for change in changes:
        local.add(
            ChangeModel(
                id_community=period.id_community,
                id_sharing_operation=dossier.id_sharing_operation,
                kind=change.kind,
                subject_type=int(change.subject_type),
                subject_ref=change.subject_ref,
                payload_json=dict(change.payload),
                source_event_id=None,
                procedure_code=definition.code,
                status=int(ChangeStatus.ATTACHED),
                id_dossier=dossier.id,
                detected_at=datetime.datetime.now(datetime.UTC),
            )
        )
    await local.flush()
    await recompute(local, ctx, dossier)
    sent_at = datetime.datetime.combine(sent_date, datetime.time(), BRUSSELS)
    dossier.frozen_at = sent_at
    dossier.submitted_at = sent_at
    await local.flush()


async def driving_refs(
    local: AsyncSession, period: ModuleActivationModel
) -> list[tuple[int, int, str, str]]:
    """(origin, subject type, subject ref, procedure id) for each new episode
    whose status a procedure other than the baseline now drives."""
    baseline = period.baseline_json or {}
    origin = int(baseline.get("id_dossier") or 0)
    if not origin:
        return []
    rows = await local.scalars(
        select(ChangeModel).where(
            ChangeModel.id_community == period.id_community,
            ChangeModel.source_event_id.is_(None),
            ChangeModel.status == int(ChangeStatus.ATTACHED),
            ChangeModel.kind.in_(_ADD_KINDS),
        )
    )
    refs = []
    for change in rows:
        marker = (change.payload_json or {}).get("baseline") or {}
        if marker.get("period") == period.id and change.id_dossier is not None:
            refs.append((origin, change.subject_type, change.subject_ref, str(change.id_dossier)))
    return refs
