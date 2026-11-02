"""When a community's summary email is due (``digest_setting``), as dates.

An admin turns the summary on and picks a rhythm (``DigestCadence``): every
working day, a weekday every week, a weekday every two weeks from a first
date, or the month's first such weekday. The daily job runs at
``DEADLINE_SWEEP_HOUR_LOCAL`` (Brussels) and handles a community when a
scheduled date has come since the last one it handled, so a run the worker
missed is caught up at the next one.

A date counts only if its run comes after the rhythm was set (``since``):
Tuesdays, turned on a Tuesday at 10:00, waits for next Tuesday instead of
sending one on Wednesday for a run that took place before it was on.

Pure: the API shows « Prochain envoi » with it, the worker decides with it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from engine.events import BRUSSELS
from shared.const import DigestCadence

#: Wider than any rhythm's gap (a month and its first weekday's drift).
_SPAN_DAYS = 62
_FRIDAY = 5
_FORTNIGHT = 14
#: The month's first such weekday falls on one of its first seven days.
_FIRST_WEEK_LAST_DAY = 7


@dataclass(frozen=True)
class Schedule:
    cadence: DigestCadence
    weekday: int | None = None  # ISO, 1 Monday … 7 Sunday; None: working days
    first_on: datetime.date | None = None  # every two weeks: the first send


def is_scheduled(schedule: Schedule, day: datetime.date) -> bool:
    if schedule.cadence == DigestCadence.WORKING_DAYS:
        return day.isoweekday() <= _FRIDAY
    if day.isoweekday() != schedule.weekday:
        return False
    if schedule.cadence == DigestCadence.BIWEEKLY:
        first = schedule.first_on
        return first is not None and day >= first and (day - first).days % _FORTNIGHT == 0
    if schedule.cadence == DigestCadence.MONTHLY:
        return day.day <= _FIRST_WEEK_LAST_DAY
    return True


def next_on(schedule: Schedule, after: datetime.date) -> datetime.date | None:
    """The first scheduled date after ``after``."""
    first = schedule.first_on
    if schedule.cadence == DigestCadence.BIWEEKLY and first is not None and after < first:
        return first
    for offset in range(1, _SPAN_DAYS + 1):
        day = after + datetime.timedelta(days=offset)
        if is_scheduled(schedule, day):
            return day
    return None


def first_day(since: datetime.datetime, *, hour: int) -> datetime.date:
    """The first date whose run comes at or after ``since``: its own day when
    set before the run hour, else the next."""
    local = since.astimezone(BRUSSELS)
    run = datetime.datetime.combine(local.date(), datetime.time(hour), BRUSSELS)
    return local.date() if local <= run else local.date() + datetime.timedelta(days=1)


def due_on(
    schedule: Schedule,
    today: datetime.date,
    *,
    since: datetime.datetime,
    last_due_on: datetime.date | None,
    hour: int,
) -> datetime.date | None:
    """The latest scheduled date on or before ``today`` not handled yet and
    whose run came after ``since``; None when nothing is due."""
    floor = first_day(since, hour=hour)
    if last_due_on is not None:
        floor = max(floor, last_due_on + datetime.timedelta(days=1))
    day = today
    while day >= floor and (today - day).days <= _SPAN_DAYS:
        if is_scheduled(schedule, day):
            return day
        day -= datetime.timedelta(days=1)
    return None


def coming_on(
    schedule: Schedule,
    *,
    now: datetime.datetime,
    since: datetime.datetime,
    last_due_on: datetime.date | None,
    hour: int,
) -> datetime.date | None:
    """« Prochain envoi »: the next scheduled date whose run is still to come."""
    floor = max(first_day(since, hour=hour), first_day(now, hour=hour))
    if last_due_on is not None:
        floor = max(floor, last_due_on + datetime.timedelta(days=1))
    if is_scheduled(schedule, floor):
        return floor
    return next_on(schedule, floor)


def first_dates(weekday: int, *, now: datetime.datetime, hour: int) -> list[datetime.date]:
    """Every two weeks: the two dates the admin may start from, the next two
    such weekdays whose run is still to come."""
    start = first_day(now, hour=hour)
    first = start + datetime.timedelta(days=(weekday - start.isoweekday()) % 7)
    return [first, first + datetime.timedelta(days=7)]
