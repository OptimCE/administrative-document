"""Belgian business-day arithmetic.

A deadline landing one day off is the failure mode this module exists to
prevent, so the movable feasts (whose dates come from the computus) get explicit
coverage rather than being trusted to the library.
"""

import datetime

import pytest

from domain.calendar import BelgianCalendar


@pytest.fixture
def calendar() -> BelgianCalendar:
    return BelgianCalendar()


class TestHolidays:
    def test_the_ten_federal_holidays_are_known(self, calendar):
        assert len(calendar.holidays(2026)) == 10

    @pytest.mark.parametrize(
        ("day", "label"),
        [
            (datetime.date(2026, 1, 1), "New Year"),
            (datetime.date(2026, 4, 6), "Easter Monday (movable)"),
            (datetime.date(2026, 5, 1), "Labour Day"),
            (datetime.date(2026, 5, 14), "Ascension (movable)"),
            (datetime.date(2026, 5, 25), "Whit Monday (movable)"),
            (datetime.date(2026, 7, 21), "National Day"),
            (datetime.date(2026, 8, 15), "Assumption"),
            (datetime.date(2026, 11, 1), "All Saints"),
            (datetime.date(2026, 11, 11), "Armistice"),
            (datetime.date(2026, 12, 25), "Christmas"),
        ],
    )
    def test_known_belgian_holidays(self, calendar, day, label):
        assert day in calendar.holidays(2026), label
        assert not calendar.is_business_day(day), label

    def test_movable_feasts_track_easter_across_years(self, calendar):
        # Easter Monday: 2026-04-06, 2027-03-29. A static table would drift here.
        assert datetime.date(2026, 4, 6) in calendar.holidays(2026)
        assert datetime.date(2027, 3, 29) in calendar.holidays(2027)


class TestBusinessDays:
    def test_weekend_is_not_a_business_day(self, calendar):
        assert not calendar.is_business_day(datetime.date(2026, 7, 18))  # Saturday
        assert not calendar.is_business_day(datetime.date(2026, 7, 19))  # Sunday

    def test_offset_skips_the_weekend(self, calendar):
        # Friday + 1 business day is the following Monday.
        assert calendar.add_business_days(datetime.date(2026, 7, 17), 1) == datetime.date(
            2026, 7, 20
        )

    def test_offset_skips_a_public_holiday(self, calendar):
        # Monday 20 Jul + 1 skips National Day (Tue 21 Jul) and lands on Wed 22.
        assert calendar.add_business_days(datetime.date(2026, 7, 20), 1) == datetime.date(
            2026, 7, 22
        )

    def test_offset_skips_a_movable_feast(self, calendar):
        # Friday 3 Apr + 1 skips Easter Monday (6 Apr) and lands on Tue 7 Apr.
        assert calendar.add_business_days(datetime.date(2026, 4, 3), 1) == datetime.date(2026, 4, 7)

    def test_the_base_date_is_day_zero(self, calendar):
        """ "Within N business days" counts from the day after the act."""
        monday = datetime.date(2026, 9, 7)
        assert calendar.is_business_day(monday)
        assert calendar.add_business_days(monday, 0) == monday
        assert calendar.add_business_days(monday, 1) == datetime.date(2026, 9, 8)

    def test_ten_business_days_is_two_calendar_weeks(self, calendar):
        # The CWaPE completeness check, over a holiday-free stretch.
        assert calendar.add_business_days(datetime.date(2026, 9, 7), 10) == datetime.date(
            2026, 9, 21
        )

    def test_fifteen_business_days_from_a_weekend_start(self, calendar):
        # A Saturday base date still yields a business day.
        due = calendar.add_business_days(datetime.date(2026, 9, 5), 15)
        assert calendar.is_business_day(due)

    def test_negative_offset_is_rejected(self, calendar):
        with pytest.raises(ValueError, match="negative"):
            calendar.add_business_days(datetime.date(2026, 9, 7), -1)

    def test_implausible_offset_is_rejected(self, calendar):
        """A corrupt rule row must not spin the day-stepping loop."""
        with pytest.raises(ValueError, match="implausibly large"):
            calendar.add_business_days(datetime.date(2026, 9, 7), 100_000)


class TestConventionSwitches:
    def test_jours_ouvrables_counts_saturday(self):
        """The Belgian legal reading excludes only Sundays and public holidays."""
        ouvrables = BelgianCalendar(saturday_is_working=True)
        assert ouvrables.add_business_days(datetime.date(2026, 7, 17), 1) == datetime.date(
            2026, 7, 18
        )

    def test_jours_ouvrables_still_skips_sunday_and_holidays(self):
        ouvrables = BelgianCalendar(saturday_is_working=True)
        assert not ouvrables.is_business_day(datetime.date(2026, 7, 19))  # Sunday
        assert not ouvrables.is_business_day(datetime.date(2026, 7, 21))  # National Day

    def test_extra_holidays_are_honoured(self):
        """The seam for a closure the federal calendar does not know about."""
        closed = BelgianCalendar(extra_holidays=[datetime.date(2026, 9, 8)])
        assert not closed.is_business_day(datetime.date(2026, 9, 8))
        assert closed.add_business_days(datetime.date(2026, 9, 7), 1) == datetime.date(2026, 9, 9)


class TestMonths:
    def test_plus_six_months(self, calendar):
        assert calendar.add_months(datetime.date(2026, 3, 15), 6) == datetime.date(2026, 9, 15)

    def test_month_end_clamps_to_a_shorter_month(self, calendar):
        assert calendar.add_months(datetime.date(2026, 8, 31), 6) == datetime.date(2027, 2, 28)

    def test_leap_day_clamps_on_a_non_leap_year(self, calendar):
        assert calendar.add_months(datetime.date(2028, 2, 29), 12) == datetime.date(2029, 2, 28)

    def test_months_are_calendar_months_not_business_days(self, calendar):
        """A legal period in months runs on the wall clock, weekends included."""
        due = calendar.add_months(datetime.date(2026, 3, 14), 6)  # a Saturday
        assert due == datetime.date(2026, 9, 14)
