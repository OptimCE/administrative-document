"""CWaPE deadlines on Wallonia's calendar (acceptance criterion 1.4).

The rules and the holidays are the plugin's own files; the expected dates were
counted by hand and checked against workalendar.
"""

from __future__ import annotations

import datetime

import pytest

from engine.calendar import CalendarNotCoveredError, WorkingCalendar
from engine.deadlines import due_date
from regulators.cwape.plugin import CwapePlugin

D = datetime.date
PLUGIN = CwapePlugin()
CALENDAR = WorkingCalendar(PLUGIN.calendar())


def _rule(procedure: str, code: str):
    entry = PLUGIN.catalogue().in_force(procedure, D(2026, 11, 2))
    assert entry is not None
    return next(r for r in entry.definition.deadline_rules if r.code == code)


@pytest.mark.parametrize(
    ("procedure", "rule", "anchor", "expected", "why"),
    [
        ("PARTAGE_MODIF", "completeness", D(2026, 4, 30), D(2026, 5, 18), "1 May, Ascension"),
        ("PARTAGE_MODIF", "grd_opinion", D(2026, 5, 13), D(2026, 6, 12), "Ascension, Whit Monday"),
        ("PARTAGE_MODIF", "regulator_decision", D(2026, 6, 4), D(2026, 7, 31), "21 July"),
        ("PARTAGE_MODIF", "grd_control", D(2026, 10, 30), D(2026, 11, 30), "11 November"),
        ("PARTAGE_MODIF", "amendment_return", D(2026, 11, 6), D(2026, 11, 23), "11 November"),
        ("PARTAGE_MODIF", "effect", D(2026, 12, 18), D(2027, 1, 19), "Christmas, New Year"),
        ("PARTAGE_MODIF", "lapse", D(2026, 8, 31), D(2027, 2, 28), "six months, clamped"),
        ("PARTAGE_MODIF", "non_conformity_reply", D(2026, 3, 31), D(2026, 5, 31), "two months"),
        ("PARTAGE_MODIF", "appeal_annulment", D(2026, 12, 15), D(2027, 1, 14), "30 days"),
        ("PARTAGE_FIN", "effect", D(2026, 4, 1), D(2026, 4, 30), "Easter Monday"),
        ("CE_NOTIFICATION", "completeness", D(2026, 4, 30), D(2026, 5, 18), "1 May, Ascension"),
        ("CE_ACTUALISATION", "submission", D(2026, 7, 15), D(2026, 8, 6), "21 July"),
    ],
)
def test_relative_deadlines(procedure, rule, anchor, expected, why):
    assert due_date(_rule(procedure, rule), CALENDAR, anchor=anchor) == expected, why


def test_the_annual_report_is_due_on_1_september_of_its_year():
    assert due_date(_rule("CE_RAPPORT_ANNUEL", "submission"), CALENDAR, period_year=2027) == D(
        2027, 9, 1
    )


def test_a_regulator_decision_cannot_be_counted_past_the_calendar():
    """40 working days from mid-December 2035 leave the file's range: refused,
    not guessed."""
    with pytest.raises(CalendarNotCoveredError, match="covers 2026-2035"):
        due_date(_rule("PARTAGE_MODIF", "regulator_decision"), CALENDAR, anchor=D(2035, 12, 15))
