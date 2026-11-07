from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .constants import SEMESTER_LST


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

    def __post_init__(self) -> None:
        if self.end.position < self.start.position:
            raise ValueError("The finish semester must not be earlier than the current semester.")

    def terms(self) -> list[Term]:
        terms = [self.start]
        while terms[-1] != self.end:
            terms.append(terms[-1].next())
        return terms


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
        return self.confirmed[-1].term.next() if self.confirmed else self.request.start

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
