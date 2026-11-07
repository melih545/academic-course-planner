from __future__ import annotations

import pandas as pd

from course_planner.preprocessing import (
    split_years,
    time_to_float,
)


def test_time_to_float_parses_half_hours() -> None:
    assert time_to_float("0830") == 8.5


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
