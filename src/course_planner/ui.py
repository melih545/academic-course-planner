from __future__ import annotations

import pandas as pd
import streamlit as st

from .constants import ALL_LECTURES_PATH, COURSE_PLANS_PATH, GRADE_CREDITS, SEMESTER_LST
from .curriculum import Curriculum, minimum_grades
from .optimization import (
    PlanResult,
    ScheduledSection,
    advance_term,
    generate_schedule_options,
    semester_position,
)
from .preprocessing import process_uploaded_files
from .visualization import visualize_schedule


@st.cache_data(show_spinner=False)
def load_course_plan(path: str) -> pd.DataFrame:
    return pd.read_csv(path)


@st.cache_data(show_spinner=False)
def load_processed_schedule_data(lectures_path: str, course_plan_path: str) -> pd.DataFrame:
    course_plan = pd.read_csv(course_plan_path)
    return process_uploaded_files(lectures_path, course_plan)


def main() -> None:
    st.set_page_config(page_title="Course Planner", layout="wide")
    st.title("Course Planner")
    st.caption(
        "Generate semester-by-semester schedule options from bundled course history and curriculum data."
    )

    initialize_session_state()

    course_plan = load_course_plan(str(COURSE_PLANS_PATH))
    processed_data = load_processed_schedule_data(
        str(ALL_LECTURES_PATH),
        str(COURSE_PLANS_PATH),
    )
    course_labels = (
        course_plan.drop_duplicates(subset=["Course Code"])
        .set_index("Course Code")["Course Name"]
        .to_dict()
    )

    curriculum = Curriculum.from_frame(course_plan)
    grades = {
        grade
        for course in curriculum.courses.values()
        for grade in minimum_grades(course.prerequisite)
    }
    for grade in grades:
        course_labels.setdefault(grade.code, "External prerequisite")
    higher_grade_options = sorted({grade.code for grade in grades if grade.grade == "BB"})
    render_configuration_form(course_labels, higher_grade_options)

    if not st.session_state.calculated:
        st.info("Select completed courses and planning dates, then calculate schedule options.")
        return

    if st.session_state.path:
        st.subheader("Confirmed Schedules")
    for term, sections in st.session_state.path.items():
        if sections:
            visualize_schedule(
                processed_data, sections, title=f"Selected schedule for {term.replace('_', ' ')}"
            )
        else:
            st.write(f"No courses selected for {term.replace('_', ' ')}.")

    confirmed_codes = {
        section.code for sections in st.session_state.path.values() for section in sections
    }
    completed_grades = st.session_state.completed_grades
    accounted_codes = set(completed_grades) | confirmed_codes
    remaining_count = len(curriculum.remaining_required(accounted_codes)) + sum(
        curriculum.elective_deficits(accounted_codes).values()
    )

    status_col, remaining_col = st.columns(2)
    status_col.metric(
        "Current Grade Level", calculate_current_grade_level(course_plan, completed_grades)
    )
    remaining_col.metric("Remaining Required Courses and Electives", remaining_count)
    if not remaining_count:
        st.success("All remaining curriculum requirements have been scheduled or completed.")
        return

    current_term = (st.session_state.current_year, st.session_state.current_semester)
    last_term = (st.session_state.last_year, st.session_state.last_semester)
    if semester_position(*current_term) > semester_position(*last_term):
        st.warning("The planning window has ended with outstanding curriculum requirements.")
        return

    if st.session_state.result is None:
        with st.spinner("Generating schedule options..."):
            try:
                st.session_state.result = generate_schedule_options(
                    processed_data,
                    completed_grades,
                    *current_term,
                    *last_term,
                    curriculum=curriculum,
                    confirmed_courses=confirmed_codes,
                )
            except RuntimeError as error:
                st.error(str(error))
                return
    result: PlanResult = st.session_state.result
    st.subheader(f"Planning for {current_term[0]} {current_term[1]}")
    if result.issues:
        issues = "\n".join(f"- {issue}" for issue in result.issues)
        st.warning(f"No feasible schedule was found.\n\n{issues}")
        return

    st.caption("Prerequisites scheduled in earlier terms are conditional on earning their minimum grades.")
    render_schedule_options(course_labels, curriculum, processed_data, result.options)


def initialize_session_state() -> None:
    if "completed_grades" not in st.session_state:
        st.session_state.completed_grades = {}
    if "current_year" not in st.session_state:
        st.session_state.current_year = 2024
    if "current_semester" not in st.session_state:
        st.session_state.current_semester = SEMESTER_LST[0]
    if "last_year" not in st.session_state:
        st.session_state.last_year = 2024
    if "last_semester" not in st.session_state:
        st.session_state.last_semester = SEMESTER_LST[0]
    if "path" not in st.session_state:
        st.session_state.path = {}
    if "result" not in st.session_state:
        st.session_state.result = None
    if "calculated" not in st.session_state:
        st.session_state.calculated = False


def render_configuration_form(
    course_labels: dict[str, str], higher_grade_options: list[str]
) -> None:
    max_year = max(2034, st.session_state.current_year, st.session_state.last_year) + 1
    available_years = list(range(2024, max_year + 1))
    current_year_index = available_years.index(st.session_state.current_year)
    current_semester_index = SEMESTER_LST.index(st.session_state.current_semester)
    default_last_year = max(st.session_state.current_year, st.session_state.last_year)
    last_semester_index = SEMESTER_LST.index(st.session_state.last_semester)

    with st.form("planner_controls"):
        selected_completed_courses = st.multiselect(
            "Select completed courses",
            options=sorted(course_labels),
            default=list(st.session_state.completed_grades),
            help="Selected courses count as DD or higher unless marked BB or higher below.",
            format_func=lambda code: f"{code} - {course_labels.get(code, code)}",
        )

        completed_with_bb = st.multiselect(
            "Completed prerequisites with a grade of BB or higher",
            options=higher_grade_options,
            default=[
                code for code in higher_grade_options
                if st.session_state.completed_grades.get(code) == "BB"
            ],
            format_func=lambda code: f"{code} - {course_labels[code]}",
        )

        col1, col2 = st.columns(2)
        with col1:
            current_year = st.selectbox(
                "Select the current year",
                options=available_years,
                index=current_year_index,
            )
        with col2:
            current_semester = st.selectbox(
                "Select the current semester",
                options=SEMESTER_LST,
                index=current_semester_index,
            )

        adjusted_last_year_options = list(range(current_year, max_year + 1))
        if default_last_year not in adjusted_last_year_options:
            default_last_year = current_year
        adjusted_last_year_index = adjusted_last_year_options.index(default_last_year)

        col1, col2 = st.columns(2)
        with col1:
            last_year = st.selectbox(
                "Select the year you want to finish",
                options=adjusted_last_year_options,
                index=adjusted_last_year_index,
            )
        with col2:
            last_semester = st.selectbox(
                "Select the last semester you attended",
                options=SEMESTER_LST,
                index=last_semester_index,
            )

        submitted = st.form_submit_button("Calculate")

    if not submitted:
        return

    if semester_position(last_year, last_semester) < semester_position(
        current_year, current_semester
    ):
        st.error("The finish semester must not be earlier than the current semester.")
        return

    if set(completed_with_bb) - set(selected_completed_courses):
        st.error("Select each BB-or-higher course in the completed courses list as well.")
        return

    st.session_state.completed_grades = {
        code: "BB" if code in completed_with_bb else "DD"
        for code in selected_completed_courses
    }
    st.session_state.current_year = current_year
    st.session_state.current_semester = current_semester
    st.session_state.last_year = last_year
    st.session_state.last_semester = last_semester
    st.session_state.path = {}
    st.session_state.result = None
    st.session_state.calculated = True
    st.rerun()


def calculate_current_grade_level(
    course_plan: pd.DataFrame, completed_grades: dict[str, str]
) -> int:
    completed_credits = pd.to_numeric(
        course_plan.drop_duplicates("Course Code")
        .loc[lambda rows: rows["Course Code"].isin(completed_grades), "Credits"],
        errors="coerce",
    ).sum()

    return max(
        [
            grade
            for grade, credits in GRADE_CREDITS.items()
            if completed_credits >= credits
        ],
        default=1,
    )


def render_schedule_options(
    course_labels: dict[str, str],
    curriculum: Curriculum,
    data: pd.DataFrame,
    options: list[list[ScheduledSection]],
) -> None:
    current_year = st.session_state.current_year
    current_semester = st.session_state.current_semester
    term_options = [
        [
            section for section in plan
            if (section.year, section.term) == (current_year, current_semester)
        ]
        for plan in options
    ]
    for number, sections in enumerate(term_options, start=1):
        st.subheader(f"Option {number}")
        if not sections:
            st.write("No courses scheduled for this term.")
            continue

        codes = sorted(section.code for section in sections)
        st.write("Courses:", ", ".join(f"{code} - {course_labels[code]}" for code in codes))
        for category in ("ITB", "Elective Analytic", "Elective Functional", "Main"):
            count = sum(curriculum.courses[code].category == category for code in codes)
            st.write(f"{category} Courses: {count}")
        visualize_schedule(data, sections, title=f"Option {number} Schedule")

    selected_option = st.radio(
        "Select your preferred schedule",
        options=range(len(term_options)),
        format_func=lambda index: f"Option {index + 1}",
        key=f"radio_{current_year}_{current_semester}",
    )
    if st.button(
        f"Confirm {current_year} {current_semester}",
        key=f"confirm_{current_year}_{current_semester}",
    ):
        st.session_state.path[f"{current_year}_{current_semester}"] = term_options[selected_option]
        st.session_state.current_year, st.session_state.current_semester = advance_term(
            current_year, current_semester
        )
        st.session_state.result = None
        st.rerun()
