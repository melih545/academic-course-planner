from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from uuid import NAMESPACE_URL, uuid5

from .constants import DAY_MAP
from .schedule import LOCAL_TIMEZONE, ConfirmedTerm


def export_calendar(
    confirmed: ConfirmedTerm,
    course_labels: Mapping[str, str],
    first_day: date,
    last_day: date,
) -> bytes:
    if not 0 <= (last_day - first_day).days < 183:
        raise ValueError("Choose a teaching date range of up to 183 days, in date order.")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Course Planner//Timetable//EN",
        "CALSCALE:GREGORIAN",
    ]
    generated = _utc_stamp(datetime.now(UTC))
    for scheduled in confirmed.sections:
        section = scheduled.section
        for meeting in section.meetings:
            weekday = DAY_MAP[meeting.day]
            day = first_day + timedelta(days=(weekday - first_day.weekday()) % 7)
            while day <= last_day:
                midnight = datetime.combine(day, time.min, LOCAL_TIMEZONE)
                start = _utc_stamp(midnight + timedelta(minutes=meeting.start_minute))
                end = _utc_stamp(midnight + timedelta(minutes=meeting.end_minute))
                key = f"{section.code}/{section.id}/{start}/{end}/{meeting.location}"
                uid = uuid5(NAMESPACE_URL, f"academic-course-planner/{key}")
                summary = _escape(f"{section.code} {course_labels.get(section.code, '')}".strip())
                lines.extend([
                    "BEGIN:VEVENT", f"UID:{uid}@course-planner", f"DTSTAMP:{generated}",
                    f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{summary}",
                ])
                if meeting.location:
                    lines.append(f"LOCATION:{_escape(meeting.location)}")
                lines.append("END:VEVENT")
                day += timedelta(days=7)
    if len(lines) == 4:
        raise ValueError("No weekly meetings fall inside the selected teaching dates.")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode("utf-8")


def _utc_stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
        .replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    )


def _fold(line: str) -> str:
    parts = []
    current = []
    width = 0
    for character in line:
        size = len(character.encode("utf-8"))
        if width + size > 75:
            parts.append("".join(current))
            current = [" "]
            width = 1
        current.append(character)
        width += size
    parts.append("".join(current))
    return "\r\n".join(parts)
