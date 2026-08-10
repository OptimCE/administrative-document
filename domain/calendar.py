"""Belgian business-day and calendar-month arithmetic for regulatory deadlines.

Why a library and not a static holiday table: several Belgian public holidays are
movable feasts (Easter Monday, Ascension, Whit Monday) whose dates come from the
computus. A hand-maintained table would need regenerating every year and would
fail *silently* — a deadline landing one day late is exactly the kind of error
this module exists to prevent. ``workalendar`` computes them.

Day counting: ``add_business_days`` treats the base date as day 0 and returns the
Nth business day strictly after it, which is the usual reading of "within 10
business days of submission".

Weekend convention: the default is **jours ouvres** (Monday-Friday). Belgian law
also knows *jours ouvrables*, which excludes only Sundays and public holidays —
so Saturday counts. Which one a given CWaPE deadline means is a regulatory
question, so it is a constructor flag rather than a hard-coded assumption; see
``saturday_is_working``.

Month offsets ("+6 months", "+1 year") are deliberately *calendar* months via
``relativedelta``: a legal period expressed in months runs on the wall clock, not
on business days.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable

from dateutil.relativedelta import relativedelta
from workalendar.europe import Belgium

# A rule offset is small (10, 15 business days). This only exists so a corrupt
# rule row can never spin the loop below forever.
_MAX_BUSINESS_DAY_OFFSET = 3650


class BelgianCalendar:
    """Business-day arithmetic over the Belgian federal public holidays.

    ``extra_holidays`` injects additional non-working dates (a regional feast a
    specific procedure honours, a one-off administrative closure) without a code
    change. Instances are cheap; the per-year holiday set is memoised.
    """

    def __init__(
        self,
        *,
        extra_holidays: Iterable[datetime.date] | None = None,
        saturday_is_working: bool = False,
    ) -> None:
        self._calendar = Belgium()
        self._extra_holidays = frozenset(extra_holidays or ())
        self._saturday_is_working = saturday_is_working
        self._holidays_by_year: dict[int, frozenset[datetime.date]] = {}

    # -- holidays ---------------------------------------------------------

    def holidays(self, year: int) -> frozenset[datetime.date]:
        """The Belgian public holidays for ``year``, plus any injected extras."""
        cached = self._holidays_by_year.get(year)
        if cached is None:
            cached = frozenset(day for day, _label in self._calendar.holidays(year))
            self._holidays_by_year[year] = cached
        return cached | {d for d in self._extra_holidays if d.year == year}

    def is_business_day(self, day: datetime.date) -> bool:
        """True when ``day`` counts towards a business-day deadline."""
        # Monday=0 … Saturday=5, Sunday=6.
        last_working_weekday = 5 if self._saturday_is_working else 4
        if day.weekday() > last_working_weekday:
            return False
        return day not in self.holidays(day.year)

    # -- arithmetic -------------------------------------------------------

    def add_business_days(self, start: datetime.date, count: int) -> datetime.date:
        """Return the ``count``-th business day strictly after ``start``.

        ``start`` is day 0 and is never returned for ``count > 0``, whether or
        not it is itself a business day.
        """
        if count < 0:
            raise ValueError("business-day offsets must not be negative")
        if count > _MAX_BUSINESS_DAY_OFFSET:
            raise ValueError(f"business-day offset {count} is implausibly large")

        current = start
        remaining = count
        while remaining > 0:
            current += datetime.timedelta(days=1)
            if self.is_business_day(current):
                remaining -= 1
        return current

    def add_months(self, start: datetime.date, count: int) -> datetime.date:
        """Return ``start`` shifted by ``count`` calendar months.

        ``relativedelta`` clamps to the end of a shorter target month, so
        31 January + 1 month is 28/29 February rather than an error.
        """
        shifted: datetime.date = start + relativedelta(months=count)
        return shifted
