"""Turning ``deadline_rule`` rows into concrete due dates (spec R3).

The regulatory clocks live in the database as data, so adding or amending one is
a seed change rather than a deployment. This module is the pure half of that: it
takes the candidate rules a transition matched and returns the deadlines they
imply. Persisting them (and the idempotency that stops a replayed event creating
duplicates) is the repository's job.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from domain.calendar import BelgianCalendar
from shared.const import OffsetUnit


@dataclass(frozen=True)
class DeadlineRuleSpec:
    """The fields of a ``deadline_rule`` row this module needs.

    A plain value object rather than the ORM model so the rules engine stays
    testable without a database.
    """

    id: int
    deadline_type: str
    offset_value: int
    offset_unit: int
    recurring: bool = False
    recur_months: int | None = None
    id_community: int | None = None


@dataclass(frozen=True)
class DerivedDeadline:
    """A deadline a transition implies, before it is persisted."""

    deadline_type: str
    due_date: datetime.date
    recurring: bool
    id_deadline_rule: int


def resolve_rules(candidates: Iterable[DeadlineRuleSpec]) -> list[DeadlineRuleSpec]:
    """Collapse candidate rules to one per ``deadline_type``, most specific wins.

    Reference rules ship platform-wide (``id_community IS NULL``) and a community
    may override individual ones. Resolution is therefore *per deadline_type*,
    not a single global pick: a community that overrides only its completeness
    check still inherits the platform lapse rule.
    """
    best: dict[str, DeadlineRuleSpec] = {}
    for rule in candidates:
        current = best.get(rule.deadline_type)
        if current is None:
            best[rule.deadline_type] = rule
            continue
        # A community-scoped rule beats the platform default; between two of the
        # same specificity the later id wins (deterministic, and matches the
        # "latest row" intuition when reference data is re-seeded).
        current_is_default = current.id_community is None
        rule_is_default = rule.id_community is None
        overrides_default = current_is_default and not rule_is_default
        same_specificity_but_newer = current_is_default == rule_is_default and rule.id > current.id
        if overrides_default or same_specificity_but_newer:
            best[rule.deadline_type] = rule
    return sorted(best.values(), key=lambda r: r.deadline_type)


def due_date_for(
    rule: DeadlineRuleSpec, base_date: datetime.date, calendar: BelgianCalendar
) -> datetime.date:
    """Apply a single rule's offset to the transition's base date."""
    if rule.offset_unit == OffsetUnit.BUSINESS_DAYS:
        return calendar.add_business_days(base_date, rule.offset_value)
    if rule.offset_unit == OffsetUnit.MONTHS:
        return calendar.add_months(base_date, rule.offset_value)
    raise ValueError(f"unknown offset unit {rule.offset_unit!r} on deadline_rule {rule.id}")


def evaluate(
    candidates: Sequence[DeadlineRuleSpec],
    base_date: datetime.date,
    calendar: BelgianCalendar,
) -> list[DerivedDeadline]:
    """Return the deadlines implied by ``candidates`` firing on ``base_date``.

    ``candidates`` are the rules the repository matched on
    (region, dossier_type, trigger_event); this resolves overrides and computes
    the due dates. Ordering is stable so callers and tests can rely on it.
    """
    return [
        DerivedDeadline(
            deadline_type=rule.deadline_type,
            due_date=due_date_for(rule, base_date, calendar),
            recurring=rule.recurring,
            id_deadline_rule=rule.id,
        )
        for rule in resolve_rules(candidates)
    ]


def next_occurrence(
    previous_due_date: datetime.date,
    recur_months: int,
    calendar: BelgianCalendar,
) -> datetime.date:
    """The due date of the occurrence following ``previous_due_date``.

    Anchored on the previous *due date*, not on when it was actually met, so an
    annual obligation stays on the same calendar slot instead of drifting later
    every year.
    """
    if recur_months <= 0:
        raise ValueError("recur_months must be positive for a recurring deadline")
    return calendar.add_months(previous_due_date, recur_months)
