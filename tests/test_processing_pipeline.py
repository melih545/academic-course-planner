from __future__ import annotations

from pathlib import Path

import pandas as pd

from course_planner.preprocessing import process_uploaded_files


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_process_uploaded_files_keeps_expected_columns_and_split_rows() -> None:
    course_plan = pd.read_csv(FIXTURE_DIR / "course_plans_sample.csv")

    processed = process_uploaded_files(
        FIXTURE_DIR / "all_lectures_sample.csv",
        course_plan,
    )

    assert len(processed) == 3
    assert set(processed["Code"]) == {"END 101E", "END 201E"}
    assert {"Code", "CourseName", "Days", "StartTimeFloat", "EndTimeFloat", "Type"}.issubset(
        processed.columns
    )
    assert sorted(processed.loc[processed["Code"] == "END 101E", "Days"].tolist()) == [
        "Pazartesi",
        "Salı",
    ]
    assert processed.loc[processed["Code"] == "END 201E", "Prerequisites"].iloc[0] == "END 101E"
