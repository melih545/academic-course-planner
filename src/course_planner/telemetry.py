"""Low-cardinality metrics and structured application events."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()
PLAN_RUNS = Counter(
    "course_planner_plans_total", "Planning runs by outcome.", ("status",), registry=REGISTRY
)
SOLVE_DURATION = Histogram(
    "course_planner_solve_duration_seconds", "Time spent planning a request.",
    registry=REGISTRY, buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
)
LOGGER = logging.getLogger("course_planner.events")


def event(name: str, *, level: int = logging.INFO, **fields: object) -> None:
    if not LOGGER.handlers:
        LOGGER.addHandler(logging.StreamHandler())
        LOGGER.setLevel(logging.INFO)
        LOGGER.propagate = False
    LOGGER.log(level, json.dumps({
        "timestamp": datetime.now(UTC).isoformat(), "event": name, **fields,
    }, default=str))
