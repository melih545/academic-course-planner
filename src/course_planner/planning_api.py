"""FastAPI contract shared by the Streamlit client and planning workers."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import FastAPI, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .curriculum import GRADE_ORDER, matched_requirements, minimum_grades
from .jobs.store import (
    IdempotencyConflict,
    Job,
    enqueue,
    existing_job,
    get_job,
    initialize_job_schema,
    request_digest,
)
from .metrics_view import scrape_metrics
from .optimization import PlanIssue, generate_schedule_options
from .planning_data import (
    DataUnavailable,
    Evidence,
    PlanningData,
    PublishedSnapshot,
    connect,
    load_planning_data,
    verified_curriculum,
)
from .schedule import PlanRequest, ScheduledSection, Term
from .telemetry import PLAN_RUNS, SOLVE_DURATION, event

LOGGER = logging.getLogger(__name__)
CourseCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]+ [0-9]+[A-Z]*$", max_length=24)]
Grade = Literal["DD", "DC", "CC", "CB", "BB", "BA", "AA"]


class TermInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=1900, le=2100)
    season: Literal["Spring", "Summer", "Fall"]

    def to_domain(self) -> Term:
        return Term(self.year, self.season)


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: TermInput
    end: TermInput
    completed_grades: dict[CourseCode, Grade] = Field(default_factory=dict, max_length=500)
    confirmed_courses: set[CourseCode] = Field(default_factory=set, max_length=500)
    snapshot_ids: tuple[Annotated[int, Field(gt=0)], ...] = Field(default=(), max_length=30)
    max_credits: int | None = Field(default=None, ge=1, le=100)
    preferred_free_day: str | None = None
    allow_summer: bool = True
    english_eligible: bool = False
    planning_goal: Literal["graduation", "term"] = "graduation"
    allow_partial: bool = False

    @model_validator(mode="after")
    def validate_window(self) -> PlanInput:
        self.to_domain()
        if self.end.year - self.start.year > 8:
            raise ValueError("Choose a planning window of at most nine calendar years.")
        if len(set(self.snapshot_ids)) != len(self.snapshot_ids):
            raise ValueError("Snapshot IDs must be unique.")
        return self

    def to_domain(self) -> PlanRequest:
        return PlanRequest(
            self.start.to_domain(), self.end.to_domain(), self.completed_grades,
            self.max_credits, self.preferred_free_day, self.allow_summer, self.planning_goal,
        )


class Catalogue(BaseModel):
    snapshots: tuple[PublishedSnapshot, ...]
    course_labels: dict[str, str]
    elective_codes: set[str]
    higher_grades: dict[str, list[str]]


class TermCoverage(BaseModel):
    term: Term
    evidence: Evidence
    snapshot_id: int | None = None
    source_term: Term | None = None
    observed_at: datetime | None = None
    source_kind: Literal["published", "archive"] | None = None


class PlanResponse(BaseModel):
    status: Literal["solved", "partial", "infeasible", "time_limit", "interrupted"]
    options: list[list[ScheduledSection]]
    issues: tuple[PlanIssue, ...]
    coverage: list[TermCoverage]
    snapshot_ids: tuple[int, ...]
    required_count: int
    completed_count: int
    confirmed_count: int


def read_dataset(snapshot_ids: tuple[int, ...] = ()) -> PlanningData:
    try:
        with connect() as connection:
            return load_planning_data(connection, snapshot_ids)
    except (DataUnavailable, psycopg.errors.UndefinedTable) as error:
        message = str(error) if isinstance(error, DataUnavailable) else "Import an ENDE_LS snapshot first."
        raise HTTPException(503, detail={"code": "data_missing", "message": message}) from error
    except psycopg.Error as error:
        LOGGER.exception("Failed to read planning data")
        raise HTTPException(503, detail={
            "code": "data_unavailable", "message": "The planning database is unavailable."
        }) from error


def solve_plan(data: PlanningData, payload: PlanInput) -> PlanResponse:
    started = monotonic()
    try:
        result = _solve_plan(data, payload)
    except Exception:
        PLAN_RUNS.labels("error").inc()
        event("plan_failed", level=logging.ERROR)
        raise
    finally:
        SOLVE_DURATION.observe(monotonic() - started)
    PLAN_RUNS.labels(result.status).inc()
    event("plan_completed", status=result.status, duration_seconds=round(monotonic() - started, 4),
          snapshots=result.snapshot_ids, options=len(result.options))
    return result


def _solve_plan(data: PlanningData, payload: PlanInput) -> PlanResponse:
    request = payload.to_domain()
    curriculum = verified_curriculum(data.curriculum, payload.english_eligible)
    coverage = []
    offerings = {}
    published = set()
    incomplete_courses = set()
    for term in request.terms():
        evidence, snapshot, sections = data.sections_for(term, published_only=request.planning_goal == "term")
        offerings[term] = (
            tuple(section for section in sections if not section.advisor_arranged)
            if request.planning_goal == "term" else sections
        )
        if evidence == "published":
            published.add(term)
        if snapshot:
            incomplete_courses.update(data.incomplete_courses.get(snapshot.term, ()))
        coverage.append(TermCoverage(
            term=term, evidence=evidence,
            snapshot_id=snapshot.id if snapshot else None,
            source_term=snapshot.term if snapshot else None,
            observed_at=snapshot.observed_at if snapshot else None,
            source_kind=snapshot.source_kind if snapshot else None,
        ))
    result = generate_schedule_options(
        (), request, curriculum=curriculum, confirmed_courses=payload.confirmed_courses,
        offerings=offerings, published_terms=published, incomplete_courses=incomplete_courses,
        allow_partial=payload.allow_partial,
    )
    groups = curriculum.requirement_groups
    completed = set(payload.completed_grades)
    return PlanResponse(
        status=result.status, options=result.options,
        issues=result.diagnostics or tuple(PlanIssue("constraints", issue) for issue in result.issues),
        coverage=coverage, snapshot_ids=tuple(snapshot.id for snapshot in data.snapshots),
        required_count=len(groups), completed_count=len(matched_requirements(groups, completed)),
        confirmed_count=len(matched_requirements(groups, completed | payload.confirmed_courses)),
    )


app = FastAPI(title="Course Planner API", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(scrape_metrics(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@app.get("/v1/catalogue", response_model=Catalogue)
def catalogue() -> Catalogue:
    data = read_dataset()
    grades = {item for course in data.curriculum.courses.values() for item in minimum_grades(course.prerequisite)}
    labels = {**{item.code: "External prerequisite" for item in grades}, **data.labels}
    return Catalogue(
        snapshots=data.snapshots, course_labels=labels,
        elective_codes={code for code, course in data.curriculum.courses.items() if course.category == "Elective"},
        higher_grades={
            grade: sorted({item.code for item in grades if item.grade == grade})
            for grade in GRADE_ORDER[GRADE_ORDER.index("DD") + 1:]
            if any(item.grade == grade for item in grades)
        },
    )


@app.post("/v1/plans", response_model=PlanResponse)
def create_plan(payload: PlanInput) -> PlanResponse:
    return solve_plan(read_dataset(payload.snapshot_ids), payload)


JobKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._:-]+$")
]


@contextmanager
def job_connection(*, initialize: bool = False):
    try:
        with connect() as connection:
            if initialize:
                initialize_job_schema(connection)
            yield connection
    except IdempotencyConflict as error:
        raise HTTPException(409, detail={"code": "idempotency_conflict", "message": str(error)}) from error
    except psycopg.Error as error:
        event("job_database_unavailable", error_type=type(error).__name__)
        raise HTTPException(503, detail={
            "code": "job_store_unavailable", "message": "The job store is unavailable."
        }) from error


@app.post("/v1/jobs/plans", status_code=202, response_model=Job)
def queue_plan(payload: PlanInput, response: Response, key: JobKey) -> Job:
    original = payload.model_dump(mode="json")
    original["confirmed_courses"] = sorted(original["confirmed_courses"])
    original["snapshot_ids"] = sorted(original["snapshot_ids"])
    digest = request_digest(original)
    with job_connection(initialize=True) as connection:
        job = existing_job(connection, "plan", key, digest)
        if job is None:
            data = read_dataset(payload.snapshot_ids)
            pinned = {**original, "snapshot_ids": [snapshot.id for snapshot in data.snapshots]}
            job = enqueue(connection, "plan", key, digest, pinned)
    response.headers["Location"] = f"/v1/jobs/{job.id}"
    return job


@app.post("/v1/jobs/imports", status_code=202, response_model=Job)
def queue_import(response: Response, key: JobKey) -> Job:
    with job_connection(initialize=True) as connection:
        job = enqueue(connection, "import", key, request_digest({}), {})
    response.headers["Location"] = f"/v1/jobs/{job.id}"
    return job


@app.get("/v1/jobs/{job_id}", response_model=Job)
def job_status(job_id: UUID) -> Job:
    with job_connection() as connection:
        job = get_job(connection, job_id)
    if job is None:
        raise HTTPException(404, detail={"code": "job_not_found", "message": "Job not found."})
    return job
