from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import psycopg

from ..planning_data import connect
from ..telemetry import event
from .audit import ImportStatus, record_import
from .itu import PROGRAM, FetchError, Snapshot, SourceError, collect_snapshot
from .postgres import ImportResult, import_snapshot, initialize_schema


def summary(snapshot: Snapshot, result: ImportResult) -> dict:
    return {
        "program": snapshot.program,
        "plan_id": snapshot.plan_id,
        "term": snapshot.term_label,
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
    started = datetime.now(UTC)
    try:
        with connect() as connection:
            initialize_schema(connection)
            try:
                snapshot = collect_snapshot()
                with connection.transaction():
                    result = import_snapshot(connection, snapshot)
                    record_import(connection, started, "succeeded", result.snapshot_id)
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

    payload = summary(snapshot, result)
    event("import_completed", duration_seconds=(datetime.now(UTC) - started).total_seconds(),
          **payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
