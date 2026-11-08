"""Persist immutable source snapshots and atomically activate validated imports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from importlib.resources import files

from psycopg import Connection, Cursor

from .itu import Snapshot


@dataclass(frozen=True)
class ImportResult:
    snapshot_id: int
    created: bool
    active_snapshot_id: int


def initialize_schema(connection: Connection) -> None:
    schema = files("course_planner.ingestion").joinpath("schema.sql").read_text(encoding="utf-8")
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(328423051894263)")
        connection.execute(schema)


def import_snapshot(connection: Connection, snapshot: Snapshot) -> ImportResult:
    payload = asdict(snapshot)
    del payload["observed_at"]
    digest = hashlib.sha256(
        json.dumps(payload, default=str, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    key = (snapshot.program, snapshot.plan_id, snapshot.term_year, snapshot.season)
    with connection.transaction(), connection.cursor() as cursor:
        inserted = cursor.execute(
            """
            INSERT INTO itu_snapshots (
                program, plan_id, term_year, season, plan_title, term_label,
                content_sha256, observed_at, source_urls
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (content_sha256) DO NOTHING
            RETURNING id
            """,
            (*key, snapshot.plan_title, snapshot.term_label, digest, snapshot.observed_at,
             list(snapshot.source_urls)),
        ).fetchone()
        if inserted is None:
            (snapshot_id,) = cursor.execute(
                "SELECT id FROM itu_snapshots WHERE content_sha256 = %s", (digest,)
            ).fetchone()
        else:
            (snapshot_id,) = inserted
            _write_snapshot(cursor, snapshot_id, snapshot)

        cursor.execute(
            """
            INSERT INTO itu_active_snapshots AS active (
                program, plan_id, term_year, season, snapshot_id, observed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (program, plan_id, term_year, season) DO UPDATE
            SET snapshot_id = EXCLUDED.snapshot_id, observed_at = EXCLUDED.observed_at
            WHERE EXCLUDED.observed_at >= active.observed_at
            """,
            (*key, snapshot_id, snapshot.observed_at),
        )
        (active_id,) = cursor.execute(
            """
            SELECT snapshot_id FROM itu_active_snapshots
            WHERE (program, plan_id, term_year, season) = (%s, %s, %s, %s)
            """,
            key,
        ).fetchone()
    return ImportResult(snapshot_id, inserted is not None, active_id)


def _write_snapshot(cursor: Cursor, snapshot_id: int, snapshot: Snapshot) -> None:
    cursor.executemany(
        """
        INSERT INTO itu_courses (snapshot_id, code, title, language, credits, ects)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            (snapshot_id, course.code, course.title, course.language, course.credits, course.ects)
            for course in snapshot.courses
        ),
    )
    cursor.executemany(
        """
        INSERT INTO itu_requirements (
            snapshot_id, semester, position, category, title, elective_group, unresolved_options
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            (snapshot_id, item.semester, item.position, item.category, item.title,
             item.elective_group, list(item.unresolved_options))
            for item in snapshot.requirements
        ),
    )
    cursor.executemany(
        """
        INSERT INTO itu_requirement_courses (snapshot_id, semester, position, course_code)
        VALUES (%s, %s, %s, %s)
        """,
        (
            (snapshot_id, item.semester, item.position, code)
            for item in snapshot.requirements for code in item.course_codes
        ),
    )
    cursor.executemany(
        """
        INSERT INTO itu_prerequisites (snapshot_id, course_code, course_rule, standing_rule, source_url)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            (snapshot_id, rule.code, rule.course_rule, rule.standing_rule, rule.source_url)
            for rule in snapshot.rules
        ),
    )
    cursor.executemany(
        """
        INSERT INTO itu_sections (
            snapshot_id, crn, course_code, instruction, instructor, building, room, quota,
            enrolled, allowed_programs, source_url
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            (
                snapshot_id, section.crn, section.course_code, section.instruction,
                section.instructor, section.building, section.room, section.quota, section.enrolled,
                None if section.allowed_programs is None else list(section.allowed_programs),
                section.source_url,
            )
            for section in snapshot.sections
        ),
    )
    cursor.executemany(
        """
        INSERT INTO itu_meetings (snapshot_id, crn, weekday, start_minute, end_minute)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            (snapshot_id, section.crn, meeting.weekday, meeting.start_minute, meeting.end_minute)
            for section in snapshot.sections for meeting in section.meetings
        ),
    )
