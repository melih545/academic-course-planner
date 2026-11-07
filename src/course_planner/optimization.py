from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from .constants import MAX_SOLUTIONS
from .curriculum import (
    AnyOf,
    Curriculum,
    MinimumGrade,
    Prerequisite,
    UnresolvedPrerequisite,
    can_satisfy,
)

from .schedule import PlanRequest, ScheduledSection, Section


SOLVE_BUDGET_SECONDS = 8.0

try:
    from pyscipopt import Expr, Model, quicksum
except ModuleNotFoundError:
    Model = None


@dataclass
class PlanResult:
    options: list[list[ScheduledSection]]
    issues: tuple[str, ...] = ()
    status: Literal["solved", "infeasible", "time_limit", "interrupted"] = "solved"


def generate_schedule_options(
    sections: Sequence[Section],
    request: PlanRequest,
    max_solutions: int = MAX_SOLUTIONS,
    *,
    curriculum: Curriculum,
    confirmed_courses: Set[str] = frozenset(),
    time_limit_seconds: float = SOLVE_BUDGET_SECONDS,
) -> PlanResult:
    if Model is None:
        raise RuntimeError(
            "PySCIPOpt is not installed. Install project dependencies before running the planner."
        )
    if max_solutions < 1:
        raise ValueError("At least one schedule option must be requested.")
    if time_limit_seconds <= 0:
        raise ValueError("The solve time limit must be positive.")

    terms = request.terms()
    completed_grades = request.completed_grades
    completed_codes = set(completed_grades) | confirmed_courses
    required = curriculum.remaining_required(completed_codes)
    deficits = curriculum.elective_deficits(completed_codes)

    sections_by_season = defaultdict(list)
    for section in sections:
        if section.code in curriculum.courses and section.code not in completed_codes:
            sections_by_season[section.season].append(section)

    # A course can be a prerequisite only after an earlier term. This pass
    # checks optimistic reachability before asking SCIP to resolve conflicts.
    prior_codes = set(confirmed_courses)
    offered = set()
    candidates_by_term = {}
    for term in terms:
        offered_sections = sections_by_season[term.season]
        offered.update(section.code for section in offered_sections)
        candidates = [
            ScheduledSection(section, term)
            for section in offered_sections
            if can_satisfy(
                curriculum.courses[section.code].prerequisite,
                lambda rule: rule.code in prior_codes or rule.is_met_by(completed_grades),
            )
        ]
        candidates_by_term[term] = candidates
        prior_codes.update(section.code for section in candidates)

    eligible = prior_codes - confirmed_courses
    courses_with_sections = {
        section.code for season in sections_by_season.values() for section in season
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
        return PlanResult([], tuple(issues), "infeasible")
    if not required and not any(deficits.values()):
        return PlanResult([[]])

    deadline = monotonic() + time_limit_seconds
    model = Model("Course_Planner")
    model.hideOutput()
    section_vars = {
        section: model.addVar(vtype="B", name=f"Section_{section.term.year}_{section.section.id}")
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

    candidate_sections = {
        scheduled.section for candidates in candidates_by_term.values() for scheduled in candidates
    }
    conflicts = _section_conflicts(candidate_sections)
    used_terms = {}
    prior_vars = defaultdict(list)
    for term, candidates in candidates_by_term.items():
        used = model.addVar(vtype="B", name=f"Use_{term.year}_{term.season}")
        used_terms[term] = used
        term_vars = {section.section.id: section_vars[section] for section in candidates}
        model.addCons(quicksum(term_vars.values()) <= 10 * used)
        for first, second in conflicts[term.season]:
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
        if left.season == "Summer" and index > 0:
            left = terms[index - 1]
        model.addCons(used_terms[left] >= used_terms[right])
    model.setObjective(
        (len(curriculum.courses) + 1) * quicksum(used_terms.values())
        + quicksum(section_vars.values()),
        "minimize",
    )

    options = []
    for index in range(max_solutions):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return PlanResult(options, status="time_limit")
        model.setRealParam("limits/time", remaining)
        model.optimize()
        status = model.getStatus()
        if status == "infeasible":
            break
        if model.getNSols():
            solution = model.getBestSol()
            selected = [
                section for section, variable in section_vars.items()
                if model.getSolVal(solution, variable) > 0.5
            ]
            options.append(selected)
        if status == "timelimit":
            return PlanResult(options, status="time_limit")
        if status != "optimal":
            return PlanResult(options, status="interrupted")
        if index + 1 == max_solutions:
            break
        model.freeTransform()
        model.addCons(quicksum(section_vars[section] for section in selected) <= len(selected) - 1)

    if not options:
        return PlanResult(
            [],
            ("No feasible schedule fits the prerequisites, timetable conflicts, "
             "elective requirements, and ten-course limit per term in this window.",),
            "infeasible",
        )
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


def _section_conflicts(sections: Set[Section]) -> dict[str, set[tuple[int, int]]]:
    meetings_by_day = defaultdict(list)
    for section in sections:
        for meeting in section.meetings:
            meetings_by_day[section.season, meeting.day].append((meeting, section))

    conflicts = defaultdict(set)
    for (season, _), meetings in meetings_by_day.items():
        active = []
        for meeting, section in sorted(meetings, key=lambda pair: pair[0].start_minute):
            active = [
                (end, other) for end, other in active
                if end > meeting.start_minute
            ]
            for _, other in active:
                if other.code != section.code:
                    conflicts[season].add(tuple(sorted((section.id, other.id))))
            active.append((meeting.end_minute, section))
    return conflicts
