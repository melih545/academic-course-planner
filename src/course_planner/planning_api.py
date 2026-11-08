"""FastAPI contract shared by the Streamlit client and planning workers."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .curriculum import GRADE_ORDER, matched_requirements, minimum_grades
from .optimization import PlanIssue, generate_schedule_options
from .planning_data import (
    DataUnavailable,
    PlanningData,
    PublishedSnapshot,
    connect,
    load_planning_data,
    verified_curriculum,
)
from .schedule import PlanRequest, ScheduledSection, Term

LOGGER = logging.getLogger(__name__)
CourseCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]+ [0-9]+[A-Z]*$", max_length=24)]
Grade = Literal["DD", "DC", "CC", "CB", "BB", "BA", "AA"]
Evidence = Literal["published", "forecast", "missing"]


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
            self.max_credits, self.preferred_free_day, self.allow_summer,
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


class PlanResponse(BaseModel):
    status: Literal["solved", "infeasible", "time_limit", "interrupted"]
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
    request = payload.to_domain()
    curriculum = verified_curriculum(data.curriculum, payload.english_eligible)
    coverage = []
    offerings = {}
    published = set()
    incomplete_courses = set()
    for term in request.terms():
        evidence, snapshot, sections = data.sections_for(term)
        offerings[term] = sections
        if evidence == "published":
            published.add(term)
        if snapshot:
            incomplete_courses.update(data.incomplete_courses.get(snapshot.term, ()))
        coverage.append(TermCoverage(
            term=term, evidence=evidence,
            snapshot_id=snapshot.id if snapshot else None,
            source_term=snapshot.term if snapshot else None,
            observed_at=snapshot.observed_at if snapshot else None,
        ))
    result = generate_schedule_options(
        (), request, curriculum=curriculum, confirmed_courses=payload.confirmed_courses,
        offerings=offerings, published_terms=published, incomplete_courses=incomplete_courses,
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
