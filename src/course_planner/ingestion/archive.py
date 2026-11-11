"""Import pinned historical ÖBS exports from the independent İTÜ course archive."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable
from datetime import datetime
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from psycopg import Connection

from .itu import (
    DAYS,
    EMPTY,
    PLAN_ID,
    PLAN_URL,
    PROGRAM,
    Course,
    FetchError,
    Section,
    Snapshot,
    SourceError,
    _add_unique,
    _count,
    _meetings,
    parse_term,
)

ARCHIVE_ROOT = (
    "https://raw.githubusercontent.com/yatuk/itu-archive/"
    "cd1c3d2157fe0ba210849dd541fb6c3f877cab8a/docs/data/terms"
)
ARCHIVE_TERMS = {
    "2023-2024-bahar": "2023-2024 Bahar Dönemi",
    "2024-2025-bahar": "2024-2025 Bahar Dönemi",
    "2025-2026-guz": "2025-2026 Güz Dönemi",
    "2025-2026-bahar": "2025-2026 Bahar Dönemi",
}
LAB_ARCHIVE_ROOT = (
    "https://raw.githubusercontent.com/keepdying/itu-web-archive/"
    "579fe1f54eabd04c3b0881fcfaf29232ef9f2ddf/public"
)
LAB_ARCHIVE_DATES = {
    "2023-2024-bahar": "2024-04-15",
    "2024-2025-bahar": "2025-05-14",
    "2025-2026-bahar": "2026-04-15",
}
LAB_CODE = "FIZ 102EL"
CSV_COLUMNS = {
    "CRN", "Ders Kodu", "Seviye", "Öğretim Yöntemi", "Öğretim Üyesi", "Bina", "Derslik",
    "Kontenjan", "Yazılan", "Alabilen Programlar", "Gün", "Saat",
}


def fetch_archive(url: str) -> str:
    try:
        request = Request(url, headers={"User-Agent": "AcademicCoursePlanner/1.0"})
        with urlopen(request, timeout=20) as response:
            if response.url != url:
                raise SourceError(f"Unexpected archive redirect: {url}")
            limit = 5_000_000
            body = response.read(limit + 1)
        if len(body) > limit:
            raise SourceError(f"Archive response too large: {url}")
        return body.decode("utf-8-sig")
    except (HTTPError, URLError, OSError) as error:
        raise FetchError(f"Could not read archive: {url}") from error
    except UnicodeError as error:
        raise SourceError(f"Archive response is not UTF-8: {url}") from error


def collect_archive(
    slug: str, plan_title: str, courses: tuple[Course, ...],
    fetch: Callable[[str], str] = fetch_archive,
) -> Snapshot:
    metadata_url = f"{ARCHIVE_ROOT}/{slug}/meta.json"
    sections_url = f"{ARCHIVE_ROOT}/{slug}/all.csv"
    body = fetch(metadata_url)
    try:
        metadata = json.loads(body)
        label = ARCHIVE_TERMS[slug]
        if metadata["slug"] != slug or metadata["source"] != "itu-archive" or metadata["term"] != label:
            raise ValueError("Unexpected archive identity")
        observed_at = datetime.fromisoformat(metadata["scrapedAt"])
        if observed_at.utcoffset() is None:
            raise ValueError("Missing time zone")
        expected_rows = int(metadata["sections"])
        _, year, season = parse_term(json.dumps({"aktifDonem": label}))
    except (KeyError, TypeError, ValueError) as error:
        raise SourceError(f"Invalid archive metadata: {metadata_url}") from error

    rows = csv.DictReader(StringIO(fetch(sections_url).lstrip("\ufeff")), strict=True)
    if not CSV_COLUMNS <= set(rows.fieldnames or ()):
        raise SourceError(f"Missing archive timetable columns: {sections_url}")
    codes = {course.code for course in courses}
    sections: dict[int, Section] = {}
    legacy_code = "ENDE" if slug == "2023-2024-bahar" else PROGRAM
    count = 0
    try:
        for row in rows:
            count += 1
            if None in row or any(row[column] is None for column in CSV_COLUMNS):
                raise SourceError(f"Malformed archive row {count}: {sections_url}")
            code = row["Ders Kodu"]
            if row["Seviye"] != "LS" or code not in codes:
                continue
            programs = set(row["Alabilen Programlar"].replace(",", " ").split())
            if legacy_code not in programs:
                continue
            programs.remove(legacy_code)
            programs.add(PROGRAM)
            crn = _count(row["CRN"])
            if not crn:
                raise SourceError(f"Missing CRN for {code}: {sections_url}")
            days = row["Gün"].replace("|", " ").strip()
            times = row["Saat"].replace("|", " ").strip()
            if days in EMPTY and times == "/":
                times = ""
            buildings = _locations(row["Bina"])
            rooms = _locations(row["Derslik"])
            section = Section(
                crn, code, row["Öğretim Yöntemi"], row["Öğretim Üyesi"], buildings, rooms,
                _count(row["Kontenjan"]), _count(row["Yazılan"]) if slug != "2025-2026-guz" else None,
                tuple(sorted(programs)), _meetings(days, times, crn), sections_url,
            )
            _add_unique(sections, crn, section, "archive section CRN")
    except csv.Error as error:
        raise SourceError(f"Invalid archive CSV: {sections_url}") from error
    if count != expected_rows or not any(section.meetings and section.quota for section in sections.values()):
        raise SourceError(f"Incomplete archive export for {slug}")

    sources = [PLAN_URL, metadata_url, sections_url]
    if slug in LAB_ARCHIVE_DATES and LAB_CODE in codes:
        lab_url = f"{LAB_ARCHIVE_ROOT}/{LAB_ARCHIVE_DATES[slug]}/FIZ.csv"
        for section in _lab_sections(fetch(lab_url), legacy_code, lab_url):
            if section.crn not in sections:
                sections[section.crn] = section
            elif sections[section.crn].course_code != section.course_code:
                raise SourceError(f"Conflicting archive course for CRN {section.crn}: {lab_url}")
        sources.append(lab_url)
    imported_codes = {section.course_code for section in sections.values()}
    return Snapshot(
        PROGRAM, PLAN_ID, plan_title, label, year, season, observed_at,
        tuple(course for course in courses if course.code in imported_codes), (), (),
        tuple(sections[crn] for crn in sorted(sections)), tuple(sources), "archive",
    )


def read_archive_curriculum(connection: Connection) -> tuple[str, tuple[Course, ...]]:
    with connection.transaction():
        row = connection.execute(
            """
            SELECT s.id, s.plan_title FROM itu_active_snapshots a
            JOIN itu_snapshots s ON s.id = a.snapshot_id
            WHERE a.program = %s AND a.plan_id = %s AND s.source_kind = 'published'
            ORDER BY a.term_year DESC, CASE a.season WHEN 'Fall' THEN 3 WHEN 'Summer' THEN 2 ELSE 1 END DESC
            LIMIT 1
            """, (PROGRAM, PLAN_ID),
        ).fetchone()
        if row is None:
            raise SourceError("Import the active ÖBS term before importing archived sections.")
        snapshot_id, title = row
        courses = connection.execute(
            "SELECT code, title, language, credits, ects FROM itu_courses WHERE snapshot_id = %s ORDER BY code",
            (snapshot_id,),
        ).fetchall()
    return title, tuple(Course(*course) for course in courses)


def _locations(value: str) -> str:
    return " / ".join(dict.fromkeys(item for part in value.split("|") if (item := part.strip()) not in EMPTY))


def _lab_sections(body: str, legacy_code: str, url: str) -> tuple[Section, ...]:
    rows = csv.DictReader(StringIO(body.lstrip("\ufeff")), strict=True)
    required = {"CRN", "Course Code", "Major Restriction", "Day", "Time", "Capacity", "Enrolled",
                "Teaching Method", "Instructor", "Building", "Room"}
    if not required <= set(rows.fieldnames or ()):
        raise SourceError(f"Missing lab archive columns: {url}")
    english_days = dict(zip(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"), DAYS))
    sections = {}
    try:
        for row in rows:
            if row["Course Code"] != LAB_CODE:
                continue
            programs = set(re.split(r"[,;\s]+", row["Major Restriction"].strip()))
            if legacy_code not in programs:
                continue
            programs.remove(legacy_code)
            programs.add(PROGRAM)
            crn = _count(row["CRN"])
            if not crn:
                raise SourceError(f"Missing lab CRN: {url}")
            days = " ".join(english_days.get(day, day) for day in row["Day"].split())
            times = re.sub(r"\b(\d{2})(\d{2})\b", r"\1:\2", row["Time"])
            section = Section(
                crn, LAB_CODE, row["Teaching Method"], row["Instructor"], row["Building"], row["Room"],
                _count(row["Capacity"]), _count(row["Enrolled"]), tuple(sorted(programs)),
                _meetings(days, times, crn), url,
            )
            _add_unique(sections, crn, section, "lab archive CRN")
    except (TypeError, csv.Error) as error:
        raise SourceError(f"Malformed lab archive: {url}") from error
    if not any(section.meetings and section.quota for section in sections.values()):
        raise SourceError(f"No eligible lab sections in {url}")
    return tuple(sections.values())
