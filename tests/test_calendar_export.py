from datetime import date

import pytest

from course_planner.calendar_export import export_calendar
from course_planner.schedule import (
    ConfirmedTerm,
    Meeting,
    ScheduledSection,
    Section,
    Term,
)


def test_calendar_exports_weekly_meetings_in_istanbul_time():
    term = Term(2026, "Fall")
    section = Section(1, "END 101E", "Fall", (
        Meeting("Pazartesi", 9 * 60, 10 * 60, "Room 1; East"),
        Meeting("Çarşamba", 12 * 60, 13 * 60),
    ))
    confirmed = ConfirmedTerm(term, (ScheduledSection(section, term),))
    labels = {"END 101E": "Ç" * 50 + ", planning"}
    first, last = date(2026, 9, 7), date(2026, 9, 14)

    raw = export_calendar(confirmed, labels, first, last)

    assert all(len(line) <= 75 for line in raw.split(b"\r\n"))
    lines = raw.decode().replace("\r\n ", "").splitlines()
    assert [line for line in lines if line.startswith("DTSTART:")] == [
        "DTSTART:20260907T060000Z", "DTSTART:20260914T060000Z", "DTSTART:20260909T090000Z",
    ]
    assert "LOCATION:Room 1\\; East" in lines
    assert f"SUMMARY:END 101E {'Ç' * 50}\\, planning" in lines
    uids = [line for line in lines if line.startswith("UID:")]
    assert len(set(uids)) == 3
    assert all(uid in export_calendar(confirmed, labels, first, last).decode() for uid in uids)

    with pytest.raises(ValueError, match="date order"):
        export_calendar(confirmed, labels, last, first)
    with pytest.raises(ValueError, match="No weekly meetings"):
        export_calendar(confirmed, labels, date(2026, 9, 13), date(2026, 9, 13))
