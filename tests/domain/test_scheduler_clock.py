"""When the daily sweep next fires.

Pure — no session, no clock stubbing — because `seconds_until_next_run` takes
`now` as an argument precisely so the wrap-around and timezone cases can be
asserted without waiting a day. Lives under tests/domain/ with the other
session-free tests.
"""

import datetime
from zoneinfo import ZoneInfo

from worker.scheduler import seconds_until_next_run

BRUSSELS = ZoneInfo("Europe/Brussels")


class TestNextRunClock:
    """Pure, so the wrap-around cases are testable without waiting a day."""

    def test_a_time_before_the_hour_waits_until_today(self):
        now = datetime.datetime(2026, 8, 3, 4, 0, tzinfo=BRUSSELS)
        assert seconds_until_next_run(now, 6) == 2 * 3600

    def test_a_time_after_the_hour_waits_until_tomorrow(self):
        now = datetime.datetime(2026, 8, 3, 7, 0, tzinfo=BRUSSELS)
        assert seconds_until_next_run(now, 6) == 23 * 3600

    def test_exactly_on_the_hour_waits_a_full_day_rather_than_firing_twice(self):
        now = datetime.datetime(2026, 8, 3, 6, 0, tzinfo=BRUSSELS)
        assert seconds_until_next_run(now, 6) == 24 * 3600

    def test_a_utc_instant_is_converted_before_comparing(self):
        """The hour is a LOCAL one; comparing in UTC would drift with DST."""
        # 03:00 UTC in August is 05:00 in Brussels, so 06:00 local is an hour off.
        now = datetime.datetime(2026, 8, 3, 3, 0, tzinfo=datetime.UTC)
        assert seconds_until_next_run(now, 6) == 3600
