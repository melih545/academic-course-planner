from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass

import pandas as pd

from .constants import MAX_SOLUTIONS, SEMESTER_LST
from .curriculum import (
    AnyOf,
    Curriculum,
    MinimumGrade,
    Prerequisite,
    UnresolvedPrerequisite,
    can_satisfy,
)

try:
    from pyscipopt import Expr, Model, quicksum
except ModuleNotFoundError:
    Model = None


@dataclass(frozen=True)
class ScheduledSection:
    code: str
    year: int
    term: str
    section_id: int


@dataclass
class PlanResult:
    options: list[list[ScheduledSection]]
    issues: tuple[str, ...] = ()


def semester_position(year: int, term: str) -> tuple[int, int]:
    return year, SEMESTER_LST.index(term)


def build_planning_terms(
    start_year: int, start_term: str, end_year: int, end_term: str
) -> list[tuple[int, str]]:
    if semester_position(end_year, end_term) < semester_position(start_year, start_term):
        raise ValueError("End semester must not be earlier than the current semester.")

    terms: list[tuple[int, str]] = []
    start_position = semester_position(start_year, start_term)
    end_position = semester_position(end_year, end_term)

    for year in range(start_year, end_year + 1):
        for term in SEMESTER_LST:
            position = semester_position(year, term)
            if start_position <= position <= end_position:
                terms.append((year, term))

    return terms


def advance_term(year: int, term: str) -> tuple[int, str]:
    current_index = SEMESTER_LST.index(term)
    next_index = (current_index + 1) % len(SEMESTER_LST)
    next_year = year + 1 if term == "Fall" else year
    return next_year, SEMESTER_LST[next_index]


def generate_schedule_options(
    data: pd.DataFrame,
    completed_grades: Mapping[str, str],
    start_year: int,
    start_term: str,
    end_year: int,
    end_term: str,
    max_solutions: int = MAX_SOLUTIONS,
    *,
    curriculum: Curriculum,
    confirmed_courses: Set[str] = frozenset(),
) -> PlanResult:
    if Model is None:
        raise RuntimeError(
            "PySCIPOpt is not installed. Install project dependencies before running the planner."
        )
    if max_solutions < 1:
        raise ValueError("At least one schedule option must be requested.")

    terms = build_planning_terms(start_year, start_term, end_year, end_term)
    completed_codes = set(completed_grades) | confirmed_courses
    required = curriculum.remaining_required(completed_codes)
    deficits = curriculum.elective_deficits(completed_codes)

    sections_by_season = defaultdict(list)
    if not data.empty:
        for section_id, code, season in data[
            ["SectionId", "Code", "Term"]
        ].drop_duplicates().itertuples(index=False, name=None):
            if code in curriculum.courses and code not in completed_codes:
                sections_by_season[season].append((code, section_id))

    # A course can be a prerequisite only after an earlier term. This pass
    # checks optimistic reachability before asking SCIP to resolve conflicts.
    prior_codes = set(confirmed_courses)
    offered = set()
    candidates_by_term = {}
    for year, season in terms:
        sections = sections_by_season[season]
        offered.update(code for code, _ in sections)
        candidates = [
            ScheduledSection(code, year, season, section_id)
            for code, section_id in sections
            if can_satisfy(
                curriculum.courses[code].prerequisite,
                lambda rule: rule.code in prior_codes or rule.is_met_by(completed_grades),
            )
        ]
        candidates_by_term[year, season] = candidates
        prior_codes.update(section.code for section in candidates)

    eligible = prior_codes - confirmed_courses
    courses_with_sections = {
        code for sections in sections_by_season.values() for code, _ in sections
    }
    issues = [
        f"{code}: no eligible section in the selected planning window."
        for code in sorted(required - offered)
        if code in courses_with_sections
    ]
    issues.extend(
        f"{code}: no eligible section in the bundled course history."
        for code in sorted(required - courses_with_sections)
    )
    issues.extend(
        _prerequisite_issue(curriculum, code)
        for code in sorted((required & offered) - eligible)
    )
    for category, deficit in deficits.items():
        available = {code for code in eligible if curriculum.courses[code].category == category}
        if len(available) < deficit:
            issues.append(
                f"{category}: {deficit} more required, but only {len(available)} "
                "can be scheduled in this window."
            )
            issues.extend(
                _prerequisite_issue(curriculum, code)
                for code in sorted(offered - eligible)
                if curriculum.courses[code].category == category
            )
    if issues:
        return PlanResult([], tuple(issues))
    if not required and not any(deficits.values()):
        return PlanResult([[]])

    model = Model("Course_Planner")
    model.hideOutput()
    section_vars = {
        section: model.addVar(vtype="B", name=f"Section_{section.year}_{section.section_id}")
        for candidates in candidates_by_term.values()
        for section in candidates
    }
    vars_by_code = defaultdict(list)
    for section, variable in section_vars.items():
        vars_by_code[section.code].append(variable)
    for code, variables in vars_by_code.items():
        if code in required:
            model.addCons(quicksum(variables) == 1)
        else:
            model.addCons(quicksum(variables) <= 1)
    for category, deficit in deficits.items():
        if deficit:
            model.addCons(
                quicksum(
                    variable
                    for code, variables in vars_by_code.items()
                    if curriculum.courses[code].category == category
                    for variable in variables
                ) >= deficit
            )

    conflicts = _section_conflicts(data)
    used_terms = {}
    prior_vars = defaultdict(list)
    for (year, season), candidates in candidates_by_term.items():
        used = model.addVar(vtype="B", name=f"Use_{year}_{season}")
        used_terms[year, season] = used
        term_vars = {section.section_id: section_vars[section] for section in candidates}
        model.addCons(quicksum(term_vars.values()) <= 10 * used)
        for first, second in conflicts[season]:
            if first in term_vars and second in term_vars:
                model.addCons(term_vars[first] + term_vars[second] <= 1)

        def prior_support(rule: MinimumGrade) -> bool | Expr:
            if rule.code in confirmed_courses or rule.is_met_by(completed_grades):
                return True
            variables = prior_vars.get(rule.code)
            return quicksum(variables) if variables else False

        vars_by_term_code = defaultdict(list)
        for section in candidates:
            vars_by_term_code[section.code].append(section_vars[section])
        for code, variables in vars_by_term_code.items():
            support = _rule_support(model, curriculum.courses[code].prerequisite, prior_support)
            if support is not True:
                model.addCons(quicksum(variables) <= support)
        for code, variables in vars_by_term_code.items():
            prior_vars[code].extend(variables)

    for index, (left, right) in enumerate(zip(terms, terms[1:])):
        if left[1] == "Summer" and index > 0:
            left = terms[index - 1]
        model.addCons(used_terms[left] >= used_terms[right])
    model.setObjective(
        (len(curriculum.courses) + 1) * quicksum(used_terms.values())
        + quicksum(section_vars.values()),
        "minimize",
    )

    options = []
    for index in range(max_solutions):
        model.optimize()
        if model.getStatus() != "optimal":
            break
        solution = model.getBestSol()
        selected = [
            section for section, variable in section_vars.items()
            if model.getSolVal(solution, variable) > 0.5
        ]
        options.append(selected)
        if index + 1 == max_solutions:
            break
        model.freeTransform()
        model.addCons(quicksum(section_vars[section] for section in selected) <= len(selected) - 1)

    if not options:
        return PlanResult([], (
            "No feasible schedule fits the prerequisites, timetable conflicts, "
            "elective requirements, and ten-course limit per term in this window.",
        ))
    return PlanResult(options)


def _prerequisite_issue(curriculum: Curriculum, code: str) -> str:
    course = curriculum.courses[code]
    if isinstance(course.prerequisite, UnresolvedPrerequisite):
        return f"{code}: prerequisite information is unavailable or unreadable ({course.prerequisite_text})."
    return f"{code}: prerequisite cannot be met before an offered term ({course.prerequisite_text})."


def _rule_support(
    model: Model,
    rule: Prerequisite | None,
    available: Callable[[MinimumGrade], bool | Expr],
) -> bool | Expr:
    if rule is None:
        return True
    if isinstance(rule, MinimumGrade):
        return available(rule)
    if isinstance(rule, UnresolvedPrerequisite):
        return False

    supports = [_rule_support(model, child, available) for child in rule.rules]
    if isinstance(rule, AnyOf):
        if any(support is True for support in supports):
            return True
        expressions = [support for support in supports if support is not False]
        return quicksum(expressions) if expressions else False

    if any(support is False for support in supports):
        return False
    expressions = [support for support in supports if support is not True]
    if not expressions:
        return True
    if len(expressions) == 1:
        return expressions[0]
    satisfied = model.addVar(vtype="B")
    for expression in expressions:
        model.addCons(satisfied <= expression)
    return satisfied


def _section_conflicts(data: pd.DataFrame) -> dict[str, set[tuple[int, int]]]:
    conflicts = defaultdict(set)
    columns = ["SectionId", "Code", "StartTimeFloat", "EndTimeFloat"]
    for (season, _), meetings in data.groupby(["Term", "Days"]):
        active = []
        for section_id, code, start, end in (
            meetings.sort_values("StartTimeFloat")[columns].itertuples(index=False, name=None)
        ):
            active = [
                (other_id, other_code, stop)
                for other_id, other_code, stop in active
                if stop > start
            ]
            for other_id, other_code, _ in active:
                if other_code != code:
                    conflicts[season].add(tuple(sorted((section_id, other_id))))
            active.append((section_id, code, end))
    return conflicts
