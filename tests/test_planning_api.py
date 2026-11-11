from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from course_planner import planning_api
from course_planner.curriculum import (
    Course,
    Curriculum,
    MinimumGrade,
    RequirementGroup,
    UnresolvedPrerequisite,
)
from course_planner.planning_data import PlanningData, PublishedSnapshot
from course_planner.schedule import Meeting, Section, Term

FALL = Term(2026, "Fall")
NEXT_FALL = Term(2027, "Fall")
SPRING = Term(2027, "Spring")
CODES = ("MEK 205E", "MEK 205")


def _data(rule=None, *, section=True, incomplete=False):
    courses = {code: Course("Main", rule, "MAT 104E MIN. DD") for code in CODES}
    return PlanningData(
        (PublishedSnapshot(7, FALL, datetime(2026, 11, 8, tzinfo=UTC), ("https://example.org",)),),
        Curriculum(courses, {}, (RequirementGroup("Mechanics", CODES),)),
        dict(zip(CODES, ("Mechanics EN", "Mechanics TR"))),
        {FALL: (Section(1, CODES[0], "Fall", (Meeting("Salı", 540, 600),), "", FALL, 12345),) if section else ()},
        {FALL: set(CODES) if incomplete else set()},
    )


def _payload(term):
    date = {"year": term.year, "season": term.season}
    return {"start": date, "end": date, "snapshot_ids": [7]}


def test_fastapi_validates_input_and_preserves_published_crn_and_forecast_provenance(monkeypatch):
    def read_dataset(snapshot_ids=()):
        assert snapshot_ids in {(), (7,)}
        return _data()

    monkeypatch.setattr(planning_api, "read_dataset", read_dataset)
    with TestClient(planning_api.app) as client:
        assert client.get("/v1/catalogue").json()["snapshots"][0]["id"] == 7
        exact = client.post("/v1/plans", json=_payload(FALL)).json()
        assert exact["status"] == "solved"
        assert exact["coverage"][0]["evidence"] == "published"
        assert exact["options"][0][0]["section"]["crn"] == 12345
        assert len(exact["options"][0]) == 1
        forecast = client.post("/v1/plans", json=_payload(NEXT_FALL)).json()
        assert forecast["coverage"][0]["evidence"] == "forecast"
        assert forecast["options"][0][0]["section"]["published_term"]["year"] == 2026
        assert forecast["options"][0][0]["term"]["year"] == 2027
        bad = client.post("/v1/plans", json={**_payload(FALL), "max_credits": 0})
        assert bad.status_code == 422


def test_plan_separates_missing_data_no_section_and_unmet_prerequisites():
    for data, term, expected in (
        (_data(), SPRING, "data_missing"),
        (_data(section=False), FALL, "section_not_offered"),
        (_data(section=False, incomplete=True), FALL, "data_missing"),
        (_data(MinimumGrade("MAT 104E", "DD")), FALL, "prerequisite_unmet"),
        (_data(UnresolvedPrerequisite("Unknown")), FALL, "data_missing"),
    ):
        response = planning_api.solve_plan(data, planning_api.PlanInput.model_validate(_payload(term)))
        assert response.status == "infeasible"
        assert [issue.code for issue in response.issues] == [expected]


def test_completed_course_cannot_fill_two_published_requirements():
    data = _data()
    original = data.curriculum.requirement_groups[0]
    data.curriculum = Curriculum(data.curriculum.courses, {}, (original, original))
    plan = planning_api.PlanInput.model_validate({
        **_payload(FALL), "completed_grades": {CODES[0]: "AA"},
    })

    result = planning_api.solve_plan(data, plan)

    assert result.completed_count == 1
    assert result.status == "infeasible"
    assert result.issues[0].code == "constraints"


def test_published_term_without_sections_never_uses_an_older_same_season():
    data = _data()
    data.snapshots += (PublishedSnapshot(8, NEXT_FALL, data.snapshots[0].observed_at, ()),)
    data.sections[NEXT_FALL] = ()
    result = planning_api.solve_plan(data, planning_api.PlanInput.model_validate(_payload(NEXT_FALL)))
    assert result.coverage[0].snapshot_id == 8
    assert result.issues[0].code == "section_not_offered"


def test_term_schedule_does_not_use_forecast_sections():
    plan = planning_api.PlanInput.model_validate({
        **_payload(NEXT_FALL), "planning_goal": "term",
    })

    result = planning_api.solve_plan(_data(), plan)

    assert result.status == "infeasible"
    assert result.coverage[0].evidence == "missing"
    assert result.options == []


def test_partial_graduation_preserves_standing_and_returns_eligible_courses():
    data = _data()
    restricted = "END 449"
    data.curriculum.courses[restricted] = Course("Main", None, "3 earned credits", 0, 3)
    data.curriculum = Curriculum(
        data.curriculum.courses, {},
        (*data.curriculum.requirement_groups, RequirementGroup("Seminar", (restricted,))),
    )
    data.sections[FALL] += (Section(2, restricted, "Fall", (), "", FALL, 54321),)
    payload = {**_payload(FALL), "allow_partial": True}

    partial = planning_api.solve_plan(data, planning_api.PlanInput.model_validate(payload))
    assert partial.status == "partial"
    assert {section.code for section in partial.options[0]} == {CODES[0]}
    assert partial.issues[0].code == "prerequisite_unmet"

    data.curriculum.courses["BIL 100E"] = Course("Main", None, "", 3)
    eligible = planning_api.solve_plan(data, planning_api.PlanInput.model_validate({
        **payload, "completed_grades": {"BIL 100E": "DD"},
    }))
    assert eligible.status == "solved"
    assert {section.code for section in eligible.options[0]} == {CODES[0], restricted}


def test_streamlit_published_mode_plans_a_term_with_no_completed_courses(monkeypatch):
    import httpx
    from streamlit.testing.v1 import AppTest

    data = _data()
    blocked = "END 311E"
    data.labels[blocked] = "Statistics"
    data.curriculum.courses[blocked] = Course("Main", MinimumGrade("MAT 104E", "DD"), "MAT 104E MIN. DD")
    data.curriculum = Curriculum(
        data.curriculum.courses, {},
        (*data.curriculum.requirement_groups, RequirementGroup("Statistics", (blocked,))),
    )
    data.sections[FALL] += (Section(2, blocked, "Fall", (), "", FALL, 54321),)

    monkeypatch.setenv("COURSE_PLANNER_API_URL", "http://planner-api.test")
    monkeypatch.setattr(planning_api, "read_dataset", lambda ids=(): data)
    with TestClient(planning_api.app) as client:
        graduation = client.post("/v1/plans", json=_payload(FALL)).json()
        assert graduation["status"] == "infeasible"
        assert [issue["code"] for issue in graduation["issues"]] == ["prerequisite_unmet"]
        monkeypatch.setattr(httpx, "get", lambda url, **_: client.get(url))
        monkeypatch.setattr(httpx, "post", lambda url, **kwargs: client.post(url, json=kwargs["json"]))
        ui = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
        assert not ui.exception
        assert any("Latest published term: 2026 Fall" in item.value for item in ui.caption)
        next(button for button in ui.button if button.label == "Calculate").click().run()
        assert not ui.exception
        assert not ui.warning
        assert {item.code for item in ui.session_state.result.options[0]} == {CODES[0]}
        next(button for button in ui.button if button.label == "Confirm 2026 Fall").click().run()
        assert not ui.exception
        assert [item.value for item in ui.success] == ["Your timetable for 2026 Fall is confirmed."]


def test_successful_plan_exposes_counter_and_duration_metrics(monkeypatch):
    from prometheus_client import generate_latest

    from course_planner.telemetry import PLAN_RUNS, REGISTRY

    monkeypatch.setattr(planning_api, "scrape_metrics", lambda: generate_latest(REGISTRY))
    monkeypatch.setattr(planning_api, "read_dataset", lambda ids=(): _data())
    before = REGISTRY.get_sample_value("course_planner_plans_total", {"status": "solved"}) or 0
    PLAN_RUNS.labels("solved")
    with TestClient(planning_api.app) as client:
        assert client.post("/v1/plans", json=_payload(FALL)).status_code == 200
        output = client.get("/metrics")
    assert f'course_planner_plans_total{{status="solved"}} {before + 1}' in output.text
    assert "course_planner_solve_duration_seconds_count " in output.text
