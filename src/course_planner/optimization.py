from __future__ import annotations

from itertools import combinations
from typing import Any

import pandas as pd

from .constants import MAX_SOLUTIONS, SEMESTER_LST
from .preprocessing import check_conflict

try:
    from pyscipopt import Model
except ModuleNotFoundError:  # pragma: no cover - exercised through UI smoke test.
    Model = None


CourseRecord = dict[str, Any]
ScheduleOption = list[list[Any]]


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
    completed_courses: list[str],
    start_year: int,
    start_term: str,
    end_year: int,
    end_term: str,
    max_solutions: int = MAX_SOLUTIONS,
) -> list[ScheduleOption]:
    if Model is None:
        raise RuntimeError(
            "PySCIPOpt is not installed. Install project dependencies before running the planner."
        )

    if data.empty:
        return []

    planning_terms = build_planning_terms(
        start_year, start_term, end_year, end_term
    )
    completed_course_set = set(completed_courses)

    model = Model("Course_Planner")
    records: list[CourseRecord] = data.to_dict("records")

    course_rows_by_code: dict[str, list[CourseRecord]] = {}
    for row in records:
        course_rows_by_code.setdefault(row["Code"], []).append(row)

    course_vars = {
        _course_var_key(row, year): model.addVar(
            vtype="B",
            name=_course_var_name(row, year),
        )
        for row in records
        for year, term in planning_terms
        if row["Term"] == term
    }

    elective_types = (
        data.loc[data["Type"].fillna("").str.contains("Elective"), "Type"]
        .dropna()
        .unique()
    )
    for elective_type in elective_types:
        elective_keys = [
            _course_var_key(row, year)
            for row in records
            if row["Type"] == elective_type
            for year, term in planning_terms
            if row["Term"] == term
        ]
        if elective_keys:
            model.addCons(
                sum(course_vars[key] for key in elective_keys) >= 4,
                name=f"ElectiveCreditsConstraint_{elective_type.replace(' ', '_')}",
            )

    itb_keys = [
        _course_var_key(row, year)
        for row in records
        if row["Type"] == "ITB"
        for year, term in planning_terms
        if row["Term"] == term
    ]
    if itb_keys:
        model.addCons(sum(course_vars[key] for key in itb_keys) >= 1, name="ITBConstraint")

    for course, rows in course_rows_by_code.items():
        if course in completed_course_set:
            continue

        course_keys = [
            _course_var_key(row, year)
            for row in rows
            for year, term in planning_terms
            if row["Term"] == term
        ]

        course_type = str(rows[0].get("Type") or "")
        is_elective = any(keyword in course_type for keyword in ("Elective", "ITB"))

        if is_elective:
            model.addCons(sum(course_vars[key] for key in course_keys) <= 1, name=f"Unique_{course}")
        else:
            model.addCons(sum(course_vars[key] for key in course_keys) == 1, name=f"Unique_{course}")

    for row1, row2 in combinations(records, 2):
        if row1["Code"] == row2["Code"] or not check_conflict(row1, row2):
            continue

        for year, term in planning_terms:
            if row1["Term"] != term:
                continue

            course1_key = _course_var_key(row1, year)
            course2_key = _course_var_key(row2, year)
            model.addCons(
                course_vars[course1_key] + course_vars[course2_key] <= 1,
                name=(
                    f"Conflict_{row1['Code']}_{row2['Code']}_{year}_{term}_"
                    f"{row1['Days']}_{row1['StartTimeFloat']}_{row2['StartTimeFloat']}"
                ),
            )

    prereq_map: dict[str, list[str]] = {}
    for row in records:
        prereq_text = row.get("Prerequisites")
        if pd.notna(prereq_text) and prereq_text:
            prereqs = [
                prereq
                for prereq in str(prereq_text).split(", ")
                if prereq not in completed_course_set
            ]
            if prereqs:
                prereq_map[row["Code"]] = prereqs

    unique_codes = list(course_rows_by_code.keys())
    term_vars = {
        (course, year, term): model.addVar(
            vtype="B",
            name=f"Take_{course.replace(' ', '_')}_{year}_{term}_anytime",
        )
        for course in unique_codes
        for year, term in planning_terms
    }

    for course, rows in course_rows_by_code.items():
        for year, term in planning_terms:
            matching_keys = [
                _course_var_key(row, year) for row in rows if row["Term"] == term
            ]
            model.addCons(
                sum(course_vars[key] for key in matching_keys) == term_vars[(course, year, term)],
                name=f"Link_{course}_{year}_{term}",
            )

    for course, prereqs in prereq_map.items():
        for prereq in prereqs:
            if prereq not in course_rows_by_code:
                continue

            for year, term in planning_terms:
                valid_prereq_terms = [
                    term_vars[(prereq, prereq_year, prereq_term)]
                    for prereq_year, prereq_term in planning_terms
                    if semester_position(prereq_year, prereq_term)
                    < semester_position(year, term)
                ]

                if valid_prereq_terms:
                    model.addCons(
                        term_vars[(course, year, term)] <= sum(valid_prereq_terms),
                        name=f"Prerequisite_{course}_{prereq}_{year}_{term}",
                    )
                else:
                    model.addCons(
                        term_vars[(course, year, term)] == 0,
                        name=f"Prerequisite_{course}_{prereq}_{year}_{term}_Invalid",
                    )

    semester_vars = {
        (year, term): model.addVar(vtype="B", name=f"Use_{year}_{term}")
        for year, term in planning_terms
    }

    for year, term in planning_terms:
        semester_keys = [
            _course_var_key(row, year) for row in records if row["Term"] == term
        ]
        model.addCons(
            sum(course_vars[key] for key in semester_keys) <= semester_vars[(year, term)] * 10,
            name=f"LinkCoursesToSemester_{year}_{term}",
        )

    model.setObjective(sum(semester_vars.values()), "minimize")

    for index, current_term in enumerate(planning_terms[:-1]):
        left_term = current_term
        if current_term[1] == "Summer" and index > 0:
            left_term = planning_terms[index - 1]

        right_term = planning_terms[index + 1]
        if left_term == right_term:
            continue

        model.addCons(
            semester_vars[left_term] >= semester_vars[right_term],
            name=f"NoGap_{left_term[0]}_{left_term[1]}_to_{right_term[0]}_{right_term[1]}",
        )

    paths: list[ScheduleOption] = []
    model.optimize()

    while model.getStatus() in {"optimal", "feasible"} and len(paths) < max_solutions:
        solution = model.getBestSol()
        current_schedule = [
            [row["Code"], year, row["Term"], row["Days"], row["StartTimeFloat"]]
            for row in records
            for year, term in planning_terms
            if row["Term"] == term
            and model.getSolVal(solution, course_vars[_course_var_key(row, year)]) > 0.5
        ]

        if current_schedule not in paths:
            paths.append(current_schedule)

        model.freeTransform()
        excluded_solution = [
            course_vars[(code, year, term, day, start_time)]
            for code, year, term, day, start_time in current_schedule
        ]
        if not excluded_solution:
            break

        model.addCons(sum(excluded_solution) <= len(current_schedule) - 1)
        model.optimize()

    return paths


def _course_var_key(row: CourseRecord, year: int) -> tuple[str, int, str, str, float]:
    return (
        row["Code"],
        year,
        row["Term"],
        row["Days"],
        row["StartTimeFloat"],
    )


def _course_var_name(row: CourseRecord, year: int) -> str:
    safe_code = row["Code"].replace(" ", "_")
    safe_day = str(row["Days"]).replace(" ", "_")
    return (
        f"Take_{safe_code}_{year}_{row['Term']}_{safe_day}_at_{row['StartTimeFloat']}"
    )
