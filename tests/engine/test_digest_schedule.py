"""When a community's summary email is due (``engine/digest_schedule.py``).

Pure dates. The daily job runs at 06:00 in Brussels; 2026-04-14 is a Tuesday.
"""

from __future__ import annotations

import datetime

import pytest

from engine.digest_schedule import (
    Schedule,
    coming_on,
    due_on,
    first_dates,
    first_day,
    is_scheduled,
    next_on,
)
from engine.events import BRUSSELS
from shared.const import DigestCadence

D = datetime.date
HOUR = 6
TUESDAY = 2
WEDNESDAY = 3

WORKING_DAYS = Schedule(DigestCadence.WORKING_DAYS)
TUESDAYS = Schedule(DigestCadence.WEEKLY, weekday=TUESDAY)
WEDNESDAYS = Schedule(DigestCadence.WEEKLY, weekday=WEDNESDAY)
EVERY_OTHER_TUESDAY = Schedule(DigestCadence.BIWEEKLY, weekday=TUESDAY, first_on=D(2026, 4, 14))
FIRST_TUESDAYS = Schedule(DigestCadence.MONTHLY, weekday=TUESDAY)


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    """A Brussels wall-clock time."""
    return datetime.datetime.combine(day, datetime.time(hour, minute), BRUSSELS)


@pytest.mark.parametrize(
    ("schedule", "day", "expected"),
    [
        (WORKING_DAYS, D(2026, 4, 17), True),  # Friday
        (WORKING_DAYS, D(2026, 4, 18), False),  # Saturday
        (WORKING_DAYS, D(2026, 4, 19), False),  # Sunday
        (WORKING_DAYS, D(2026, 4, 20), True),  # Monday
        (TUESDAYS, D(2026, 4, 14), True),
        (TUESDAYS, D(2026, 4, 15), False),
        (EVERY_OTHER_TUESDAY, D(2026, 4, 14), True),
        (EVERY_OTHER_TUESDAY, D(2026, 4, 21), False),
        (EVERY_OTHER_TUESDAY, D(2026, 4, 28), True),
        (EVERY_OTHER_TUESDAY, D(2026, 4, 7), False),  # before the first send
        (EVERY_OTHER_TUESDAY, D(2027, 1, 5), True),  # 38 weeks on, across the year end
        (EVERY_OTHER_TUESDAY, D(2027, 1, 12), False),
        (FIRST_TUESDAYS, D(2026, 4, 7), True),
        (FIRST_TUESDAYS, D(2026, 4, 14), False),
        (FIRST_TUESDAYS, D(2026, 5, 5), True),  # May begins on a Friday
        (FIRST_TUESDAYS, D(2027, 1, 5), True),
    ],
)
def test_the_scheduled_dates(schedule, day, expected):
    assert is_scheduled(schedule, day) is expected


@pytest.mark.parametrize(
    ("schedule", "after", "expected"),
    [
        (WORKING_DAYS, D(2026, 4, 17), D(2026, 4, 20)),  # Friday, then Monday
        (TUESDAYS, D(2026, 4, 14), D(2026, 4, 21)),
        (EVERY_OTHER_TUESDAY, D(2026, 4, 1), D(2026, 4, 14)),  # the first send
        (EVERY_OTHER_TUESDAY, D(2026, 4, 14), D(2026, 4, 28)),
        (FIRST_TUESDAYS, D(2026, 4, 7), D(2026, 5, 5)),
        (FIRST_TUESDAYS, D(2026, 12, 1), D(2027, 1, 5)),  # across the year end
    ],
)
def test_the_next_date(schedule, after, expected):
    assert next_on(schedule, after) == expected


@pytest.mark.parametrize(
    ("since", "expected"),
    [
        (_at(D(2026, 4, 14), 5, 59), D(2026, 4, 14)),  # before the day's run
        (_at(D(2026, 4, 14), 6, 0), D(2026, 4, 14)),
        (_at(D(2026, 4, 14), 6, 1), D(2026, 4, 15)),  # after it: the next day
        # Late at night in UTC is already the next day in Brussels.
        (datetime.datetime(2026, 4, 13, 23, 30, tzinfo=datetime.UTC), D(2026, 4, 14)),
        # The clocks went forward overnight: 06:00 is 04:00 UTC.
        (datetime.datetime(2026, 3, 29, 3, 59, tzinfo=datetime.UTC), D(2026, 3, 29)),
        (datetime.datetime(2026, 3, 29, 4, 1, tzinfo=datetime.UTC), D(2026, 3, 30)),
    ],
)
def test_the_first_date_that_counts(since, expected):
    assert first_day(since, hour=HOUR) == expected


class TestDue:
    def test_on_its_date(self):
        on = due_on(
            TUESDAYS, D(2026, 4, 21), since=_at(D(2026, 4, 1), 9), last_due_on=None, hour=HOUR
        )
        assert on == D(2026, 4, 21)

    def test_not_between_its_dates(self):
        on = due_on(
            TUESDAYS,
            D(2026, 4, 22),
            since=_at(D(2026, 4, 1), 9),
            last_due_on=D(2026, 4, 21),
            hour=HOUR,
        )
        assert on is None

    def test_a_missed_run_is_caught_up_at_the_next(self):
        # Wednesday's run never happened; Thursday's sends Wednesday's.
        on = due_on(
            WEDNESDAYS,
            D(2026, 4, 16),
            since=_at(D(2026, 4, 1), 9),
            last_due_on=D(2026, 4, 8),
            hour=HOUR,
        )
        assert on == D(2026, 4, 15)

    def test_a_long_outage_sends_one_not_every_date_missed(self):
        on = due_on(
            WORKING_DAYS,
            D(2026, 4, 21),
            since=_at(D(2026, 4, 1), 9),
            last_due_on=D(2026, 4, 16),
            hour=HOUR,
        )
        assert on == D(2026, 4, 21)

    def test_turned_on_after_the_days_run_waits_for_the_next_date(self):
        since = _at(D(2026, 4, 14), 10)  # a Tuesday, after 06:00
        assert due_on(TUESDAYS, D(2026, 4, 14), since=since, last_due_on=None, hour=HOUR) is None
        assert due_on(TUESDAYS, D(2026, 4, 15), since=since, last_due_on=None, hour=HOUR) is None
        assert due_on(TUESDAYS, D(2026, 4, 21), since=since, last_due_on=None, hour=HOUR) == D(
            2026, 4, 21
        )

    def test_turned_on_before_the_days_run_counts_that_day(self):
        since = _at(D(2026, 4, 14), 5)
        assert due_on(TUESDAYS, D(2026, 4, 14), since=since, last_due_on=None, hour=HOUR) == D(
            2026, 4, 14
        )

    def test_a_date_handled_is_not_due_again(self):
        on = due_on(
            TUESDAYS,
            D(2026, 4, 14),
            since=_at(D(2026, 4, 1), 9),
            last_due_on=D(2026, 4, 14),
            hour=HOUR,
        )
        assert on is None


class TestComing:
    @pytest.mark.parametrize(
        ("schedule", "now", "expected"),
        [
            (TUESDAYS, _at(D(2026, 4, 15), 10), D(2026, 4, 21)),
            (TUESDAYS, _at(D(2026, 4, 14), 5), D(2026, 4, 14)),  # this morning's run
            (TUESDAYS, _at(D(2026, 4, 14), 7), D(2026, 4, 21)),
            (EVERY_OTHER_TUESDAY, _at(D(2026, 4, 15), 10), D(2026, 4, 28)),
            (FIRST_TUESDAYS, _at(D(2026, 4, 15), 10), D(2026, 5, 5)),
            (WORKING_DAYS, _at(D(2026, 4, 17), 10), D(2026, 4, 20)),
        ],
    )
    def test_the_next_send(self, schedule, now, expected):
        since = _at(D(2026, 4, 1), 9)
        assert coming_on(schedule, now=now, since=since, last_due_on=None, hour=HOUR) == expected

    def test_not_a_date_already_handled(self):
        now = _at(D(2026, 4, 14), 5)  # before the run, but a rerun already handled it
        on = coming_on(
            TUESDAYS, now=now, since=_at(D(2026, 4, 1), 9), last_due_on=D(2026, 4, 14), hour=HOUR
        )
        assert on == D(2026, 4, 21)

    def test_not_before_the_rhythm_was_set(self):
        since = _at(D(2026, 4, 14), 10)
        on = coming_on(TUESDAYS, now=since, since=since, last_due_on=None, hour=HOUR)
        assert on == D(2026, 4, 21)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_at(D(2026, 4, 15), 10), [D(2026, 4, 21), D(2026, 4, 28)]),
        (_at(D(2026, 4, 14), 5), [D(2026, 4, 14), D(2026, 4, 21)]),  # before its run
        (_at(D(2026, 4, 14), 7), [D(2026, 4, 21), D(2026, 4, 28)]),
    ],
)
def test_the_first_sends_offered_every_two_weeks(now, expected):
    assert first_dates(TUESDAY, now=now, hour=HOUR) == expected
