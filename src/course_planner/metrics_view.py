"""Expose live and persisted planner measurements in Prometheus format."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from prometheus_client import generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .ingestion.itu import PLAN_ID, PROGRAM
from .planning_data import connect
from .telemetry import REGISTRY, event


class ImportMetrics:
    def collect(self):
        available = GaugeMetricFamily(
            "course_planner_database_up", "Whether the planning database could be read."
        )
        age = GaugeMetricFamily(
            "course_planner_published_data_age_seconds", "Seconds since the latest active data was observed."
        )
        attempts = CounterMetricFamily(
            "course_planner_import_runs_total", "Recorded import attempts.", labels=["status"]
        )
        last_success = GaugeMetricFamily(
            "course_planner_last_successful_import_timestamp_seconds",
            "Unix timestamp of the last successful recorded import.",
        )
        try:
            with connect() as connection:
                observed = connection.execute(
                    "SELECT max(observed_at) FROM itu_active_snapshots WHERE program = %s AND plan_id = %s",
                    (PROGRAM, PLAN_ID),
                ).fetchone()[0]
                rows = connection.execute("""
                    SELECT status, count(*), max(finished_at) FROM itu_import_runs GROUP BY status
                """).fetchall()
            available.add_metric([], 1)
            if observed is not None:
                age.add_metric([], max(0, (datetime.now(UTC) - observed).total_seconds()))
            for status, count, finished_at in rows:
                attempts.add_metric([status], count)
                if status == "succeeded":
                    last_success.add_metric([], finished_at.timestamp())
        except psycopg.Error as error:
            available.add_metric([], 0)
            event("metrics_database_unavailable", error_type=type(error).__name__)
        yield available
        yield age
        yield attempts
        yield last_success


REGISTRY.register(ImportMetrics())


def scrape_metrics() -> bytes:
    return generate_latest(REGISTRY)
