from __future__ import annotations

import pandas as pd

from course_planner.preprocessing import (
    check_conflict,
    prerequisites,
    split_years,
    time_to_float,
)


def test_time_to_float_parses_half_hours() -> None:
    assert time_to_float("0830") == 8.5


def test_prerequisites_extracts_multiple_matching_courses() -> None:
    prereq_text = "MIN DD END 201E and MAT 102E and TUR 101"

    assert prerequisites(prereq_text) == "END 201E, MAT 102E, TUR 101"


def test_split_years_normalizes_term_and_year() -> None:
    term_data = pd.DataFrame(
        {
            "Term": [
                "2021 - 2022 Güz Dönemi",
                "2021 - 2022 Bahar Dönemi",
                "2022 - 2023 Yaz Dönemi",
            ]
        }
    )

    result = split_years(term_data)

    assert result["Year"].tolist() == [2021, 2022, 2023]
    assert result["Term"].tolist() == ["Fall", "Spring", "Summer"]


def test_check_conflict_detects_overlapping_slots() -> None:
    row1 = {
        "Days": "Pazartesi",
        "Term": "Fall",
        "StartTimeFloat": 8.5,
        "EndTimeFloat": 10.5,
    }
    row2 = {
        "Days": "Pazartesi",
        "Term": "Fall",
        "StartTimeFloat": 10.0,
        "EndTimeFloat": 11.0,
    }

    assert check_conflict(row1, row2) is True
