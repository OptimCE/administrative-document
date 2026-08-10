"""Rule evaluation: turning deadline_rule rows into due dates (spec R3)."""

import datetime

import pytest

from domain import deadlines
from domain.calendar import BelgianCalendar
from domain.deadlines import DeadlineRuleSpec
from shared.const import OffsetUnit


@pytest.fixture
def calendar() -> BelgianCalendar:
    return BelgianCalendar()


def _rule(**overrides) -> DeadlineRuleSpec:
    defaults = {
        "id": 1,
        "deadline_type": "completeness_check",
        "offset_value": 10,
        "offset_unit": int(OffsetUnit.BUSINESS_DAYS),
    }
    return DeadlineRuleSpec(**{**defaults, **overrides})


class TestDueDate:
    def test_business_day_rule(self, calendar):
        due = deadlines.due_date_for(_rule(), datetime.date(2026, 9, 7), calendar)
        assert due == datetime.date(2026, 9, 21)

    def test_month_rule(self, calendar):
        rule = _rule(deadline_type="lapse", offset_value=6, offset_unit=int(OffsetUnit.MONTHS))
        due = deadlines.due_date_for(rule, datetime.date(2026, 3, 15), calendar)
        assert due == datetime.date(2026, 9, 15)

    def test_unknown_offset_unit_is_rejected(self, calendar):
        with pytest.raises(ValueError, match="unknown offset unit"):
            deadlines.due_date_for(_rule(offset_unit=99), datetime.date(2026, 9, 7), calendar)


class TestEvaluate:
    def test_no_rules_yields_no_deadlines(self, calendar):
        assert deadlines.evaluate([], datetime.date(2026, 9, 7), calendar) == []

    def test_one_deadline_per_rule(self, calendar):
        rules = [
            _rule(id=1, deadline_type="completeness_check"),
            _rule(id=2, deadline_type="lapse", offset_value=6, offset_unit=int(OffsetUnit.MONTHS)),
        ]
        derived = deadlines.evaluate(rules, datetime.date(2026, 9, 7), calendar)
        assert [d.deadline_type for d in derived] == ["completeness_check", "lapse"]

    def test_result_carries_the_originating_rule(self, calendar):
        derived = deadlines.evaluate([_rule(id=42)], datetime.date(2026, 9, 7), calendar)
        assert derived[0].id_deadline_rule == 42

    def test_recurring_flag_is_propagated(self, calendar):
        rule = _rule(
            deadline_type="annual_report",
            offset_value=12,
            offset_unit=int(OffsetUnit.MONTHS),
            recurring=True,
            recur_months=12,
        )
        derived = deadlines.evaluate([rule], datetime.date(2026, 9, 7), calendar)
        assert derived[0].recurring is True

    def test_ordering_is_stable(self, calendar):
        rules = [_rule(id=3, deadline_type="zeta"), _rule(id=1, deadline_type="alpha")]
        derived = deadlines.evaluate(rules, datetime.date(2026, 9, 7), calendar)
        assert [d.deadline_type for d in derived] == ["alpha", "zeta"]


class TestOverrideResolution:
    """Reference rules ship platform-wide; a community may override individual ones."""

    def test_community_rule_beats_the_platform_default(self):
        default = _rule(id=1, offset_value=10, id_community=None)
        override = _rule(id=2, offset_value=20, id_community=7)
        resolved = deadlines.resolve_rules([default, override])
        assert len(resolved) == 1
        assert resolved[0].offset_value == 20

    def test_override_order_does_not_matter(self):
        default = _rule(id=1, offset_value=10, id_community=None)
        override = _rule(id=2, offset_value=20, id_community=7)
        assert deadlines.resolve_rules([override, default])[0].offset_value == 20

    def test_overriding_one_type_keeps_the_default_for_another(self):
        """The resolution is per deadline_type, not a single global pick."""
        resolved = deadlines.resolve_rules(
            [
                _rule(id=1, deadline_type="completeness_check", offset_value=10),
                _rule(id=2, deadline_type="lapse", offset_value=6),
                _rule(id=3, deadline_type="completeness_check", offset_value=25, id_community=7),
            ]
        )
        by_type = {r.deadline_type: r for r in resolved}
        assert by_type["completeness_check"].offset_value == 25  # overridden
        assert by_type["lapse"].offset_value == 6  # inherited

    def test_ties_at_the_same_specificity_take_the_later_row(self):
        resolved = deadlines.resolve_rules(
            [_rule(id=1, offset_value=10), _rule(id=5, offset_value=99)]
        )
        assert resolved[0].offset_value == 99


class TestRecurrence:
    def test_next_occurrence_is_anchored_on_the_previous_due_date(self, calendar):
        """Anchoring on the due date keeps an annual obligation from drifting."""
        assert deadlines.next_occurrence(datetime.date(2026, 9, 21), 12, calendar) == datetime.date(
            2027, 9, 21
        )

    def test_non_positive_interval_is_rejected(self, calendar):
        with pytest.raises(ValueError, match="must be positive"):
            deadlines.next_occurrence(datetime.date(2026, 9, 21), 0, calendar)
