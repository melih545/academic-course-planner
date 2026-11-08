from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256

import httpx
import pandas as pd
import streamlit as st

from .calendar_export import export_calendar
from .constants import (
    ALL_LECTURES_PATH,
    COURSE_PLANS_PATH,
    GRADE_CREDITS,
    SEMESTER_LST,
    WEEKDAYS,
)
from .curriculum import ELECTIVE_MINIMUMS, GRADE_ORDER, Curriculum, minimum_grades
from .datasets import MAX_CSV_MB, Dataset, parse_uploaded_dataset
from .optimization import PlanResult, generate_schedule_options
from .planning_api import Catalogue, PlanResponse
from .schedule import (
    ConfirmedTerm,
    PlanningSession,
    PlanRequest,
    ScheduledSection,
    Term,
    current_planning_term,
)
from .visualization import schedule_hours, visualize_schedule


@st.cache_data(show_spinner=False)
def load_dataset(lectures_path: str, course_plan_path: str) -> Dataset:
    return Dataset.from_frames(pd.read_csv(course_plan_path), pd.read_csv(lectures_path))


def select_dataset() -> tuple[Dataset, bool, dict[str, int] | None] | None:
    source = st.radio("Data source", ["Bundled data", "Use my CSV files"], horizontal=True)
    bundled = source == "Bundled data"
    minimums = None
    if bundled:
        token = ("bundled",)
        dataset = load_dataset(str(ALL_LECTURES_PATH), str(COURSE_PLANS_PATH))
    else:
        left, right = st.columns(2)
        catalog_file = left.file_uploader("Curriculum CSV", type="csv", max_upload_size=MAX_CSV_MB)
        history_file = right.file_uploader("Lecture history CSV", type="csv", max_upload_size=MAX_CSV_MB)
        st.caption("Upload either file or both. The bundled file is used for any input left empty.")
        if catalog_file is None and history_file is None:
            return None
        department = st.text_input(
            "Department code filter",
            value="ENDE" if catalog_file is None else "",
            max_chars=20,
            help="Leave blank to allow all departments in the timetable.",
        ).strip().upper()
        catalog_bytes = catalog_file.getvalue() if catalog_file is not None else None
        history_bytes = history_file.getvalue() if history_file is not None else None
        file_token = (
            sha256(catalog_bytes).hexdigest() if catalog_bytes is not None else "bundled",
            sha256(history_bytes).hexdigest() if history_bytes is not None else "bundled",
            department,
        )
        cached = st.session_state.get("uploaded_dataset")
        if cached is None or cached[0] != file_token:
            try:
                dataset = parse_uploaded_dataset(
                    catalog_bytes if catalog_bytes is not None else COURSE_PLANS_PATH.read_bytes(),
                    history_bytes if history_bytes is not None else ALL_LECTURES_PATH.read_bytes(),
                    department or None,
                )
            except ValueError as error:
                st.error(str(error))
                return None
            st.session_state.uploaded_dataset = (file_token, dataset)
        else:
            dataset = cached[1]

        with st.expander("Elective requirements"):
            minimums = {
                category: st.number_input(
                    category, min_value=0, value=default, max_value=100, key=f"quota_{file_token}_{category}"
                )
                for category, default in ELECTIVE_MINIMUMS.items()
                if category in set(dataset.catalog["Type"])
            }
        token = (*file_token, tuple(minimums.items()))

    if st.session_state.get("active_dataset") != token:
        st.session_state.pop("planner", None)
        st.session_state.pop("result", None)
        st.session_state.active_dataset = token
    return dataset, bundled, minimums


def main() -> None:
    st.set_page_config(page_title="Course Planner", layout="wide")
    st.title("Course Planner")
    st.caption(
        "Generate semester-by-semester schedule options from bundled course history and curriculum data."
    )

    api_url = os.environ.get("COURSE_PLANNER_API_URL")
    if api_url and st.radio(
        "Planner mode", ["Published İTÜ data", "CSV planner"], horizontal=True
    ) == "Published İTÜ data":
        render_published_planner(api_url.rstrip("/"))
        return

    selection = select_dataset()
    if selection is None:
        return
    dataset, bundled, minimums = selection
    course_plan = dataset.catalog
    source_label = "Bundled" if bundled else "Selected"
    st.caption(
        f"{source_label} lecture history: {dataset.first_term}–{dataset.last_term}. "
        "Future sections are projections based on historical timetables."
    )
    course_labels = (
        course_plan.drop_duplicates(subset=["Course Code"])
        .set_index("Course Code")["Course Name"]
        .to_dict()
    )

    curriculum = Curriculum.from_frame(course_plan, minimums)
    grades = {
        grade
        for course in curriculum.courses.values()
        for grade in minimum_grades(course.prerequisite)
    }
    for grade in grades:
        course_labels.setdefault(grade.code, "External prerequisite")
    higher_grade_options = {
        grade: sorted({item.code for item in grades if item.grade == grade})
        for grade in GRADE_ORDER[GRADE_ORDER.index("DD") + 1:]
        if any(item.grade == grade for item in grades)
    }
    session: PlanningSession | None = st.session_state.get("planner")
    if bundled and st.button("Load example plan", help="Use a simulated transcript with two courses left."):
        request = example_request(curriculum)
    else:
        request = render_configuration_form(course_labels, higher_grade_options, session)
    if request is not None:
        st.session_state.planner = PlanningSession(request)
        st.session_state.result = None
        st.rerun()

    if session is None:
        st.info("Select completed courses and planning dates, then calculate schedule options.")
        return

    completed_grades = session.request.completed_grades
    progress = curriculum.progress(set(completed_grades), session.confirmed_codes)
    remaining_count = sum(row.remaining for row in progress)
    st.subheader("Graduation Progress")
    status_col, remaining_col = st.columns(2)
    status_col.metric(
        "Current Grade Level", calculate_current_grade_level(course_plan, completed_grades)
    )
    remaining_col.metric("Remaining Required Courses and Electives", remaining_count)
    st.table([
        {
            "Requirement": row.category,
            "Completed": row.completed,
            "Confirmed in plan": row.planned,
            "Remaining": row.remaining,
        }
        for row in progress
    ])

    if session.confirmed:
        st.subheader("Confirmed Schedules")
    for confirmed in session.confirmed:
        if confirmed.sections:
            visualize_schedule(
                confirmed.sections, course_labels, title=f"Selected schedule for {confirmed.term}"
            )
        else:
            st.write(f"No courses selected for {confirmed.term}.")

    render_calendar_download(session.confirmed, course_labels)

    if not remaining_count:
        st.success("All remaining curriculum requirements have been scheduled or completed.")
        return

    current_term = session.current_term
    if current_term.position > session.request.end.position:
        st.warning("The planning window has ended with outstanding curriculum requirements.")
        return

    if st.session_state.result is None:
        with st.spinner("Generating schedule options..."):
            try:
                st.session_state.result = generate_schedule_options(
                    dataset.sections,
                    session.remaining_request(),
                    curriculum=curriculum,
                    confirmed_courses=session.confirmed_codes,
                )
            except RuntimeError as error:
                st.error(str(error))
                return
    result: PlanResult = st.session_state.result
    st.subheader(f"Planning for {current_term}")
    if result.status in {"time_limit", "interrupted"}:
        reason = "reached its time limit" if result.status == "time_limit" else "stopped early"
        detail = (
            "Showing the feasible options found so far."
            if result.options else "No feasible option was found before the search stopped."
        )
        st.warning(f"The planner {reason}. {detail}")
        if not result.options:
            return
    if result.issues:
        issues = "\n".join(f"- {issue}" for issue in result.issues)
        st.warning(f"No feasible schedule was found.\n\n{issues}")
        return

    st.caption("Prerequisites scheduled in earlier terms are conditional on earning their minimum grades.")
    render_schedule_options(
        course_labels,
        {code for code, course in curriculum.courses.items() if course.category in ELECTIVE_MINIMUMS},
        session, result.options,
    )


def render_published_planner(api_url: str) -> None:
    if st.button("Refresh published data") or st.session_state.get("published_catalogue") is None:
        try:
            response = httpx.get(f"{api_url}/v1/catalogue", timeout=8)
            response.raise_for_status()
            st.session_state.published_catalogue = Catalogue.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as error:
            st.error(_api_error(error))
            return
    catalogue: Catalogue = st.session_state.published_catalogue
    ids = tuple(snapshot.id for snapshot in catalogue.snapshots)
    token = ("published", ids)
    if st.session_state.get("active_dataset") != token:
        st.session_state.pop("planner", None)
        st.session_state.result = None
        st.session_state.active_dataset = token
    latest = catalogue.snapshots[-1]
    st.caption(f"Latest published term: {latest.term} · snapshot {latest.id} · observed {latest.observed_at:%Y-%m-%d %H:%M %Z}")
    english_eligible = st.checkbox("I have verified my ING 100 English proficiency eligibility")
    if st.session_state.get("published_english_eligible") != english_eligible:
        st.session_state.published_english_eligible = english_eligible
        st.session_state.pop("planner", None)
        st.session_state.result = None
    session: PlanningSession | None = st.session_state.get("planner")
    request = render_configuration_form(
        catalogue.course_labels, catalogue.higher_grades, session, initial_term=latest.term
    )
    if request is not None:
        st.session_state.planner = PlanningSession(request)
        st.session_state.result = None
        st.rerun()
    if session is None:
        st.info("Enter your completed courses and planning window to calculate with published data.")
        return
    if session.confirmed:
        st.subheader("Confirmed Schedules")
        for confirmed in session.confirmed:
            if confirmed.sections:
                visualize_schedule(confirmed.sections, catalogue.course_labels, title=str(confirmed.term))
        render_calendar_download(session.confirmed, catalogue.course_labels)

    if session.current_term.position > session.request.end.position:
        if st.session_state.get("published_requirements_met"):
            st.success("All curriculum requirements have been completed or scheduled.")
        else:
            st.warning("The planning window has ended. Review the confirmed schedule above.")
        return
    if st.session_state.result is None:
        payload = {
            **asdict(session.remaining_request()),
            "confirmed_courses": sorted(session.confirmed_codes),
            "snapshot_ids": ids,
            "english_eligible": english_eligible,
        }
        with st.spinner("Calculating against the selected data version..."):
            try:
                response = httpx.post(f"{api_url}/v1/plans", json=payload, timeout=25)
                response.raise_for_status()
                st.session_state.result = PlanResponse.model_validate(response.json())
            except (httpx.HTTPError, ValueError) as error:
                st.error(_api_error(error))
                return
    result: PlanResponse = st.session_state.result
    st.metric("Curriculum requirements satisfied or confirmed", f"{result.confirmed_count}/{result.required_count}")
    st.table([
        {"Term": str(item.term), "Data": item.evidence, "Source term": str(item.source_term or ""),
         "Snapshot": item.snapshot_id}
        for item in result.coverage
    ])
    if result.confirmed_count == result.required_count:
        st.success("All curriculum requirements have been completed or scheduled.")
        return
    if result.issues:
        issues = "\n".join(f"- **{item.code}** · {item.message}" for item in result.issues)
        st.warning(f"No complete plan fits this window.\n\n{issues}")
    elif result.status in {"time_limit", "interrupted"}:
        st.warning(f"Search status: {result.status}. Showing any feasible options found so far.")
    if result.options:
        render_schedule_options(
            catalogue.course_labels, catalogue.elective_codes, session, result.options
        )


def _api_error(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        try:
            detail = error.response.json().get("detail", {})
            if isinstance(detail, dict) and detail.get("message"):
                return detail["message"]
            if isinstance(detail, list) and detail:
                return detail[0].get("msg", "The planning request is invalid.")
        except ValueError:
            pass
    return "The planning API is unavailable or returned an unexpected response."


def render_calendar_download(
    confirmed_terms: tuple[ConfirmedTerm, ...], course_labels: dict[str, str]
) -> None:
    terms = [confirmed for confirmed in confirmed_terms if any(
        section.section.meetings for section in confirmed.sections
    )]
    if not terms:
        return
    with st.expander("Export a confirmed timetable (.ics)"):
        confirmed = st.selectbox("Confirmed term", terms, format_func=lambda item: str(item.term))
        st.caption("Choose the actual teaching dates. Events repeat weekly in Istanbul time.")
        left, right = st.columns(2)
        first_day = left.date_input("First teaching day", value=None)
        last_day = right.date_input("Last teaching day", value=None)
        if first_day is None or last_day is None:
            return
        try:
            calendar = export_calendar(confirmed, course_labels, first_day, last_day)
        except ValueError as error:
            st.warning(str(error))
            return
        st.download_button(
            "Download calendar",
            calendar,
            file_name=f"course-plan-{confirmed.term.year}-{confirmed.term.season.lower()}.ics",
            mime="text/calendar",
            on_click="ignore",
        )


def example_request(curriculum: Curriculum) -> PlanRequest:
    remaining = {"MAT 103E", "MAT 104E"}
    completed = {code: "DD" for code in curriculum.remaining_required(set()) - remaining}
    for category, count in ELECTIVE_MINIMUMS.items():
        codes = sorted(code for code, course in curriculum.courses.items() if course.category == category)
        completed.update(dict.fromkeys(codes[:count], "DD"))
    current = current_planning_term()
    spring = Term(current.year + (current.season != "Spring"), "Spring")
    return PlanRequest(spring, Term(spring.year, "Fall"), completed)


def render_configuration_form(
    course_labels: dict[str, str],
    higher_grade_options: dict[str, list[str]],
    session: PlanningSession | None,
    initial_term: Term | None = None,
) -> PlanRequest | None:
    start = session.request.start if session else (initial_term or current_planning_term())
    end = session.request.end if session else start
    completed = session.request.completed_grades if session else {}
    available_years = list(range(min(2024, start.year), max(2034, start.year, end.year) + 2))

    with st.form(f"planner_controls_{st.session_state.active_dataset}"):
        selected_completed_courses = st.multiselect(
            "Select completed courses",
            options=sorted(course_labels),
            default=list(completed),
            help="Selected courses count as DD or higher unless a higher grade is marked below.",
            format_func=lambda code: f"{code} - {course_labels.get(code, code)}",
        )

        grade_overrides = {}
        for grade, options in higher_grade_options.items():
            selected = st.multiselect(
                f"Completed prerequisites with a grade of {grade} or higher",
                options=options,
                default=[code for code in options if completed.get(code) == grade],
                format_func=lambda code: f"{code} - {course_labels[code]}",
            )
            grade_overrides.update(dict.fromkeys(selected, grade))

        col1, col2 = st.columns(2)
        with col1:
            current_year = st.selectbox(
                "Select the current year",
                options=available_years,
                index=available_years.index(start.year),
            )
        with col2:
            current_semester = st.selectbox(
                "Select the current semester",
                options=SEMESTER_LST,
                index=SEMESTER_LST.index(start.season),
            )

        col1, col2 = st.columns(2)
        with col1:
            last_year = st.selectbox(
                "Select the year you want to finish",
                options=available_years,
                index=available_years.index(end.year),
            )
        with col2:
            last_semester = st.selectbox(
                "Select the final semester",
                options=SEMESTER_LST,
                index=SEMESTER_LST.index(end.season),
            )

        credit_col, day_col, summer_col = st.columns(3)
        max_credits = credit_col.number_input(
            "Maximum credits per term",
            min_value=1,
            value=session.request.max_credits if session else None,
            help="Leave blank to keep only the ten-course cap.",
        )
        day = session.request.preferred_free_day if session else None
        preferred_free_day = day_col.selectbox(
            "Preferred free day",
            options=[None, *WEEKDAYS],
            index=WEEKDAYS.index(day) + 1 if day else 0,
            format_func=lambda day: day or "No preference",
            help="A preference used to rank otherwise equivalent plans.",
        )
        allow_summer = summer_col.checkbox(
            "Allow summer terms", value=session.request.allow_summer if session else True
        )

        submitted = st.form_submit_button("Calculate")

    if not submitted:
        return

    if set(grade_overrides) - set(selected_completed_courses):
        st.error("Add each course with a grade override to the completed courses list.")
        return

    completed = {
        code: grade_overrides.get(code, "DD")
        for code in selected_completed_courses
    }
    try:
        return PlanRequest(
            Term(current_year, current_semester),
            Term(last_year, last_semester),
            completed,
            max_credits=max_credits,
            preferred_free_day=preferred_free_day,
            allow_summer=allow_summer,
        )
    except ValueError as error:
        st.error(str(error))
        return None


def calculate_current_grade_level(
    course_plan: pd.DataFrame, completed_grades: Mapping[str, str]
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
    elective_codes: set[str],
    session: PlanningSession,
    options: list[list[ScheduledSection]],
) -> None:
    current_term = session.current_term
    term_options = [[section for section in plan if section.term == current_term] for plan in options]
    st.subheader("Compare Schedule Options")
    st.caption("Compare full paths below. Confirming an option saves only the current term.")
    choice_ids = range(len(options))
    hours = schedule_hours([section for term in term_options for section in term])
    for side, column in enumerate(st.columns(min(2, len(options)))):
        with column:
            if len(options) == 1:
                choice = 0
            else:
                choice = st.selectbox(
                    f"Option to compare ({'left' if side == 0 else 'right'})",
                    options=choice_ids,
                    index=side,
                    format_func=lambda index: f"Option {index + 1}",
                    key=f"compare_{current_term}_{side}",
                )
            show_option(options[choice], current_term, elective_codes, course_labels, hours)

    selected_option = st.radio(
        "Select your preferred schedule",
        options=range(len(term_options)),
        format_func=lambda index: f"Option {index + 1}",
        key=f"radio_{current_term}",
    )
    if st.button(
        f"Confirm {current_term}",
        key=f"confirm_{current_term}",
    ):
        confirmed = session.confirm(term_options[selected_option])
        st.session_state.planner = confirmed
        if isinstance(st.session_state.result, PlanResponse):
            st.session_state.published_requirements_met = all(
                section.term == current_term for section in options[selected_option]
            )
        st.session_state.result = None
        st.rerun()


def show_option(
    option: list[ScheduledSection],
    current_term: Term,
    elective_codes: set[str],
    course_labels: dict[str, str],
    hours: tuple[int, int],
) -> None:
    by_term = {}
    for section in option:
        by_term.setdefault(section.term, []).append(section)
    current = by_term.get(current_term, [])
    if current:
        visualize_schedule(
            current, course_labels, title=f"Weekly schedule for {current_term}", hours=hours
        )
    else:
        st.info("No courses scheduled for this term.")
    if by_term:
        st.table([
            {
                "Term": str(term),
                "Courses": ", ".join(sorted(section.code for section in sections)),
                "Electives": sum(section.code in elective_codes for section in sections),
                "Data": ", ".join(sorted({
                    "forecast" if section.section.published_term != section.term else "published"
                    for section in sections if section.section.published_term is not None
                })) or "historical projection",
            }
            for term, sections in by_term.items()
        ])
