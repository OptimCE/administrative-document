"""The Belgian public holidays of 2026 and 2027, written out by hand.

Test data for the engine's acceptance criterion 1.4 ("deadlines are exact on a
set that includes Belgian public holidays"). Hand-written so the expected dates
in the tests do not come from the code under test; ``test_calendar.py`` checks
this list against workalendar, an independent computation.
"""

from __future__ import annotations

from engine.calendar import CalendarData, WorkingCalendar

BELGIAN_HOLIDAYS = {
    2026: [
        ("2026-01-01", "Nouvel An"),
        ("2026-04-06", "Lundi de Pâques"),
        ("2026-05-01", "Fête du Travail"),
        ("2026-05-14", "Ascension"),
        ("2026-05-25", "Lundi de Pentecôte"),
        ("2026-07-21", "Fête nationale"),
        ("2026-08-15", "Assomption"),  # a Saturday
        ("2026-11-01", "Toussaint"),  # a Sunday
        ("2026-11-11", "Armistice"),
        ("2026-12-25", "Noël"),
    ],
    2027: [
        ("2027-01-01", "Nouvel An"),
        ("2027-03-29", "Lundi de Pâques"),
        ("2027-05-01", "Fête du Travail"),
        ("2027-05-06", "Ascension"),
        ("2027-05-17", "Lundi de Pentecôte"),
        ("2027-07-21", "Fête nationale"),
        ("2027-08-15", "Assomption"),
        ("2027-11-01", "Toussaint"),
        ("2027-11-11", "Armistice"),
        ("2027-12-25", "Noël"),
    ],
}


def belgian_calendar_data(*, saturday_is_working: bool = False) -> CalendarData:
    return CalendarData.model_validate(
        {
            "region": "BETEST",
            "saturday_is_working": saturday_is_working,
            "years": {"from": 2026, "to": 2027},
            "holidays": [
                {"day": day, "label": label}
                for holidays in BELGIAN_HOLIDAYS.values()
                for day, label in holidays
            ],
        }
    )


def belgian_calendar(*, saturday_is_working: bool = False) -> WorkingCalendar:
    return WorkingCalendar(belgian_calendar_data(saturday_is_working=saturday_is_working))
