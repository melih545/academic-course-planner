"""Read a consistent set of immutable ÖBS snapshots for planning."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import psycopg

from .curriculum import (
    Course,
    Curriculum,
    RequirementGroup,
    UnresolvedPrerequisite,
    parse_prerequisite,
)
from .ingestion.itu import DAYS, ING100_ELIGIBILITY, PLAN_ID, PROGRAM
from .schedule import Meeting, Section, Term


class DataUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class PublishedSnapshot:
    id: int
    term: Term
    observed_at: datetime
    source_urls: tuple[str, ...]


@dataclass
class PlanningData:
    snapshots: tuple[PublishedSnapshot, ...]
    curriculum: Curriculum
    labels: dict[str, str]
    sections: dict[Term, tuple[Section, ...]]
    incomplete_courses: dict[Term, set[str]]

    def sections_for(self, term: Term) -> tuple[str, PublishedSnapshot | None, tuple[Section, ...]]:
        if term in self.sections:
            snapshot = next(item for item in self.snapshots if item.term == term)
            return "published", snapshot, self.sections[term]
        if term.position > self.snapshots[-1].term.position:
            for snapshot in reversed(self.snapshots):
                if snapshot.term.season == term.season:
                    return "forecast", snapshot, self.sections[snapshot.term]
        return "missing", None, ()


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("COURSE_PLANNER_DATABASE_URL", ""), connect_timeout=5)


def load_planning_data(
    connection: psycopg.Connection, snapshot_ids: tuple[int, ...] = ()
) -> PlanningData:
    with connection.transaction():
        if snapshot_ids:
            rows = connection.execute(
                """
                SELECT id, term_year, season, observed_at, source_urls FROM itu_snapshots
                WHERE program = %s AND plan_id = %s AND id = ANY(%s)
                """, (PROGRAM, PLAN_ID, list(snapshot_ids)),
            ).fetchall()
            if len(rows) != len(snapshot_ids):
                raise DataUnavailable("One or more requested snapshots are unavailable.")
        else:
            rows = connection.execute(
                """
                SELECT s.id, s.term_year, s.season, a.observed_at, s.source_urls
                FROM itu_active_snapshots a JOIN itu_snapshots s ON s.id = a.snapshot_id
                WHERE a.program = %s AND a.plan_id = %s
                """, (PROGRAM, PLAN_ID),
            ).fetchall()
        snapshots = tuple(sorted(
            (PublishedSnapshot(row[0], Term(row[1], row[2]), row[3], tuple(row[4])) for row in rows),
            key=lambda item: item.term.position,
        ))
        if not snapshots:
            raise DataUnavailable("No ENDE_LS snapshot has been imported yet.")
        if len({item.term for item in snapshots}) != len(snapshots):
            raise DataUnavailable("Use only one snapshot per published term.")
        latest = snapshots[-1].id
        requirements = connection.execute(
            """
            SELECT r.title, r.elective_group, r.unresolved_options,
                   array_agg(rc.course_code ORDER BY rc.course_code)
            FROM itu_requirements r JOIN itu_requirement_courses rc
              USING (snapshot_id, semester, position)
            WHERE r.snapshot_id = %s
            GROUP BY r.snapshot_id, r.semester, r.position ORDER BY r.semester, r.position
            """, (latest,),
        ).fetchall()
        course_rows = connection.execute(
            """
            SELECT c.code, c.title, c.credits, p.course_rule, p.standing_rule
            FROM itu_courses c LEFT JOIN itu_prerequisites p
              ON (p.snapshot_id, p.course_code) = (c.snapshot_id, c.code)
            WHERE c.snapshot_id = %s ORDER BY c.code
            """, (latest,),
        ).fetchall()
        ids = [item.id for item in snapshots]
        section_rows = connection.execute(
            """
            SELECT snapshot_id, crn, course_code, instructor, building, room, source_url,
                   allowed_programs, quota
            FROM itu_sections WHERE snapshot_id = ANY(%s) ORDER BY snapshot_id, crn
            """, (ids,),
        ).fetchall()
        meeting_rows = connection.execute(
            """
            SELECT snapshot_id, crn, weekday, start_minute, end_minute
            FROM itu_meetings WHERE snapshot_id = ANY(%s)
            ORDER BY snapshot_id, crn, weekday, start_minute, end_minute
            """, (ids,),
        ).fetchall()

    groups = tuple(RequirementGroup(title, tuple(codes), bool(unresolved)) for title, _, unresolved, codes in requirements)
    required = {code for _, group, _, codes in requirements if group is None for code in codes}
    courses = {}
    labels = {}
    for code, title, credits, rule_text, standing in course_rows:
        if rule_text is None:
            text = "No prerequisite evidence in the imported sources."
            rule = UnresolvedPrerequisite(text)
        elif standing and standing.strip() not in {"-", "Yok"}:
            text = " / ".join(part for part in (rule_text, standing) if part)
            rule = UnresolvedPrerequisite(text)
        else:
            text = rule_text
            rule = parse_prerequisite(rule_text)
        courses[code] = Course("Main" if code in required else "Elective", rule, text, float(credits))
        labels[code] = title
    if not groups or not courses:
        raise DataUnavailable("The selected snapshot has no usable curriculum.")

    slots = defaultdict(list)
    for snapshot_id, crn, weekday, start, end in meeting_rows:
        slots[snapshot_id, crn].append((weekday, start, end))
    term_by_id = {item.id: item.term for item in snapshots}
    sections = {item.term: [] for item in snapshots}
    incomplete = {item.term: set() for item in snapshots}
    for section_id, row in enumerate(section_rows, start=1):
        snapshot_id, crn, code, instructor, building, room, source_url, programs, quota = row
        term = term_by_id[snapshot_id]
        if programs is None or (PROGRAM in programs and (quota is None or (quota > 0 and not slots[snapshot_id, crn]))):
            incomplete[term].add(code)
            continue
        if PROGRAM not in programs or not quota:
            continue
        location = " ".join(value for value in (building, room) if value not in {"", "-", "--"})
        meetings = tuple(
            Meeting(DAYS[weekday], start, end, location)
            for weekday, start, end in slots[snapshot_id, crn]
        )
        sections[term].append(Section(section_id, code, term.season, meetings, instructor, term, crn, source_url))

    return PlanningData(snapshots, Curriculum(courses, {}, groups), labels, {
        term: tuple(items) for term, items in sections.items()
    }, incomplete)


def verified_curriculum(curriculum: Curriculum, english_eligible: bool) -> Curriculum:
    if not english_eligible or "ING 100" not in curriculum.courses:
        return curriculum
    course = curriculum.courses["ING 100"]
    if course.prerequisite_text != ING100_ELIGIBILITY:
        return curriculum
    return Curriculum(
        {**curriculum.courses, "ING 100": Course(course.category, None, course.prerequisite_text, course.credits)},
        curriculum.elective_minimums, curriculum.requirement_groups,
    )
