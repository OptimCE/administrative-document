"""The CWaPE plugin's calendar: Wallonia's holidays, held as data."""

from __future__ import annotations

import datetime

import pytest
from workalendar.europe import Belgium

from regulators.cwape.plugin import CwapePlugin

DATA = CwapePlugin().calendar()
YEARS = range(DATA.years.first, DATA.years.last + 1)


@pytest.mark.parametrize("year", YEARS)
def test_every_year_lists_the_belgian_legal_holidays(year):
    """workalendar is the independent oracle: the file must say what it says."""
    listed = sorted(h.day for h in DATA.holidays if h.day.year == year)
    assert listed == sorted(day for day, _label in Belgium().holidays(year))


def test_counts_on_monday_to_friday():
    assert DATA.region == "WAL"
    assert not DATA.saturday_is_working


def test_covers_a_year_beyond_today():
    """Deadlines run months ahead (6 months, then 40 working days), and past the
    file's last year the engine refuses to count. Extend WAL.yaml when this
    fails: it is meant to, a year before the calendar would run out."""
    assert (
        datetime.date.today().year + 1 <= DATA.years.last
    ), f"regulators/cwape/calendar/WAL.yaml ends in {DATA.years.last}: add the next years"
