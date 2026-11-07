from course_planner.curriculum import (
    AllOf,
    Course,
    Curriculum,
    MinimumGrade,
    RequirementProgress,
    parse_prerequisite,
)


def test_prerequisites_extracts_multiple_matching_courses() -> None:
    prereq_text = "MIN DD END 201E and MAT 102E MIN CB and TUR 101 MIN DD"

    assert parse_prerequisite(prereq_text) == AllOf((
        MinimumGrade("END 201E", "DD"),
        MinimumGrade("MAT 102E", "CB"),
        MinimumGrade("TUR 101", "DD"),
    ))


def test_progress_separates_completed_and_planned_and_caps_elective_credit():
    courses = {
        "MAIN 101": Course("Main", None, ""),
        "MAIN 201": Course("Main", None, ""),
        **{f"ELC {i}": Course("Elective Analytic", None, "") for i in range(5)},
    }
    curriculum = Curriculum(courses)
    completed = {"MAIN 101", "ELC 0", "ELC 1", "ELC 2"}
    planned = {"MAIN 101", "MAIN 201", "ELC 3", "ELC 4"}

    assert curriculum.progress(completed, planned) == (
        RequirementProgress("Required courses", 1, 1, 0),
        RequirementProgress("Elective Analytic", 3, 1, 0),
    )
