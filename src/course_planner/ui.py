from __future__ import annotations

import pandas as pd
import streamlit as st

from .constants import ALL_LECTURES_PATH, COURSE_PLANS_PATH, GRADE_CREDITS, SEMESTER_LST
from .optimization import (
    advance_term,
    build_planning_terms,
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

    render_configuration_form(course_labels, processed_data)

    if not st.session_state.calculated:
        st.info("Select completed courses and planning dates, then calculate schedule options.")
        return

    if st.session_state.path:
        st.subheader("Confirmed Schedules")
        for term, lectures in st.session_state.path.items():
            if lectures:
                visualize_schedule(
                    st.session_state.path_data,
                    lectures,
                    title=f"Selected schedule for {term.replace('_', ' ')}",
                )
            else:
                st.write(f"No courses selected for {term.replace('_', ' ')}.")

    if st.session_state.remaining_data is None:
        st.session_state.remaining_data = processed_data.copy()

    status_col, remaining_col = st.columns(2)
    status_col.metric(
        "Current Grade Level",
        calculate_current_grade_level(
            course_plan,
            st.session_state.completed_courses,
        ),
    )
    remaining_col.metric(
        "Remaining Courses",
        st.session_state.remaining_data["Code"].nunique(),
    )

    if st.session_state.remaining_data.empty:
        st.success("All remaining courses have been scheduled within the selected plan.")
        return

    try:
        build_planning_terms(
            st.session_state.current_year,
            st.session_state.current_semester,
            st.session_state.last_year,
            st.session_state.last_semester,
        )
    except ValueError:
        st.warning("The selected planning window has ended before all remaining courses were scheduled.")
        return

    if st.session_state.updated:
        with st.spinner("Generating schedule options..."):
            try:
                st.session_state.unique_paths = generate_schedule_options(
                    st.session_state.remaining_data,
                    st.session_state.completed_courses,
                    st.session_state.current_year,
                    st.session_state.current_semester,
                    st.session_state.last_year,
                    st.session_state.last_semester,
                )
            except RuntimeError as error:
                st.error(str(error))
                return
        st.session_state.updated = False

    st.subheader(
        f"Planning for {st.session_state.current_year} {st.session_state.current_semester}"
    )

    if not st.session_state.unique_paths:
        st.warning("No feasible schedules were found for the remaining planning window.")
        return

    render_schedule_options(course_plan, st.session_state.remaining_data)


def initialize_session_state() -> None:
    if "remaining_data" not in st.session_state:
        st.session_state.remaining_data = None
    if "completed_courses" not in st.session_state:
        st.session_state.completed_courses = []
    if "seed_completed_courses" not in st.session_state:
        st.session_state.seed_completed_courses = []
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
    if "path_data" not in st.session_state:
        st.session_state.path_data = pd.DataFrame()
    if "unique_paths" not in st.session_state:
        st.session_state.unique_paths = []
    if "updated" not in st.session_state:
        st.session_state.updated = False
    if "calculated" not in st.session_state:
        st.session_state.calculated = False


def render_configuration_form(
    course_labels: dict[str, str], processed_data: pd.DataFrame
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
            default=st.session_state.seed_completed_courses,
            format_func=lambda code: f"{code} - {course_labels.get(code, code)}",
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

    reset_planner(
        processed_data,
        selected_completed_courses,
        current_year,
        current_semester,
        last_year,
        last_semester,
    )
    st.rerun()


def reset_planner(
    processed_data: pd.DataFrame,
    selected_completed_courses: list[str],
    current_year: int,
    current_semester: str,
    last_year: int,
    last_semester: str,
) -> None:
    completed_courses = list(dict.fromkeys(selected_completed_courses))

    st.session_state.seed_completed_courses = completed_courses.copy()
    st.session_state.completed_courses = completed_courses.copy()
    st.session_state.current_year = current_year
    st.session_state.current_semester = current_semester
    st.session_state.last_year = last_year
    st.session_state.last_semester = last_semester
    st.session_state.remaining_data = processed_data[
        ~processed_data["Code"].isin(completed_courses)
    ].reset_index(drop=True)
    st.session_state.path = {}
    st.session_state.path_data = pd.DataFrame()
    st.session_state.unique_paths = []
    st.session_state.updated = True
    st.session_state.calculated = True


def calculate_current_grade_level(
    course_plan: pd.DataFrame, completed_courses: list[str]
) -> int:
    completed_credits = pd.to_numeric(
        course_plan[course_plan["Course Code"].isin(completed_courses)]["Credits"],
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
    course_plan: pd.DataFrame, current_data: pd.DataFrame
) -> None:
    course_name_lookup = (
        course_plan.drop_duplicates(subset=["Course Code"])
        .set_index("Course Code")["Course Name"]
        .to_dict()
    )
    type_lookup = (
        current_data.drop_duplicates(subset=["Code"])
        .set_index("Code")["Type"]
        .fillna("")
        .to_dict()
    )

    option_indices = list(range(len(st.session_state.unique_paths)))
    for option_index, path in enumerate(st.session_state.unique_paths, start=1):
        term_path = [
            course
            for course in path
            if course[1] == st.session_state.current_year
            and course[2] == st.session_state.current_semester
        ]

        st.subheader(f"Option {option_index}")
        if not term_path:
            st.write("No courses scheduled for this term.")
            continue

        selected_codes = sorted({course[0] for course in term_path})
        st.write(
            "Courses:",
            ", ".join(
                f"{code} - {course_name_lookup.get(code, code)}" for code in selected_codes
            ),
        )
        st.write(f"ITB Courses: {_count_courses_by_type(type_lookup, term_path, 'ITB')}")
        st.write(
            "Elective Analytic Courses: "
            f"{_count_courses_by_type(type_lookup, term_path, 'Elective Analytic')}"
        )
        st.write(
            "Elective Functional Courses: "
            f"{_count_courses_by_type(type_lookup, term_path, 'Elective Functional')}"
        )
        st.write(
            "Main Courses: "
            f"{_count_main_courses(type_lookup, term_path)}"
        )
        visualize_schedule(current_data, term_path, title=f"Option {option_index} Schedule")

    selected_option = st.radio(
        "Select your preferred schedule",
        options=option_indices,
        format_func=lambda option_index: f"Option {option_index + 1}",
        key=f"radio_{st.session_state.current_year}_{st.session_state.current_semester}",
    )
    confirmed = st.button(
        f"Confirm {st.session_state.current_year} {st.session_state.current_semester}",
        key=f"confirm_{st.session_state.current_year}_{st.session_state.current_semester}",
    )

    if not confirmed:
        return

    confirm_selected_path(
        st.session_state.unique_paths[selected_option],
        current_data,
    )
    st.rerun()


def confirm_selected_path(selected_path: list[list], current_data: pd.DataFrame) -> None:
    selected_courses = [
        course
        for course in selected_path
        if course[1] == st.session_state.current_year
        and course[2] == st.session_state.current_semester
    ]

    selected_codes = [course[0] for course in selected_courses]
    st.session_state.completed_courses = list(
        dict.fromkeys(st.session_state.completed_courses + selected_codes)
    )
    st.session_state.remaining_data = current_data[
        ~current_data["Code"].isin(st.session_state.completed_courses)
    ].reset_index(drop=True)

    selected_rows = current_data[
        current_data.apply(
            lambda row: any(
                row["Code"] == course[0]
                and row["Term"] == course[2]
                and row["Days"] == course[3]
                and row["StartTimeFloat"] == course[4]
                for course in selected_courses
            ),
            axis=1,
        )
    ]
    st.session_state.path_data = pd.concat(
        [st.session_state.path_data, selected_rows],
        ignore_index=True,
    ).drop_duplicates()
    st.session_state.path[
        f"{st.session_state.current_year}_{st.session_state.current_semester}"
    ] = selected_courses

    next_year, next_semester = advance_term(
        st.session_state.current_year,
        st.session_state.current_semester,
    )
    st.session_state.current_year = next_year
    st.session_state.current_semester = next_semester
    st.session_state.updated = True


def _count_courses_by_type(
    type_lookup: dict[str, str], term_path: list[list], expected_type: str
) -> int:
    return sum(1 for course in term_path if type_lookup.get(course[0], "") == expected_type)


def _count_main_courses(type_lookup: dict[str, str], term_path: list[list]) -> int:
    excluded_types = {"ITB", "Elective Analytic", "Elective Functional"}
    return sum(
        1 for course in term_path if type_lookup.get(course[0], "") not in excluded_types
    )
