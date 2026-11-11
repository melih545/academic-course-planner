from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from .constants import DAY_MAP, SEMESTER_LST

LOCAL_TIMEZONE = ZoneInfo("Europe/Istanbul")
PlanningGoal = Literal["graduation", "term"]


def current_planning_term(today: date | None = None) -> Term:
    today = today or datetime.now(LOCAL_TIMEZONE).date()
    season = "Spring" if today.month <= 6 else "Summer" if today.month <= 8 else "Fall"
    return Term(today.year, season)


@dataclass(frozen=True)
class Term:
    year: int
    season: str

    @property
    def position(self) -> tuple[int, int]:
        return self.year, SEMESTER_LST.index(self.season)

    def next(self) -> Term:
        year, index = self.position
        next_index = (index + 1) % len(SEMESTER_LST)
        return Term(year + (next_index == 0), SEMESTER_LST[next_index])

    def __str__(self) -> str:
        return f"{self.year} {self.season}"


@dataclass(frozen=True)
class Meeting:
    day: str
    start_minute: int
    end_minute: int
    location: str = ""


@dataclass(frozen=True)
class Section:
    id: int
    code: str
    season: str
    meetings: tuple[Meeting, ...]
    instructor: str = ""
    published_term: Term | None = None
    crn: int | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class ScheduledSection:
    section: Section
    term: Term

    @property
    def code(self) -> str:
        return self.section.code


@dataclass(frozen=True)
class PlanRequest:
    start: Term
    end: Term
    completed_grades: Mapping[str, str]
    max_credits: int | None = None
    preferred_free_day: str | None = None
    allow_summer: bool = True
    planning_goal: PlanningGoal = "graduation"

    def __post_init__(self) -> None:
        if self.planning_goal == "term" and self.start != self.end:
            raise ValueError("A term schedule must start and end in the same term.")
        if self.end.position < self.start.position:
            raise ValueError("The finish semester must not be earlier than the current semester.")
        if not self.allow_summer and self.start == self.end and self.start.season == "Summer":
            raise ValueError("The window must include a non-summer term when summer is disabled.")
        if self.max_credits is not None and self.max_credits < 1:
            raise ValueError("The credit limit must be positive.")
        if self.preferred_free_day is not None and self.preferred_free_day not in DAY_MAP:
            raise ValueError("Choose a valid preferred free day.")

    def terms(self) -> list[Term]:
        terms = [self.start]
        while terms[-1] != self.end:
            terms.append(terms[-1].next())
        return [term for term in terms if self.allow_summer or term.season != "Summer"]


@dataclass(frozen=True)
class ConfirmedTerm:
    term: Term
    sections: tuple[ScheduledSection, ...]


@dataclass(frozen=True)
class PlanningSession:
    request: PlanRequest
    confirmed: tuple[ConfirmedTerm, ...] = ()

    @property
    def current_term(self) -> Term:
        term = self.confirmed[-1].term.next() if self.confirmed else self.request.start
        return term.next() if term.season == "Summer" and not self.request.allow_summer else term

    def remaining_request(self) -> PlanRequest:
        return replace(self.request, start=self.current_term)

    @property
    def confirmed_codes(self) -> set[str]:
        return {section.code for term in self.confirmed for section in term.sections}

    def confirm(self, sections: Sequence[ScheduledSection]) -> PlanningSession:
        term = self.current_term
        if term.position > self.request.end.position or any(
            section.term != term for section in sections
        ):
            raise ValueError("Schedule options must belong to the current planning term.")
        return replace(self, confirmed=(*self.confirmed, ConfirmedTerm(term, tuple(sections))))
