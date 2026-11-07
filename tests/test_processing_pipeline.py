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
    assert processed.loc[processed["Code"] == "END 201E", "Prerequisites"].iloc[0] == "MIN DD END 101E"
    first_course = processed.loc[processed["Code"] == "END 101E"]
    assert first_course["SectionId"].nunique() == 1
    assert first_course["CRN"].tolist() == [10002, 10002]


def test_sections_keep_all_meetings_without_combining_different_crns(tmp_path):
    raw = pd.read_csv(FIXTURE_DIR / "all_lectures_sample.csv").iloc[0].to_dict()
    raw["Gün"], raw["Saat"] = "Pazartesi", "0830/0929"
    rows = [
        raw,
        {**raw, "Gün": "Salı", "Bölüm sınırlaması": None},
        {**raw, "CRN": 10005},
        {**raw, "CRN": 10005, "Gün": "Çarşamba"},
    ]
    history = tmp_path / "history.csv"
    pd.DataFrame(rows).to_csv(history, index=False)
    catalog = pd.read_csv(FIXTURE_DIR / "course_plans_sample.csv")

    processed = process_uploaded_files(history, catalog)

    assert processed.groupby("CRN")["SectionId"].nunique().to_dict() == {10001: 1, 10005: 1}
    assert processed.groupby("CRN")["Days"].apply(set).to_dict() == {
        10001: {"Pazartesi", "Salı"},
        10005: {"Pazartesi", "Çarşamba"},
    }
