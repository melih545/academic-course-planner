from course_planner.curriculum import AllOf, MinimumGrade, parse_prerequisite


def test_prerequisites_extracts_multiple_matching_courses() -> None:
    prereq_text = "MIN DD END 201E and MAT 102E MIN CB and TUR 101 MIN DD"

    assert parse_prerequisite(prereq_text) == AllOf((
        MinimumGrade("END 201E", "DD"),
        MinimumGrade("MAT 102E", "CB"),
        MinimumGrade("TUR 101", "DD"),
    ))
