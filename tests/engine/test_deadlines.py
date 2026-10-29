"""engine.deadlines: due dates from catalogue rules (acceptance criterion 1.4)."""

from __future__ import annotations

import datetime

import pytest

from engine.calendar import WorkingCalendar
from engine.catalogue import DeadlineRule
from engine.deadlines import due_date
from regulators.testing.fake import FakeRegulator
from tests.engine.belgian_calendar import belgian_calendar

D = datetime.date
LABELS = {"fr": "Échéance", "en": "Deadline", "nl": "Termijn", "de": "Frist"}


def _relative(value: int, unit: str) -> DeadlineRule:
    return DeadlineRule.model_validate(
        {
            "code": "clock",
            "kind": "relative",
            "anchor": "step:sent",
            "offset": {"value": value, "unit": unit},
            "party": "grd",
            "deadline_kind": "response",
            "labels": LABELS,
        }
    )


ANNUAL = DeadlineRule.model_validate(
    {
        "code": "filing",
        "kind": "annual",
        "annual": {"month": 9, "day": 1},
        "party": "community",
        "deadline_kind": "submission",
        "labels": LABELS,
    }
)


class TestOnTheBelgianCalendar:
    @pytest.mark.parametrize(
        ("rule", "anchor", "expected"),
        [
            # A GRD's 10 working days, sent the day before Christmas Eve.
            (_relative(10, "working_days"), D(2026, 12, 23), D(2027, 1, 8)),
            # 20 working days across National Day.
            (_relative(20, "working_days"), D(2026, 7, 1), D(2026, 7, 30)),
            # 40 working days across Easter Monday, 1 May, Ascension, Whit Monday.
            (_relative(40, "working_days"), D(2026, 4, 1), D(2026, 6, 2)),
            # Six months, clamped to the end of February.
            (_relative(6, "months"), D(2026, 8, 31), D(2027, 2, 28)),
            # Thirty calendar days, holidays included.
            (_relative(30, "calendar_days"), D(2026, 12, 15), D(2027, 1, 14)),
        ],
    )
    def test_relative_rules(self, rule, anchor, expected):
        assert due_date(rule, belgian_calendar(), anchor=anchor) == expected

    def test_an_annual_rule_falls_on_its_day_of_the_period(self):
        assert due_date(ANNUAL, belgian_calendar(), period_year=2026) == D(2026, 9, 1)


class TestThroughTheFakeRegulator:
    def test_the_plugins_own_holidays_are_the_ones_counted(self):
        plugin = FakeRegulator()
        entry = plugin.catalogue().in_force("FAKE_CHANGE", D(2026, 3, 1))
        assert entry is not None
        rule = next(r for r in entry.definition.deadline_rules if r.code == "grd_answer")
        calendar = WorkingCalendar(plugin.calendar())
        # TST makes Monday 2 March 2026 a holiday; no real calendar does.
        assert due_date(rule, calendar, anchor=D(2026, 2, 26)) == D(2026, 3, 13)


class TestMissingInputs:
    def test_a_relative_rule_needs_its_anchor_day(self):
        with pytest.raises(ValueError, match="needs the day its anchor happened"):
            due_date(_relative(10, "working_days"), belgian_calendar())

    def test_an_annual_rule_needs_the_period(self):
        with pytest.raises(ValueError, match="needs the procedure's period year"):
            due_date(ANNUAL, belgian_calendar(), anchor=D(2026, 1, 1))
