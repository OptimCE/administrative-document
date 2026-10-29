"""Working-day arithmetic over a region's holidays, held as data.

The holidays come from the plugin's calendar file
(``regulators/<plugin>/calendar/<REGION>.yaml``), not from a library: which
days a regulator counts is a regulatory fact, and a file a reviewer can read
and diff is the honest place for it. The catalogue sync projects the same days
into ``working_calendar``.

A file covers a declared range of years, and the price of data over a formula
is that it runs out. So a computation that steps outside the range raises
``CalendarNotCoveredError`` instead of quietly treating the year as one without
holidays: a deadline one day late is exactly what this module exists to
prevent.

Day counting: ``add_working_days`` treats the start as day 0 and returns the
Nth working day strictly after it ("within 10 working days of sending").
Month offsets are calendar months (``relativedelta``, clamped to the end of a
shorter month). Nothing here rolls a date that lands on a holiday forward.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import yaml
from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# An offset is small (10, 20, 40 working days). This only exists so a corrupt
# rule can never spin the loop below for long.
_MAX_WORKING_DAY_OFFSET = 3650

# Monday=0 … Friday=4, Saturday=5, Sunday=6.
_FRIDAY = 4
_SATURDAY = 5


class CalendarError(Exception):
    """A calendar file that cannot be used."""


class CalendarNotCoveredError(Exception):
    """A computation reached a day outside the years the calendar covers."""

    def __init__(self, region: str, day: datetime.date, first: int, last: int) -> None:
        super().__init__(
            f"the {region} working calendar covers {first}-{last}, not {day.isoformat()}: "
            f"extend the plugin's calendar file"
        )
        self.region = region
        self.day = day


class Holiday(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: datetime.date
    label: str = Field(min_length=1, max_length=128)


class YearRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    first: int = Field(alias="from", ge=2000, le=2100)
    last: int = Field(alias="to", ge=2000, le=2100)


class CalendarData(BaseModel):
    """A region's calendar file, validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    region: str = Field(pattern=r"^[A-Z]{2,8}$")
    # Belgian law knows "jours ouvrables" (Saturday counts) and "jours ouvrés"
    # (it does not). Which one a regulator means is its call, so it is data.
    saturday_is_working: bool = False
    years: YearRange
    holidays: tuple[Holiday, ...]

    @model_validator(mode="after")
    def _consistent(self) -> CalendarData:
        if self.years.first > self.years.last:
            raise ValueError(f"years run backwards: {self.years.first} > {self.years.last}")
        days = [holiday.day for holiday in self.holidays]
        duplicates = sorted({day for day in days if days.count(day) > 1})
        if duplicates:
            raise ValueError(f"listed twice: {', '.join(d.isoformat() for d in duplicates)}")
        outside = [d for d in days if not self.years.first <= d.year <= self.years.last]
        if outside:
            raise ValueError(
                f"outside {self.years.first}-{self.years.last}: "
                f"{', '.join(d.isoformat() for d in sorted(outside))}"
            )
        # Every year has public holidays, so a covered year without any is a
        # block missing from the file, not a year off.
        empty = sorted(
            set(range(self.years.first, self.years.last + 1)) - {day.year for day in days}
        )
        if empty:
            raise ValueError(f"no holiday listed for {', '.join(map(str, empty))}")
        return self


def load_calendar(path: Path) -> CalendarData:
    """Read and validate one calendar file; the file name must be its region."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        data = CalendarData.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise CalendarError(f"{path}: {exc}") from exc
    if path.stem != data.region:
        raise CalendarError(f"{path}: the file of region {data.region} must be {data.region}.yaml")
    return data


class WorkingCalendar:
    """Working days of one region, from its ``CalendarData``."""

    def __init__(self, data: CalendarData) -> None:
        self.region = data.region
        self._first = data.years.first
        self._last = data.years.last
        self._last_working_weekday = _SATURDAY if data.saturday_is_working else _FRIDAY
        self._holidays = frozenset(holiday.day for holiday in data.holidays)

    def _covered(self, day: datetime.date) -> None:
        if not self._first <= day.year <= self._last:
            raise CalendarNotCoveredError(self.region, day, self._first, self._last)

    def is_working_day(self, day: datetime.date) -> bool:
        self._covered(day)
        return day.weekday() <= self._last_working_weekday and day not in self._holidays

    def add_working_days(self, start: datetime.date, count: int) -> datetime.date:
        """The ``count``-th working day strictly after ``start`` (day 0)."""
        if count < 0:
            raise ValueError("a working-day offset must not be negative")
        if count > _MAX_WORKING_DAY_OFFSET:
            raise ValueError(f"a working-day offset of {count} is implausibly large")
        current = start
        remaining = count
        while remaining:
            current += datetime.timedelta(days=1)
            if self.is_working_day(current):
                remaining -= 1
        return current

    @staticmethod
    def add_calendar_days(start: datetime.date, count: int) -> datetime.date:
        if count < 0:
            raise ValueError("a calendar-day offset must not be negative")
        return start + datetime.timedelta(days=count)

    @staticmethod
    def add_months(start: datetime.date, count: int) -> datetime.date:
        """``start`` plus ``count`` calendar months; 31/01 + 1 month is 28/02."""
        if count < 0:
            raise ValueError("a month offset must not be negative")
        shifted: datetime.date = start + relativedelta(months=count)
        return shifted

    def working_days_until(self, today: datetime.date, due: datetime.date) -> int:
        """Working days from ``today`` to ``due``: "due in N working days".

        Counts the working days in (today, due]; negative when ``due`` is past,
        counting the working days in (due, today]. Zero on the due day itself.
        """
        if due == today:
            return 0
        sign, low, high = (1, today, due) if due > today else (-1, due, today)
        count = 0
        current = low
        while current < high:
            current += datetime.timedelta(days=1)
            if self.is_working_day(current):
                count += 1
        return sign * count
