"""What the core shows of each filed subject: the episodes the annexe writes.

Spec, « Cycle de vie et répercussion sur le CRM » (the badge table) and
« Écriture des effets ». A change about a meter or a key of an operation has a
row in the core's ``regulatory_link_status`` (an *episode*), and the row says
where the procedure carrying the change stands:

- an addition (a meter, an installation, a key) OWNS its row, keyed by the
  procedure where it was first filed: the row's origin. A deferral moves the
  change to another procedure, never the origin. One the baseline filed owns
  the row the baseline confirmation wrote;
- a removal MOVES the row of what it removes: the addition's, or the one the
  baseline or an earlier procedure left in the core;
- a key that takes effect ends the operation's previous key.

Each change pins the row it owns or moves in ``payload_json["episode"]``, once,
the first time it is planned: the identity never depends on where the change
sits later, nor on what the core holds by then.

``plan`` folds every change on a subject into one absolute value per row and
returns the writes that differ from the core, so planning twice writes nothing
the second time. What a row says:

====================================  ===================  ======================
change, its procedure's step          addition (own row)   removal (on its row)
====================================  ===================  ======================
attached, draft or ready; deferred    1 to notify          6 leaving
attached, a "sent" step               the plugin's 2/3/4   6 leaving
  (with the effect date, once known)  4 from that date     6 until the day before
applied (in force on date E)          5 active from E      7 ended the day before E
refused                               8 refused            back as it was
excluded, compensated, cancelled      9 withdrawn          back as it was
attached in a lapsed procedure        1 to notify          back as it was
====================================  ===================  ======================

A removal moves a row only while the row is pending, authorised or active (2
to 5): removing a meter the regulator refused changes nothing. A key that
takes effect ends the operation's active key rows the same way. A change
APPLIED is the core's truth (the in-force write put it there): its row is
kept as it is, unless a later change moves it. ``valid_to`` is the last day
in the operation, like ``meter_data.end_date``: a meter leaving on E and
joining another operation on E occupies each on consecutive days.

The engine decides which rows, statuses and dates; the plugin only says which
regulatory status each of its "sent" steps means
(``RegulatorPlugin.episode_status``).

Nothing here commits, except ``push`` and ``record_effect_error``, which run
after the caller's local commit (see them).
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.catalogue import ProcedureDefinition
from engine.contracts import (
    ADDITION_KINDS,
    REMOVAL_KINDS,
    PluginRegistry,
    RegulatorPlugin,
    plugin_for,
)
from ports.core_effects import CoreEffectsPort, EffectConflictError, EpisodeWrite
from ports.crm_engine import EngineCrmRead, Episode
from shared.const import ChangeStatus, ChangeSubjectType, ProcedureStatus, RegulatoryStatus
from shared.models.local_models import (
    ChangeModel,
    DossierModel,
    ModuleActivationModel,
    ProcedureDefinitionModel,
)

logger = logging.getLogger(__name__)

_METER = int(ChangeSubjectType.METER)
_KEY = int(ChangeSubjectType.KEY)

ADDITIONS = {kind: int(subject) for kind, subject in ADDITION_KINDS.items()}
REMOVALS = {kind: int(subject) for kind, subject in REMOVAL_KINDS.items()}
TRACKED_KINDS = {**ADDITIONS, **REMOVALS}

# A removal or a new key moves a row only while it is one of these.
_MOVABLE = frozenset(
    {
        RegulatoryStatus.PENDING_GRD,
        RegulatoryStatus.PENDING_REGULATOR,
        RegulatoryStatus.AUTHORIZED,
        RegulatoryStatus.ACTIVE,
    }
)
_DAY = datetime.timedelta(days=1)

RowKey = tuple[int, int, str]  # (origin, subject type, subject ref): UNIQUE in the core


@dataclass(frozen=True)
class StepOverride:
    """Plan a procedure as if it stood on ``step``: what a response would do
    (the dry run), or what going in force writes before it is journaled."""

    step: str
    effective_date: datetime.date | None = None


@dataclass(frozen=True)
class RowState:
    status: RegulatoryStatus
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None
    ref: str | None = None

    @classmethod
    def of(cls, episode: Episode) -> RowState:
        return cls(
            status=RegulatoryStatus(episode.regulatory_status),
            valid_from=episode.valid_from,
            valid_to=episode.valid_to,
            ref=episode.regulatory_ref,
        )

    def pinned(self) -> dict[str, Any]:
        return {
            "status": int(self.status),
            "from": self.valid_from.isoformat() if self.valid_from else None,
            "to": self.valid_to.isoformat() if self.valid_to else None,
            "ref": self.ref,
        }

    @classmethod
    def from_pin(cls, value: Mapping[str, Any]) -> RowState:
        return cls(
            status=RegulatoryStatus(int(value["status"])),
            valid_from=_day(value.get("from")),
            valid_to=_day(value.get("to")),
            ref=value.get("ref"),
        )


# A change APPLIED: its row is the core's, as the in-force write left it.
_KEEP = object()


@dataclass(frozen=True)
class _Procedure:
    id: int
    step: str
    common: ProcedureStatus
    effective_date: datetime.date | None
    definition: ProcedureDefinition
    plugin: RegulatorPlugin
    overridden: bool = False


@dataclass
class _Row:
    id_sharing_operation: int
    owner: ChangeModel | None = None
    # (change, its pin): removals moving this row, keys replacing it.
    movers: list[tuple[ChangeModel, Mapping[str, Any]]] = field(default_factory=list)


def _day(value: Any) -> datetime.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def is_tracked(change: ChangeModel) -> bool:
    """A change whose subject the core tracks: a meter or a key of an operation."""
    return (
        change.kind in TRACKED_KINDS
        and change.subject_type == TRACKED_KINDS[change.kind]
        and change.id_sharing_operation is not None
    )


# -- loading ---------------------------------------------------------------------


async def _changes_of(
    local: AsyncSession, id_community: int, dossier_ids: Iterable[int]
) -> list[ChangeModel]:
    ids = sorted(set(dossier_ids))
    if not ids:
        return []
    rows = await local.scalars(
        select(ChangeModel).where(
            ChangeModel.id_community == id_community,
            ChangeModel.id_dossier.in_(ids),
            ChangeModel.kind.in_(list(TRACKED_KINDS)),
        )
    )
    return [c for c in rows if is_tracked(c)]


async def _changes_on(
    local: AsyncSession, id_community: int, subjects: set[tuple[int, int, str]]
) -> list[ChangeModel]:
    """Every tracked change on these (operation, type, ref) subjects, oldest
    first: the procedures that touched them, wherever they are."""
    if not subjects:
        return []
    rows = await local.scalars(
        select(ChangeModel)
        .where(
            ChangeModel.id_community == id_community,
            ChangeModel.subject_ref.in_(sorted({s[2] for s in subjects})),
            ChangeModel.id_sharing_operation.in_(sorted({s[0] for s in subjects})),
            ChangeModel.kind.in_(list(TRACKED_KINDS)),
        )
        .order_by(ChangeModel.id)
    )
    return [
        c
        for c in rows
        if is_tracked(c)
        and (int(c.id_sharing_operation or 0), c.subject_type, c.subject_ref) in subjects
    ]


async def _procedures(
    local: AsyncSession,
    registry: PluginRegistry,
    ids: Iterable[int],
    overrides: Mapping[int, StepOverride],
) -> dict[int, _Procedure]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = await local.execute(
        select(DossierModel, ProcedureDefinitionModel.code, ProcedureDefinitionModel.version)
        .join(
            ProcedureDefinitionModel,
            ProcedureDefinitionModel.id == DossierModel.id_procedure_definition,
        )
        .where(DossierModel.id.in_(wanted))
    )
    procedures = {}
    for dossier, code, version in rows.tuples():
        plugin = plugin_for(registry, dossier.regulator)
        entry = plugin.catalogue().get(code, version) if plugin else None
        if plugin is None or entry is None:
            continue
        override = overrides.get(dossier.id)
        step = override.step if override else str(dossier.step)
        procedures[dossier.id] = _Procedure(
            id=dossier.id,
            step=step,
            common=entry.definition.step(step).common_status,
            effective_date=(override.effective_date if override else None)
            or dossier.effective_date,
            definition=entry.definition,
            plugin=plugin,
            overridden=override is not None,
        )
    return procedures


# -- pins ------------------------------------------------------------------------


async def _baseline_origins(local: AsyncSession, changes: Sequence[ChangeModel]) -> dict[int, int]:
    """{period id: its baseline procedure}, for the changes the baseline filed."""
    periods = {
        int(marker["period"])
        for c in changes
        if isinstance(marker := (c.payload_json or {}).get("baseline"), Mapping)
        and marker.get("period") is not None
    }
    if not periods:
        return {}
    rows = await local.execute(
        select(ModuleActivationModel.id, ModuleActivationModel.baseline_json).where(
            ModuleActivationModel.id.in_(sorted(periods))
        )
    )
    return {
        int(period): int(baseline["id_dossier"])
        for period, baseline in rows.tuples()
        if baseline and baseline.get("id_dossier")
    }


def _addition_origin(change: ChangeModel, baseline_origins: Mapping[int, int]) -> int | None:
    marker = (change.payload_json or {}).get("baseline")
    if isinstance(marker, Mapping) and marker.get("period") is not None:
        origin = baseline_origins.get(int(marker["period"]))
        if origin is not None:
            return origin
    return change.id_dossier


def _prior(episode: Episode) -> dict[str, Any]:
    """The row as a removal found it. A row still leaving (the baseline wrote it
    so for a removal it filed) was active before."""
    state = RowState.of(episode)
    if state.status == RegulatoryStatus.LEAVING:
        state = RowState(RegulatoryStatus.ACTIVE, state.valid_from, None, state.ref)
    return state.pinned()


async def _pins(
    local: AsyncSession,
    crm: EngineCrmRead,
    id_community: int,
    changes: Sequence[ChangeModel],
    procedures: Mapping[int, _Procedure],
    *,
    persist: bool,
) -> dict[int, dict[str, Any]]:
    baseline_origins = await _baseline_origins(local, changes)
    pins: dict[int, dict[str, Any]] = {}
    for change in changes:
        payload = dict(change.payload_json or {})
        pin = dict(payload.get("episode") or {})
        operation = int(change.id_sharing_operation or 0)
        if change.kind in ADDITIONS:
            if "origin" not in pin:
                origin = _addition_origin(change, baseline_origins)
                if origin is None:
                    continue  # not filed yet (a baseline awaiting confirmation)
                pin["origin"] = origin
            procedure = procedures.get(change.id_dossier or 0)
            if (
                change.kind == "key_changed"
                and "replaces" not in pin
                and procedure is not None
                and procedure.overridden
                and procedure.common == ProcedureStatus.IN_FORCE
            ):
                pin["replaces"] = [
                    {"origin": e.id_dossier_origin, "ref": e.subject_ref, "prior": _prior(e)}
                    for e in await crm.active_keys(id_community, operation)
                    if e.subject_ref != change.subject_ref
                ]
        elif "origin" not in pin:
            found = await _removal_pin(
                crm, id_community, change, changes, pins, operation=operation
            )
            if found is None:
                continue  # nothing to move, for now; the next plan looks again
            pin = found
        pins[change.id] = pin
        if persist and payload.get("episode") != pin:
            change.payload_json = {**payload, "episode": pin}
    if persist:
        await local.flush()
    return pins


async def _removal_pin(
    crm: EngineCrmRead,
    id_community: int,
    removal: ChangeModel,
    changes: Sequence[ChangeModel],
    pins: Mapping[int, Mapping[str, Any]],
    *,
    operation: int,
) -> dict[str, Any] | None:
    payload = removal.payload_json or {}
    if payload.get("episode_origin") is not None:  # a removal the baseline listed
        return {
            "origin": int(payload["episode_origin"]),
            "prior": RowState(
                RegulatoryStatus.ACTIVE,
                _day(payload.get("valid_from")),
                None,
                str(payload["episode_origin"]),
            ).pinned(),
        }
    owner = next(
        (
            c
            for c in reversed(changes)
            if c.id < removal.id
            and c.kind in ADDITIONS
            and c.subject_ref == removal.subject_ref
            and c.status
            in (int(ChangeStatus.ATTACHED), int(ChangeStatus.DEFERRED), int(ChangeStatus.APPLIED))
            and c.id in pins
        ),
        None,
    )
    episode = await crm.live_episode(
        id_community,
        id_sharing_operation=operation,
        subject_type=_METER,
        subject_ref=removal.subject_ref,
    )
    if owner is not None:
        origin = int(pins[owner.id]["origin"])
        pin: dict[str, Any] = {"origin": origin}
        if episode is not None and episode.id_dossier_origin == origin:
            pin["prior"] = _prior(episode)
        return pin
    if episode is None:
        return None
    return {"origin": episode.id_dossier_origin, "prior": _prior(episode)}


# -- folding ---------------------------------------------------------------------


def _addition_state(
    change: ChangeModel, procedure: _Procedure | None, *, exists: bool
) -> RowState | object | None:
    """What an addition's own row says. None: nothing to write."""
    ref = str(change.id_dossier) if change.id_dossier is not None else None
    withdrawn = RowState(RegulatoryStatus.WITHDRAWN, ref=ref) if exists else None
    status = ChangeStatus(change.status)
    if status == ChangeStatus.APPLIED:
        return _KEEP
    if status == ChangeStatus.REFUSED:
        return RowState(RegulatoryStatus.REFUSED, ref=ref)
    if status in (ChangeStatus.EXCLUDED, ChangeStatus.CANCELLED):
        return withdrawn
    if status == ChangeStatus.DEFERRED:
        return RowState(RegulatoryStatus.TO_NOTIFY, ref=ref)
    if status != ChangeStatus.ATTACHED or procedure is None:
        return None
    common = procedure.common
    if common in (ProcedureStatus.DRAFT, ProcedureStatus.READY, ProcedureStatus.LAPSED):
        return RowState(RegulatoryStatus.TO_NOTIFY, ref=ref)
    if common == ProcedureStatus.SENT:
        meant = procedure.plugin.episode_status(procedure.definition, procedure.step)
        if meant is None:
            return None
        start = procedure.effective_date if meant == RegulatoryStatus.AUTHORIZED else None
        return RowState(meant, valid_from=start, ref=ref)
    if common == ProcedureStatus.IN_FORCE:
        if procedure.overridden and procedure.effective_date is not None:
            return RowState(RegulatoryStatus.ACTIVE, valid_from=procedure.effective_date, ref=ref)
        return _KEEP
    if common == ProcedureStatus.REFUSED:
        return RowState(RegulatoryStatus.REFUSED, ref=ref)
    if common == ProcedureStatus.CANCELLED:
        return withdrawn
    return None


def _move(mover: ChangeModel, procedure: _Procedure | None) -> tuple[str, Any] | None:
    """What a removal (or a key taking effect) does to the row it moves:
    ("leaving", last day or None), ("ended", last day), ("applied", None), or
    None when it no longer moves anything."""
    status = ChangeStatus(mover.status)
    removal = mover.kind in REMOVALS
    if status == ChangeStatus.APPLIED:
        return ("applied", None)
    if status == ChangeStatus.DEFERRED and removal:
        return ("leaving", None)
    if status != ChangeStatus.ATTACHED or procedure is None:
        return None
    effective = procedure.effective_date
    if procedure.common == ProcedureStatus.IN_FORCE and procedure.overridden and effective:
        return ("ended", effective - _DAY)
    if not removal:
        return None  # a new key ends the previous one only when it takes effect
    if procedure.common in (ProcedureStatus.DRAFT, ProcedureStatus.READY):
        return ("leaving", None)
    if procedure.common == ProcedureStatus.SENT:
        return ("leaving", effective - _DAY if effective else None)
    return None


def _fold(
    row: _Row, current: Episode | None, procedures: Mapping[int, _Procedure]
) -> RowState | None:
    base: RowState | object | None = _KEEP
    if row.owner is not None:
        base = _addition_state(
            row.owner, procedures.get(row.owner.id_dossier or 0), exists=current is not None
        )
    moves = [(m, pin, _move(m, procedures.get(m.id_dossier or 0))) for m, pin in row.movers]
    if any(move is not None and move[0] == "applied" for _m, _pin, move in moves):
        return None  # an exit in force: the core holds it
    live = [(m, move) for m, _pin, move in moves if move is not None]
    if base is _KEEP:
        # The row is the core's (or another procedure's): what the latest
        # removal found there is what it goes back to once nothing moves it. A
        # key replaces a row only as it takes effect, so it never needs one.
        priors = [pin["prior"] for m, pin in row.movers if pin.get("prior") and m.kind in REMOVALS]
        if priors:
            base = RowState.from_pin(priors[-1])
        elif current is not None:
            base = RowState.of(current)
        else:
            return None
    if not isinstance(base, RowState):
        return None
    if not live or base.status not in _MOVABLE:
        return base
    mover, (what, last_day) = live[-1]
    ref = str(mover.id_dossier) if mover.id_dossier is not None else base.ref
    if what == "ended":
        if base.valid_from is not None and last_day < base.valid_from:
            return RowState(RegulatoryStatus.WITHDRAWN, base.valid_from, None, ref)
        return RowState(RegulatoryStatus.ENDED, base.valid_from, last_day, ref)
    if last_day is not None and base.valid_from is not None and last_day < base.valid_from:
        last_day = None
    return RowState(RegulatoryStatus.LEAVING, base.valid_from, last_day, ref)


# -- the plan --------------------------------------------------------------------


async def plan(
    local: AsyncSession,
    crm: EngineCrmRead,
    registry: PluginRegistry,
    id_community: int,
    dossier_ids: Iterable[int],
    *,
    overrides: Mapping[int, StepOverride] | None = None,
    persist_pins: bool = True,
) -> list[EpisodeWrite]:
    """The writes that bring the core in line with these procedures: every
    row a tracked change of theirs owns or moves, folded with every other
    change on the same subjects, minus what the core already holds.

    ``overrides`` plans a procedure on another step (``StepOverride``);
    ``persist_pins=False`` (a dry run) computes the pins without saving them.
    Flushes, never commits."""
    overrides = overrides or {}
    seed = await _changes_of(local, id_community, dossier_ids)
    if not seed:
        return []
    subjects = {(int(c.id_sharing_operation or 0), c.subject_type, c.subject_ref) for c in seed}
    changes = await _changes_on(local, id_community, subjects)
    procedures = await _procedures(
        local, registry, {c.id_dossier for c in changes if c.id_dossier is not None}, overrides
    )
    pins = await _pins(local, crm, id_community, changes, procedures, persist=persist_pins)

    rows: dict[RowKey, _Row] = {}
    for change in changes:
        pin = pins.get(change.id)
        if pin is None:
            continue
        operation = int(change.id_sharing_operation or 0)
        key = (int(pin["origin"]), change.subject_type, change.subject_ref)
        row = rows.setdefault(key, _Row(operation))
        if change.kind in ADDITIONS:
            row.owner = change  # the latest addition on the row wins
            for replaced in pin.get("replaces") or ():
                old = (int(replaced["origin"]), _KEY, str(replaced["ref"]))
                rows.setdefault(old, _Row(operation)).movers.append((change, replaced))
        else:
            row.movers.append((change, pin))

    current = await crm.episode_rows(id_community, list(rows))
    writes = []
    for key, row in rows.items():
        state = _fold(row, current.get(key), procedures)
        if state is None:
            continue
        held = current.get(key)
        if held is not None and RowState.of(held) == state:
            continue
        origin, subject_type, subject_ref = key
        writes.append(
            EpisodeWrite(
                id_sharing_operation=row.id_sharing_operation,
                subject_type=subject_type,
                subject_ref=subject_ref,
                id_dossier_origin=origin,
                regulatory_status=state.status,
                valid_from=state.valid_from,
                valid_to=state.valid_to,
                regulatory_ref=state.ref,
            )
        )
    return writes


async def tracked_dossiers(local: AsyncSession, id_community: int) -> list[int]:
    """The procedures holding a tracked change that may still move: what the
    daily reconciliation re-plans. A finished procedure's rows were written
    when it finished; one still carrying an error is retried."""
    rows = await local.execute(
        select(ChangeModel.id_dossier)
        .join(DossierModel, DossierModel.id == ChangeModel.id_dossier)
        .where(
            ChangeModel.id_community == id_community,
            ChangeModel.kind.in_(list(TRACKED_KINDS)),
            ChangeModel.id_sharing_operation.is_not(None),
            (
                DossierModel.status.in_(
                    [
                        int(ProcedureStatus.DRAFT),
                        int(ProcedureStatus.READY),
                        int(ProcedureStatus.SENT),
                    ]
                )
            )
            | DossierModel.effect_error_json.is_not(None),
        )
        .distinct()
    )
    return sorted(int(d) for d in rows.scalars() if d is not None)


# -- after the local commit ------------------------------------------------------


def error_of(kind: str, exc: Exception, *, at: datetime.datetime) -> dict[str, Any]:
    """``dossier.effect_error_json``: why the core refused, for the detail."""
    if isinstance(exc, EffectConflictError):
        return {"kind": kind, "code": "exclude", "subjects": exc.subjects, "at": at.isoformat()}
    return {"kind": kind, "code": "unavailable", "subjects": [], "at": at.isoformat()}


async def push(
    crm_session: AsyncSession,
    effects: CoreEffectsPort,
    id_community: int,
    writes: Sequence[EpisodeWrite],
    *,
    audit_payload: dict[str, Any],
) -> int:
    """Apply the writes in the core and commit it. Run after the local commit
    that saved the pins; on any failure the core is rolled back and the error
    raised, for ``record_effect_error``. A rejected write leaves the row as it
    was: the next plan computes the same writes and tries again."""
    if not writes:
        return 0
    try:
        written = await effects.apply(id_community, writes, audit_payload=audit_payload)
        await crm_session.commit()
    except Exception:
        await crm_session.rollback()
        raise
    return written


async def record_effect_error(
    local: AsyncSession, dossier_ids: Iterable[int], error: dict[str, Any] | None, *, kind: str
) -> None:
    """Set (or, with ``error=None``, clear) the procedures' effect error of this
    ``kind`` ("sync" for the badges, "in_force" for the effect date), in a
    transaction of its own: plain values, no ORM state read across the commit.
    An error of the other kind is left alone."""
    ids = sorted(set(dossier_ids))
    if not ids:
        return
    for dossier in await local.scalars(select(DossierModel).where(DossierModel.id.in_(ids))):
        held = dossier.effect_error_json
        if error is None:
            if held and held.get("kind") == kind:
                dossier.effect_error_json = None
        elif not held or held.get("kind") == kind or kind == "in_force":
            dossier.effect_error_json = error
    await local.commit()
