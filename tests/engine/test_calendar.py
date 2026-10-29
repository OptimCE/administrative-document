"""engine.calendar: working days over holidays held as data."""

from __future__ import annotations

import datetime

import pytest
import yaml
from workalendar.europe import Belgium

from engine.calendar import (
    CalendarData,
    CalendarError,
    CalendarNotCoveredError,
    WorkingCalendar,
    load_calendar,
)
from regulators.testing.fake import CALENDAR_FILE
from tests.engine.belgian_calendar import BELGIAN_HOLIDAYS, belgian_calendar

D = datetime.date


@pytest.mark.parametrize("year", sorted(BELGIAN_HOLIDAYS))
def test_the_hand_written_holidays_are_the_belgian_ones(year):
    expected = sorted(day for day, _label in Belgium().holidays(year))
    assert sorted(D.fromisoformat(day) for day, _ in BELGIAN_HOLIDAYS[year]) == expected


class TestWorkingDays:
    @pytest.mark.parametrize(
        ("start", "count", "expected", "why"),
        [
            (D(2026, 4, 3), 1, D(2026, 4, 7), "the weekend, then Easter Monday"),
            (D(2026, 5, 13), 1, D(2026, 5, 15), "Ascension"),
            (D(2026, 5, 22), 1, D(2026, 5, 26), "the weekend, then Whit Monday"),
            (D(2026, 4, 30), 10, D(2026, 5, 18), "1 May, then Ascension"),
            (D(2026, 7, 20), 1, D(2026, 7, 22), "National Day"),
            (D(2026, 8, 14), 1, D(2026, 8, 17), "15 August on a Saturday adds nothing"),
            (D(2026, 11, 10), 1, D(2026, 11, 12), "11 November"),
            (D(2026, 12, 23), 5, D(2026, 12, 31), "Christmas"),
            (D(2026, 12, 23), 7, D(2027, 1, 5), "Christmas, then into the next year"),
            (D(2026, 7, 1), 20, D(2026, 7, 30), "20 working days across 21 July"),
            (D(2026, 4, 1), 40, D(2026, 6, 2), "40 working days across four holidays"),
        ],
    )
    def test_belgian_holidays_are_skipped(self, start, count, expected, why):
        assert belgian_calendar().add_working_days(start, count) == expected, why

    def test_the_start_is_day_zero(self):
        calendar = belgian_calendar()
        assert calendar.add_working_days(D(2026, 12, 25), 0) == D(2026, 12, 25)
        # Even when the start is not a working day, day 1 is the next one.
        assert calendar.add_working_days(D(2026, 12, 25), 1) == D(2026, 12, 28)

    def test_saturday_counts_when_the_calendar_says_so(self):
        calendar = belgian_calendar(saturday_is_working=True)
        assert calendar.add_working_days(D(2026, 8, 7), 1) == D(2026, 8, 8)
        # …but a Saturday holiday still does not.
        assert calendar.add_working_days(D(2026, 8, 14), 1) == D(2026, 8, 17)

    def test_holidays_and_weekends_are_not_working_days(self):
        calendar = belgian_calendar()
        assert not calendar.is_working_day(D(2026, 7, 21))
        assert not calendar.is_working_day(D(2026, 7, 25))  # Saturday
        assert calendar.is_working_day(D(2026, 7, 22))

    def test_a_negative_offset_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            belgian_calendar().add_working_days(D(2026, 1, 5), -1)


class TestOtherOffsets:
    def test_months_clamp_to_the_end_of_a_shorter_month(self):
        assert WorkingCalendar.add_months(D(2026, 8, 31), 6) == D(2027, 2, 28)
        assert WorkingCalendar.add_months(D(2026, 1, 15), 6) == D(2026, 7, 15)

    def test_calendar_days_ignore_holidays(self):
        assert WorkingCalendar.add_calendar_days(D(2026, 12, 15), 30) == D(2027, 1, 14)


class TestWorkingDaysUntil:
    def test_counts_working_days_ahead(self):
        # 22-24 Dec, 28-31 Dec, 4-5 Jan: Christmas and New Year are skipped.
        assert belgian_calendar().working_days_until(D(2026, 12, 21), D(2027, 1, 5)) == 9

    def test_negative_once_past(self):
        assert belgian_calendar().working_days_until(D(2027, 1, 5), D(2026, 12, 21)) == -9

    def test_zero_on_the_day(self):
        assert belgian_calendar().working_days_until(D(2026, 5, 4), D(2026, 5, 4)) == 0


class TestCoverage:
    def test_stepping_past_the_last_year_raises(self):
        # The first day it cannot judge, even if that day would not have counted.
        with pytest.raises(CalendarNotCoveredError, match="covers 2026-2027, not 2028-01-01"):
            belgian_calendar().add_working_days(D(2027, 12, 30), 2)

    def test_a_day_before_the_first_year_raises(self):
        with pytest.raises(CalendarNotCoveredError):
            belgian_calendar().is_working_day(D(2025, 12, 31))


def _calendar(**changes):
    data = {
        "region": "TST",
        "years": {"from": 2026, "to": 2026},
        "holidays": [{"day": D(2026, 1, 1), "label": "New year"}],
    }
    data.update(changes)
    return data


class TestCalendarFiles:
    def test_the_fake_calendar_loads(self):
        data = load_calendar(CALENDAR_FILE)
        assert data.region == "TST"
        assert not data.saturday_is_working

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            (
                {
                    "holidays": [
                        {"day": D(2026, 1, 1), "label": "a"},
                        {"day": D(2026, 1, 1), "label": "b"},
                    ]
                },
                "listed twice: 2026-01-01",
            ),
            ({"holidays": [{"day": D(2027, 1, 1), "label": "a"}]}, "outside 2026-2026"),
            ({"years": {"from": 2026, "to": 2027}}, "no holiday listed for 2027"),
            ({"years": {"from": 2027, "to": 2026}}, "years run backwards"),
            ({"region": "tst"}, "region"),
            ({"weekend": "sunday"}, "Extra inputs are not permitted"),
        ],
    )
    def test_an_inconsistent_calendar_is_refused(self, changes, message):
        with pytest.raises(ValueError, match=message):
            CalendarData.model_validate(_calendar(**changes))

    def test_the_file_name_must_be_the_region(self, tmp_path):
        path = tmp_path / "XYZ.yaml"
        path.write_text(yaml.safe_dump(_calendar()), encoding="utf-8")
        with pytest.raises(CalendarError, match="must be TST.yaml"):
            load_calendar(path)

    def test_a_broken_file_names_itself(self, tmp_path):
        path = tmp_path / "TST.yaml"
        path.write_text("region: [", encoding="utf-8")
        with pytest.raises(CalendarError, match="TST.yaml"):
            load_calendar(path)
