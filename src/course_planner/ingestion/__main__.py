from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime

import psycopg

from ..planning_data import connect
from ..telemetry import event
from .archive import ARCHIVE_TERMS, collect_archive, read_archive_curriculum
from .audit import ImportStatus, record_import
from .itu import PROGRAM, FetchError, Snapshot, SourceError, collect_snapshot
from .postgres import ImportResult, import_snapshot, initialize_schema


def summary(snapshot: Snapshot, result: ImportResult) -> dict:
    return {
        "program": snapshot.program,
        "plan_id": snapshot.plan_id,
        "term": snapshot.term_label,
        "source_kind": snapshot.source_kind,
        "snapshot_id": result.snapshot_id,
        "created": result.created,
        "active_snapshot_id": result.active_snapshot_id,
        "courses": len(snapshot.courses),
        "requirements": len(snapshot.requirements),
        "prerequisite_records": len(snapshot.rules),
        "unlisted_prerequisites": len(snapshot.courses) - len(snapshot.rules),
        "unresolved_elective_options": sum(len(row.unresolved_options) for row in snapshot.requirements),
        "sections": len(snapshot.sections),
        "eligible_sections": sum(PROGRAM in (section.allowed_programs or ()) for section in snapshot.sections),
        "meetings": sum(len(section.meetings) for section in snapshot.sections),
    }


def classify_failure(error: SourceError | psycopg.Error) -> ImportStatus:
    if isinstance(error, FetchError):
        return "source_unavailable"
    if isinstance(error, SourceError):
        return "invalid_source"
    return "database_error"


def main() -> int:
    parser = argparse.ArgumentParser(description="Import ENDE_LS timetable snapshots into PostgreSQL.")
    parser.add_argument("--archive", action="store_true", help="Import the verified historical timetable exports.")
    args = parser.parse_args()
    started = datetime.now(UTC)
    try:
        with connect() as connection:
            initialize_schema(connection)
            try:
                if args.archive:
                    title, courses = read_archive_curriculum(connection)
                    snapshots = [collect_archive(slug, title, courses) for slug in ARCHIVE_TERMS]
                else:
                    snapshots = [collect_snapshot()]
                with connection.transaction():
                    results = []
                    for snapshot in snapshots:
                        result = import_snapshot(connection, snapshot)
                        record_import(connection, started, "succeeded", result.snapshot_id)
                        results.append(result)
            except (SourceError, psycopg.Error) as error:
                try:
                    record_import(connection, started, classify_failure(error))
                except psycopg.Error as audit_error:
                    event("import_audit_failed", level=logging.ERROR, error_type=type(audit_error).__name__)
                raise
    except (SourceError, psycopg.Error) as error:
        event("import_failed", level=logging.ERROR, program=PROGRAM, status=classify_failure(error),
              error=str(error) if isinstance(error, SourceError) else type(error).__name__)
        return 1

    for snapshot, result in zip(snapshots, results, strict=True):
        payload = summary(snapshot, result)
        event("import_completed", duration_seconds=(datetime.now(UTC) - started).total_seconds(),
              **payload)
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
