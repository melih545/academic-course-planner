from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .constants import SEMESTER_LST


PREREQUISITE_PATTERN = re.compile(
    r"\b(END \d+E|MAT \d+E|ATA \d+|TUR \d+|FIZ \d+EL|FIZ \d+E|KIM \d+EL|KIM \d+E)\b"
)


def to_semesters(term: str) -> str | None:
    if "Yaz" in term:
        return "Summer"
    if "Güz" in term:
        return "Fall"
    if "Bahar" in term:
        return "Spring"
    return None


def split_years(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    year_parts = df["Term"].str.extract(r"(\d{4}) - (\d{4})")
    semester_names = df["Term"].str.extract(r"(Güz|Bahar|Yaz)")[0]

    df["Year"] = year_parts[0].where(semester_names.eq("Güz"), year_parts[1]).astype(int)
    df["Term"] = semester_names.apply(to_semesters)
    return df


def split_days_and_times(row: pd.Series) -> list[pd.Series]:
    row = row.copy()

    if (
        row["Days"] == "---"
        or row["Times"] == "/"
        or pd.isna(row["Days"])
        or pd.isna(row["Times"])
        or row["Instructors"] == "--"
    ):
        if "490" not in row["Code"]:
            return []
        row["Days"] = "Pazartesi"
        row["Times"] = "0000/0001"

    days = str(row["Days"]).split()
    times = str(row["Times"]).split()

    if len(days) != len(times):
        times = times * len(days)

    expanded_rows: list[pd.Series] = []
    for day, time in zip(days, times):
        expanded_row = row.copy()
        expanded_row["Days"] = day
        expanded_row["Times"] = time
        expanded_rows.append(expanded_row)

    return expanded_rows


def time_to_float(time_str: str) -> float:
    hours = int(time_str[:2])
    minutes = int(time_str[2:])
    return hours + minutes / 60


def check_conflict(row1: pd.Series | dict, row2: pd.Series | dict) -> bool:
    return (
        row1["Days"] == row2["Days"]
        and row1["Term"] == row2["Term"]
        and (
            (
                row1["StartTimeFloat"] < row2["EndTimeFloat"]
                and row1["EndTimeFloat"] > row2["StartTimeFloat"]
            )
            or (
                row2["StartTimeFloat"] < row1["EndTimeFloat"]
                and row2["EndTimeFloat"] > row1["StartTimeFloat"]
            )
            or (row1["StartTimeFloat"] == row2["StartTimeFloat"])
            or (row1["EndTimeFloat"] == row2["EndTimeFloat"])
        )
    )


def prerequisites(pre: object) -> str | None:
    if pd.isna(pre):
        return None

    pre_text = str(pre).strip()
    if not pre_text or "MIN" not in pre_text:
        return None

    matches = PREREQUISITE_PATTERN.findall(pre_text)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return ", ".join(matches)


def calculate_take_probability(df: pd.DataFrame) -> pd.DataFrame:
    probability_df = df.copy()
    probability_df["Registered"] = pd.to_numeric(
        probability_df["Registered"], errors="coerce"
    )
    probability_df["Quota"] = pd.to_numeric(probability_df["Quota"], errors="coerce")
    probability_df["can_take"] = (probability_df["Quota"] > probability_df["Registered"]).astype(int)
    grouped_df = (
        probability_df.groupby(["Term", "CourseName"])["can_take"].mean().reset_index()
    )
    grouped_df.rename(columns={"can_take": "probability_to_take"}, inplace=True)
    return grouped_df


def calculate_open_rate(data: pd.DataFrame) -> pd.DataFrame:
    total_semester_counts = data.groupby("Term")["Year"].nunique().reset_index()
    total_semester_counts = total_semester_counts.rename(columns={"Year": "Total Semesters"})

    course_counts = (
        data.groupby(["Term", "Code"])["Year"]
        .nunique()
        .reset_index()
        .rename(columns={"Year": "Course Count"})
    )

    course_counts = course_counts.merge(total_semester_counts, on="Term")
    course_counts["Open Rate"] = (
        course_counts["Course Count"] / course_counts["Total Semesters"]
    )

    return course_counts


def filter_recent_lectures(data: pd.DataFrame, current_year: int) -> pd.DataFrame:
    data = data.copy()
    recent_years = list(range(current_year - 2, current_year + 1))
    data["Year"] = pd.to_numeric(data["Year"], errors="coerce")

    recent_data = data[
        data["Year"].isin(recent_years) & data["Term"].isin(SEMESTER_LST)
    ].copy()

    if recent_data.empty:
        return data

    recent_data["TermOrder"] = recent_data["Term"].map(
        {term: index for index, term in enumerate(SEMESTER_LST)}
    )
    recent_data["CourseName"] = recent_data["CourseName"].str.replace(
        "&", "and", regex=False
    )

    latest_codes = recent_data.sort_values(
        ["Year", "TermOrder"], ascending=[False, False]
    ).drop_duplicates(subset="CourseName", keep="first")[["CourseName", "Code"]]

    latest_durations = recent_data.sort_values(
        ["Year", "TermOrder"], ascending=[False, False]
    ).drop_duplicates(subset="Code", keep="first")[
        ["Code", "EndTimeFloat", "StartTimeFloat"]
    ]
    latest_durations["Duration"] = (
        latest_durations["EndTimeFloat"] - latest_durations["StartTimeFloat"]
    )
    duration_dict = latest_durations.set_index("Code")["Duration"].to_dict()

    recent_data = recent_data[
        recent_data["Code"].isin(latest_codes["Code"])
        & recent_data.apply(
            lambda row: abs(
                (row["EndTimeFloat"] - row["StartTimeFloat"])
                - duration_dict.get(row["Code"], 0.0)
            )
            < 0.01,
            axis=1,
        )
    ]

    valid_combinations = recent_data[["Code", "Instructors"]].drop_duplicates()
    if valid_combinations.empty:
        return data

    return data.merge(valid_combinations, on=["Code", "Instructors"], how="inner")


def process_uploaded_files(
    uploaded_file: str | Path, necessary_lectures: pd.DataFrame
) -> pd.DataFrame:
    data = pd.read_csv(uploaded_file)
    data.rename(
        columns={
            "Kod": "Code",
            "Ders": "CourseName",
            "Eğitmen(ler)": "Instructors",
            "Gün": "Days",
            "Saat": "Times",
            "Bina": "Building",
            "Kayıtlı": "Registered",
            "Kontenjan": "Quota",
            "Bölüm sınırlaması": "DepartmentRestrictions",
        },
        inplace=True,
    )

    filtered_data = data[
        data["DepartmentRestrictions"].str.contains("ENDE", na=False)
    ].copy()

    lecture_metadata = {
        row["Course Code"]: {
            "Credits": row["Credits"],
            "Prerequisites": row["Prerequisites"],
            "Grade": int((float(str(row["Semester"]).split(".")[0]) + 1) // 2),
            "Type": row["Type"],
        }
        for _, row in necessary_lectures.iterrows()
    }

    filtered_data = filtered_data[
        filtered_data["Code"].isin(necessary_lectures["Course Code"])
    ].copy()
    filtered_data = split_years(filtered_data)

    expanded_rows: list[pd.Series] = []
    for _, row in filtered_data.iterrows():
        row["Credits"] = lecture_metadata[row["Code"]]["Credits"]
        row["Prerequisites"] = prerequisites(
            lecture_metadata[row["Code"]]["Prerequisites"]
        )
        row["Grade"] = lecture_metadata[row["Code"]]["Grade"]
        row["Type"] = lecture_metadata[row["Code"]]["Type"]
        expanded_rows.extend(split_days_and_times(row))

    expanded_data = pd.DataFrame(expanded_rows)
    if expanded_data.empty:
        return expanded_data

    expanded_data["StartTimeFloat"] = (
        expanded_data["Times"].str.split("/").str[0].apply(time_to_float)
    )
    expanded_data["EndTimeFloat"] = (
        expanded_data["Times"].str.split("/").str[1].apply(time_to_float)
    )

    latest_year = pd.to_numeric(expanded_data["Year"], errors="coerce").max()
    reference_year = int(latest_year) if pd.notna(latest_year) else 2023
    expanded_data = filter_recent_lectures(expanded_data, reference_year)
    expanded_data = expanded_data[expanded_data["EndTimeFloat"] <= 17.5].copy()
    expanded_data["Registered"] = pd.to_numeric(
        expanded_data["Registered"], errors="coerce"
    )
    expanded_data["Quota"] = pd.to_numeric(expanded_data["Quota"], errors="coerce")
    expanded_data = expanded_data[
        (expanded_data["Quota"] >= 1)
        | (expanded_data["Code"].str.contains("490", na=False))
    ].copy()

    if expanded_data.empty:
        return expanded_data

    open_rates = calculate_open_rate(expanded_data)
    expanded_data = expanded_data.merge(
        open_rates[["Term", "Code", "Open Rate"]], on=["Term", "Code"], how="left"
    )
    expanded_data = expanded_data.merge(
        calculate_take_probability(expanded_data),
        on=["Term", "CourseName"],
        how="left",
    )
    expanded_data = expanded_data.drop_duplicates(
        subset=["Code", "Term", "Days", "StartTimeFloat"], keep="first"
    ).reset_index(drop=True)

    latest_names = expanded_data.sort_values(by="Year", ascending=False).drop_duplicates(
        subset=["Code"], keep="first"
    )
    expanded_data = expanded_data.drop(columns=["CourseName"]).merge(
        latest_names[["Code", "CourseName"]], on="Code", how="left"
    )

    rows_to_keep: list[int] = []
    for index, row in expanded_data.iterrows():
        if not any(
            check_conflict(row, expanded_data.iloc[other_index])
            for other_index in rows_to_keep
            if row["Code"] == expanded_data.iloc[other_index]["Code"]
            and row["Term"] == expanded_data.iloc[other_index]["Term"]
            and row["Days"] == expanded_data.iloc[other_index]["Days"]
        ):
            rows_to_keep.append(index)

    return expanded_data.iloc[rows_to_keep].reset_index(drop=True)
