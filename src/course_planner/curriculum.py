from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass

import pandas as pd

GRADE_ORDER = ("FF", "FD", "DD", "DC", "CC", "CB", "BB", "BA", "AA")
ELECTIVE_MINIMUMS = {"Elective Analytic": 4, "Elective Functional": 4, "ITB": 1}

_CONNECTOR = re.compile(r"(\(|\)|\b(?:veya|ve|or|and)\b)", re.IGNORECASE)
_COURSE_CODE = r"[A-Z]+\s+\d+[A-Z]*"
_GRADED_COURSE = re.compile(
    rf"({_COURSE_CODE})\s+MIN\s+([A-Z]{{2}})|MIN\s+([A-Z]{{2}})\s+({_COURSE_CODE})"
)


@dataclass(frozen=True)
class MinimumGrade:
    code: str
    grade: str

    def is_met_by(self, completed_grades: Mapping[str, str]) -> bool:
        grade = completed_grades.get(self.code)
        return grade in GRADE_ORDER and GRADE_ORDER.index(grade) >= GRADE_ORDER.index(self.grade)


@dataclass(frozen=True)
class AllOf:
    rules: tuple[Prerequisite, ...]


@dataclass(frozen=True)
class AnyOf:
    rules: tuple[Prerequisite, ...]


@dataclass(frozen=True)
class UnresolvedPrerequisite:
    text: str


type Prerequisite = MinimumGrade | AllOf | AnyOf | UnresolvedPrerequisite


def parse_prerequisite(value: object) -> Prerequisite | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.casefold() == "yok":
        return None

    tokens = iter(part.strip() for part in _CONNECTOR.split(text) if part.strip())
    token = next(tokens, None)

    def consume() -> str | None:
        nonlocal token
        current, token = token, next(tokens, None)
        return current

    def atom() -> Prerequisite:
        current = consume()
        if current == "(":
            rule = alternatives()
            if consume() != ")":
                raise ValueError("Missing closing parenthesis")
            return rule
        match = _GRADED_COURSE.fullmatch((current or "").upper())
        if match is None:
            raise ValueError("Expected a course code and minimum grade")
        code, grade, prefix_grade, prefix_code = match.groups()
        grade = grade or prefix_grade
        if grade not in GRADE_ORDER:
            raise ValueError("Unknown grade")
        return MinimumGrade(" ".join((code or prefix_code).split()), grade)

    def conjunction() -> Prerequisite:
        rules = [atom()]
        while (token or "").lower() in {"ve", "and"}:
            consume()
            rules.append(atom())
        return rules[0] if len(rules) == 1 else AllOf(tuple(rules))

    def alternatives() -> Prerequisite:
        rules = [conjunction()]
        while (token or "").lower() in {"veya", "or"}:
            consume()
            rules.append(conjunction())
        return rules[0] if len(rules) == 1 else AnyOf(tuple(rules))

    try:
        rule = alternatives()
        if token is None:
            return rule
    except ValueError:
        pass
    return UnresolvedPrerequisite(text)


def minimum_grades(rule: Prerequisite | None) -> tuple[MinimumGrade, ...]:
    if isinstance(rule, MinimumGrade):
        return (rule,)
    if isinstance(rule, (AllOf, AnyOf)):
        return tuple(grade for child in rule.rules for grade in minimum_grades(child))
    return ()


def can_satisfy(rule: Prerequisite | None, available: Callable[[MinimumGrade], bool]) -> bool:
    if rule is None:
        return True
    if isinstance(rule, MinimumGrade):
        return available(rule)
    if isinstance(rule, AllOf):
        return all(can_satisfy(child, available) for child in rule.rules)
    if isinstance(rule, AnyOf):
        return any(can_satisfy(child, available) for child in rule.rules)
    return False


@dataclass(frozen=True)
class Course:
    category: str
    prerequisite: Prerequisite | None
    prerequisite_text: str


@dataclass(frozen=True)
class RequirementProgress:
    category: str
    completed: int
    planned: int
    remaining: int


@dataclass(frozen=True)
class Curriculum:
    courses: Mapping[str, Course]

    @classmethod
    def from_frame(cls, data: pd.DataFrame) -> Curriculum:
        courses = {}
        for code, category, text in data[
            ["Course Code", "Type", "Prerequisites"]
        ].itertuples(index=False, name=None):
            courses[code] = Course(
                category, parse_prerequisite(text), "" if pd.isna(text) else str(text)
            )
        return cls(courses)

    def remaining_required(self, completed: Set[str]) -> set[str]:
        return {
            code
            for code, course in self.courses.items()
            if course.category not in ELECTIVE_MINIMUMS and code not in completed
        }

    def elective_deficits(self, completed: Set[str]) -> dict[str, int]:
        categories = {course.category for course in self.courses.values()}
        return {
            category: max(
                0,
                minimum - sum(
                    self.courses[code].category == category
                    for code in completed
                    if code in self.courses
                ),
            )
            for category, minimum in ELECTIVE_MINIMUMS.items()
            if category in categories
        }

    def progress(
        self, completed: Set[str], planned: Set[str]
    ) -> tuple[RequirementProgress, ...]:
        all_required = self.remaining_required(set())
        completed_required = self.remaining_required(completed)
        remaining_required = self.remaining_required(completed | planned)
        rows = [RequirementProgress(
            "Required courses",
            len(all_required - completed_required),
            len(completed_required - remaining_required),
            len(remaining_required),
        )]
        remaining_electives = self.elective_deficits(completed | planned)
        for category, deficit in self.elective_deficits(completed).items():
            remaining = remaining_electives[category]
            rows.append(RequirementProgress(
                category, ELECTIVE_MINIMUMS[category] - deficit, deficit - remaining, remaining
            ))
        return tuple(rows)
