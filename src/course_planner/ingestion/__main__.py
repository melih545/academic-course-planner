from __future__ import annotations

import json
import os
import sys

import psycopg

from .itu import PROGRAM, SourceError, collect_snapshot
from .postgres import import_snapshot, initialize_schema


def main() -> int:
    try:
        with psycopg.connect(
            os.environ.get("COURSE_PLANNER_DATABASE_URL", ""), connect_timeout=5
        ) as connection:
            initialize_schema(connection)
            snapshot = collect_snapshot()
            result = import_snapshot(connection, snapshot)
    except (SourceError, psycopg.Error) as error:
        print(f"{PROGRAM} import failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps({
        "program": snapshot.program,
        "plan_id": snapshot.plan_id,
        "term": snapshot.term_label,
        "snapshot_id": result.snapshot_id,
        "created": result.created,
        "active_snapshot_id": result.active_snapshot_id,
        "courses": len(snapshot.courses),
        "requirements": len(snapshot.requirements),
        "listed_prerequisites": len(snapshot.rules),
        "unlisted_prerequisites": len(snapshot.courses) - len(snapshot.rules),
        "unresolved_elective_options": sum(len(row.unresolved_options) for row in snapshot.requirements),
        "sections": len(snapshot.sections),
        "eligible_sections": sum(PROGRAM in (section.allowed_programs or ()) for section in snapshot.sections),
        "meetings": sum(len(section.meetings) for section in snapshot.sections),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
