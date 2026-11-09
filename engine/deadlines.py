"""Due dates from catalogue rules.

Pure: the rule and the calendar come in, a date comes out. Which rules fire on
an event is the definition's to say (``rules_started_by``); persisting the
deadline is the caller's.
"""

from __future__ import annotations

import datetime

from engine.calendar import WorkingCalendar
from engine.catalogue import DeadlineRule


def due_date(
    rule: DeadlineRule,
    calendar: WorkingCalendar,
    *,
    anchor: datetime.date | None = None,
    period_year: int | None = None,
) -> datetime.date:
    """The day ``rule`` falls due.

    A relative rule counts from ``anchor``, the day its anchor event happened;
    an annual rule falls on its day of ``period_year``, the procedure's period.
    """
    if rule.kind == "annual":
        if rule.annual is None or period_year is None:
            raise ValueError(f"annual rule {rule.code} needs the procedure's period year")
        return rule.annual.in_year(period_year)

    if rule.offset is None or anchor is None:
        raise ValueError(f"relative rule {rule.code} needs the day its anchor happened")
    if rule.offset.unit == "working_days":
        return calendar.add_working_days(anchor, rule.offset.value)
    if rule.offset.unit == "calendar_days":
        return calendar.add_calendar_days(anchor, rule.offset.value)
    return calendar.add_months(anchor, rule.offset.value)
