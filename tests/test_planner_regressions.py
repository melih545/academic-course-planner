from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from course_planner import optimization
from course_planner.curriculum import Curriculum
from course_planner.optimization import generate_schedule_options
from course_planner.schedule import Meeting, PlanRequest, Section, Term

ROOT = Path(__file__).resolve().parents[1]


def meeting(code, section_id, day="Pazartesi", term="Spring", start=9.0, end=10.0):
    return {
        "Code": code,
        "SectionId": section_id,
        "Days": day,
        "Term": term,
        "StartTimeFloat": start,
        "EndTimeFloat": end,
    }


def plan(rows, courses, completed=None, end_term="Spring", **kwargs):
    catalog = pd.DataFrame(courses, columns=["Course Code", "Type", "Prerequisites"])
    sections = {}
    for row in rows:
        key = (row["SectionId"], row["Code"], row["Term"])
        sections.setdefault(key, []).append(
            Meeting(row["Days"], round(row["StartTimeFloat"] * 60), round(row["EndTimeFloat"] * 60))
        )
    return generate_schedule_options(
        [Section(*key, tuple(meetings)) for key, meetings in sections.items()],
        PlanRequest(Term(2026, "Spring"), Term(2026, end_term), completed or {}),
        curriculum=Curriculum.from_frame(catalog),
        **kwargs,
    )


def test_conflict_with_second_section_meeting_rules_out_the_section():
    rows = [
        meeting("END 101E", 101),
        meeting("END 101E", 101, day="Salı"),
        meeting("END 201E", 201, day="Salı", start=9.5, end=10.5),
    ]
    courses = [("END 101E", "Main", None), ("END 201E", "Main", None)]

    result = plan(rows, courses)
    assert not result.options
    assert "timetable conflicts" in result.issues[0]

    rows.append(meeting("END 101E", 102, day="Çarşamba"))
    result = plan(rows, courses)
    assert [{section.section.id for section in option} for option in result.options] == [{102, 201}]

    rows[2]["StartTimeFloat"] = 10.0
    result = plan(rows, courses)
    assert {101, 201} in [{section.section.id for section in option} for option in result.options]


def test_unavailable_prerequisite_blocks_the_dependent_course():
    rows = [meeting("END 201E", 201)]
    courses = [("END 201E", "Main", "BIL 100E MIN DD")]

    result = plan(rows, courses)

    assert not result.options
    assert "BIL 100E" in result.issues[0]
    assert plan(rows, courses, {"BIL 100E": "DD"}).options


def test_prerequisite_alternatives_preserve_groups_and_minimum_grades():
    rows = [meeting("END 201E", 201)]
    courses = [("END 201E", "Main", "(BIL 100E MIN DD veya BIL 110E MIN DD) ve MAT 101E MIN BB")]

    assert plan(rows, courses, {"BIL 110E": "DD", "MAT 101E": "BA"}).options
    assert not plan(rows, courses, {"BIL 110E": "DD", "MAT 101E": "CB"}).options
    assert not plan(rows, courses, {"MAT 101E": "BA"}).options


def test_prerequisites_are_scheduled_in_an_earlier_term():
    rows = [
        meeting("BIL 100E", 100),
        meeting("END 201E", 201, term="Summer"),
    ]
    courses = [
        ("BIL 100E", "Main", None),
        ("END 201E", "Main", "BIL 100E MIN DD veya BIL 110E MIN DD"),
    ]

    result = plan(rows, courses, end_term="Summer")

    assert [(section.code, section.term.season) for section in result.options[0]] == [
        ("BIL 100E", "Spring"), ("END 201E", "Summer"),
    ]
    rows[0]["Term"] = "Summer"
    assert not plan(rows, courses, end_term="Summer").options


def test_solver_cannot_use_an_unscheduled_prerequisite_alternative():
    rows = [
        meeting("BIL 100E", 100),
        meeting("MAT 101E", 101),
        meeting("END 201E", 201, term="Summer"),
    ]
    courses = [
        ("BIL 100E", "Elective Analytic", None),
        ("MAT 101E", "Main", None),
        ("END 201E", "Main", "BIL 100E MIN DD veya BIL 110E MIN DD"),
        *((f"END {code}E", "Elective Analytic", None) for code in range(301, 305)),
    ]
    completed = {f"END {code}E": "DD" for code in range(301, 305)}

    result = plan(rows, courses, completed, end_term="Summer")

    assert not result.options
    assert "timetable conflicts" in result.issues[0]


def test_unreadable_prerequisite_is_reported():
    result = plan(
        [meeting("ING 100", 100)],
        [("ING 100", "Main", "Önşartı için tıklayınız.")],
    )

    assert not result.options
    assert "prerequisite information is unavailable or unreadable" in result.issues[0]


def test_required_course_outside_window_and_missing_course_are_reported():
    rows = [meeting("END 101E", 101, term="Fall")]
    courses = [("END 101E", "Main", None), ("END 201E", "Main", None)]

    result = plan(rows, courses)

    assert not result.options
    assert [issue.split(":")[0] for issue in result.issues] == ["END 101E", "END 201E"]


def test_electives_are_counted_from_catalog_and_completed_or_confirmed_courses():
    rows = [meeting("END 305E", 305)]
    courses = [(f"END {code}E", "Elective Analytic", None) for code in range(301, 306)]
    completed = {"END 301E": "DD", "END 302E": "DD"}

    result = plan(rows, courses, completed)
    assert not result.options
    assert "2 more required, but only 1" in result.issues[0]

    result = plan(rows, courses, completed, confirmed_courses={"END 303E"})
    assert [[section.code for section in option] for option in result.options] == [["END 305E"]]
    assert plan(rows, courses, completed, confirmed_courses={"END 303E", "END 304E"}).options == [[]]


def test_time_limit_without_an_option_is_distinct_from_infeasibility(monkeypatch):
    times = iter((0.0, 9.0))
    monkeypatch.setattr(optimization, "monotonic", lambda: next(times))

    result = plan([meeting("END 101E", 101)], [("END 101E", "Main", None)])

    assert result.status == "time_limit"
    assert not result.options


def test_time_limit_keeps_feasible_options_already_found(monkeypatch):
    times = iter((0.0, 1.0, 9.0))
    monkeypatch.setattr(optimization, "monotonic", lambda: next(times))

    result = plan(
        [meeting("END 101E", 101), meeting("END 101E", 102, day="Salı")],
        [("END 101E", "Main", None)],
    )

    assert result.status == "time_limit"
    assert len(result.options) == 1


def test_default_calculate_explains_why_the_plan_cannot_be_built():
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not app.exception

    app.button[0].click().run()

    assert not app.exception
    assert any("DAN 102" in warning.value for warning in app.warning)
    assert not app.success


def test_calculate_and_confirm_a_feasible_plan_with_bundled_data():
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    catalog = pd.read_csv(ROOT / "data" / "course_plans.csv")
    app.multiselect[0].set_value(sorted(set(catalog["Course Code"]) - {"MAT 103E", "MAT 104E"}))
    app.selectbox[2].select(2025)
    app.button[0].click().run()

    assert app.button[-1].label == "Confirm 2024 Spring"
    for _ in range(3):
        assert not app.exception
        assert not app.warning
        if app.success:
            break
        assert app.button[-1].label.startswith("Confirm 2024 ")
        app.button[-1].click().run()

    assert not app.exception
    assert app.success
    assert [section.code for section in app.session_state.planner.confirmed[0].sections] == ["MAT 103E"]
    assert [
        section.code
        for confirmed in app.session_state.planner.confirmed[1:]
        for section in confirmed.sections
    ] == ["MAT 104E"]
