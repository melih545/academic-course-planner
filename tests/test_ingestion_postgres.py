from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest

from course_planner.ingestion import itu
from course_planner.ingestion.postgres import import_snapshot


def test_repeated_import_keeps_one_snapshot_and_one_copy_of_every_row(database, snapshot):
    first = import_snapshot(database, snapshot)
    second = import_snapshot(
        database, replace(snapshot, observed_at=snapshot.observed_at + timedelta(minutes=10))
    )

    assert first.created
    assert not second.created
    assert first.snapshot_id == second.snapshot_id == second.active_snapshot_id
    counts = database.execute("""
        SELECT (SELECT count(*) FROM itu_snapshots),
               (SELECT count(*) FROM itu_active_snapshots),
               (SELECT count(*) FROM itu_courses),
               (SELECT count(*) FROM itu_requirements),
               (SELECT count(*) FROM itu_requirement_courses),
               (SELECT count(*) FROM itu_prerequisites),
               (SELECT count(*) FROM itu_sections),
               (SELECT count(*) FROM itu_meetings)
    """).fetchone()
    assert counts == (1, 1, 1, 1, 1, 1, 1, 2)


def test_invalid_snapshot_is_rolled_back_and_cannot_replace_active_data(database, snapshot):
    first = import_snapshot(database, snapshot)
    bad_section = replace(snapshot.sections[0], meetings=(itu.Meeting(8, 510, 569),))
    invalid = replace(
        snapshot, sections=(bad_section,), observed_at=snapshot.observed_at + timedelta(minutes=1)
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        import_snapshot(database, invalid)

    assert database.execute("SELECT id FROM itu_snapshots").fetchall() == [(first.snapshot_id,)]
    assert database.execute("SELECT snapshot_id FROM itu_active_snapshots").fetchone() == (
        first.snapshot_id,
    )
    assert database.execute("SELECT count(*) FROM itu_meetings").fetchone() == (2,)


def test_changed_data_is_versioned_and_an_older_collection_cannot_supersede_it(database, snapshot):
    first = import_snapshot(database, snapshot)
    updated = replace(
        snapshot,
        sections=(replace(snapshot.sections[0], enrolled=27),),
        observed_at=snapshot.observed_at + timedelta(minutes=2),
    )

    second = import_snapshot(database, updated)
    old = import_snapshot(
        database, replace(snapshot, observed_at=snapshot.observed_at + timedelta(minutes=1))
    )

    assert second.created
    assert second.snapshot_id != first.snapshot_id
    assert old.active_snapshot_id == second.snapshot_id
    assert database.execute(
        "SELECT enrolled FROM itu_sections ORDER BY snapshot_id"
    ).fetchall() == [(25,), (27,)]


def test_api_plans_against_a_pinned_imported_snapshot(database, snapshot, monkeypatch):
    from fastapi.testclient import TestClient

    from course_planner import planning_api
    from course_planner.planning_data import load_planning_data

    first = import_snapshot(database, snapshot)
    updated = replace(
        snapshot, sections=(), observed_at=snapshot.observed_at + timedelta(minutes=1)
    )
    second = import_snapshot(database, updated)
    monkeypatch.setattr(planning_api, "read_dataset", lambda ids=(): load_planning_data(database, ids))
    term = {"year": 2026, "season": "Fall"}
    payload = {"start": term, "end": term, "completed_grades": {"BIL 100E": "DD"}}

    with TestClient(planning_api.app) as client:
        assert client.get("/v1/catalogue").json()["snapshots"][0]["id"] == second.snapshot_id
        current = client.post("/v1/plans", json=payload).json()
        assert current["issues"][0]["code"] == "section_not_offered"
        pinned = client.post("/v1/plans", json={**payload, "snapshot_ids": [first.snapshot_id]}).json()
        assert pinned["snapshot_ids"] == [first.snapshot_id]
        assert pinned["options"][0][0]["section"]["crn"] == 12345


def test_prometheus_reports_import_outcomes_and_data_age(database, snapshot, monkeypatch):
    from contextlib import contextmanager

    from course_planner import metrics_view
    from course_planner.ingestion.audit import record_import

    @contextmanager
    def connection():
        yield database

    imported = import_snapshot(database, snapshot)
    record_import(database, snapshot.observed_at, "succeeded", imported.snapshot_id)
    record_import(database, snapshot.observed_at, "invalid_source")
    monkeypatch.setattr(metrics_view, "connect", connection)

    output = metrics_view.scrape_metrics().decode()

    assert 'course_planner_import_runs_total{status="succeeded"} 1.0' in output
    assert 'course_planner_import_runs_total{status="invalid_source"} 1.0' in output
    assert "course_planner_published_data_age_seconds " in output
    assert "course_planner_database_up 1.0" in output
