from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .constants import DAY_MAP
from .schedule import Meeting, Section

_TIME_RANGE = re.compile(r"(?:[01]\d|2[0-3])[0-5]\d/(?:[01]\d|2[0-3])[0-5]\d")


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
        pd.isna(row["Days"])
        or pd.isna(row["Times"])
        or str(row["Days"]).strip("- ") == ""
        or row["Times"] == "/"
    ):
        if "490" not in row["Code"]:
            return []
        row["Days"] = None
        row["Times"] = None
        row["StartTimeFloat"] = float("nan")
        row["EndTimeFloat"] = float("nan")
        return [row]

    days = str(row["Days"]).split()
    times = str(row["Times"]).split()
    if len(times) == 1:
        times *= len(days)
    if len(days) != len(times) or not all(day in DAY_MAP for day in days):
        return []

    expanded_rows: list[pd.Series] = []
    for day, time in zip(days, times):
        if not _TIME_RANGE.fullmatch(time):
            return []
        start, end = map(time_to_float, time.split("/"))
        if start >= end:
            return []
        expanded_row = row.copy()
        expanded_row["Days"] = day
        expanded_row["Times"] = time
        expanded_row["StartTimeFloat"] = start
        expanded_row["EndTimeFloat"] = end
        expanded_rows.append(expanded_row)

    return expanded_rows


def time_to_float(time_str: str) -> float:
    hours = int(time_str[:2])
    minutes = int(time_str[2:])
    return hours + minutes / 60


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

    lecture_metadata = {
        row["Course Code"]: {
            "Credits": row["Credits"],
            "Prerequisites": row["Prerequisites"],
            "Grade": int((float(str(row["Semester"]).split(".")[0]) + 1) // 2),
            "Type": row["Type"],
        }
        for _, row in necessary_lectures.iterrows()
    }

    filtered_data = data[data["Code"].isin(lecture_metadata)].copy()
    filtered_data = split_years(filtered_data)

    # CRNs may be reused in other years or seasons. Missing CRNs identify only
    # their own source row, whose meetings can still be kept together.
    source_crns = filtered_data.get(
        "CRN", pd.Series(index=filtered_data.index, dtype="string")
    ).astype("string")
    missing_crns = pd.Series(
        [f"row-{index}" for index in filtered_data.index], index=filtered_data.index
    )
    filtered_data["SectionId"] = filtered_data.assign(
        SourceCRN=source_crns.fillna(missing_crns)
    ).groupby(["Code", "Year", "Term", "SourceCRN"], sort=False, dropna=False).ngroup()
    filtered_data["Quota"] = pd.to_numeric(filtered_data["Quota"], errors="coerce")
    filtered_data["Registered"] = pd.to_numeric(filtered_data["Registered"], errors="coerce")
    eligible_rows = filtered_data["DepartmentRestrictions"].str.contains(r"\bENDE\b", na=False)

    expanded_rows: list[pd.Series] = []
    for _, section in filtered_data.groupby("SectionId", sort=False):
        eligible = section.loc[eligible_rows]
        if eligible.empty:
            continue
        code = section["Code"].iloc[0]
        if "490" not in code and not eligible["Quota"].ge(1).any():
            continue
        meetings = [split_days_and_times(row) for _, row in section.iterrows()]
        if not all(meetings):
            continue
        for row_meetings in meetings:
            for meeting in row_meetings:
                for column, value in lecture_metadata[code].items():
                    meeting[column] = value
                expanded_rows.append(meeting)

    if not expanded_rows:
        return filtered_data.iloc[:0].assign(
            StartTimeFloat=pd.Series(dtype=float), EndTimeFloat=pd.Series(dtype=float)
        )

    expanded_data = pd.DataFrame(expanded_rows).drop_duplicates(
        subset=["SectionId", "Days", "StartTimeFloat", "EndTimeFloat"]
    )

    open_rates = calculate_open_rate(expanded_data)
    expanded_data = expanded_data.merge(
        open_rates[["Term", "Code", "Open Rate"]], on=["Term", "Code"], how="left"
    )
    expanded_data = expanded_data.merge(
        calculate_take_probability(expanded_data.drop_duplicates("SectionId")),
        on=["Term", "CourseName"],
        how="left",
    )

    # Keep one historical example of each complete timetable, preferring the
    # latest instance. Matching individual slots would splice different CRNs.
    representatives = {}
    for section_id, section in expanded_data.groupby("SectionId", sort=False):
        first = section.iloc[0]
        timetable = frozenset(
            section[["Days", "Times"]].itertuples(index=False, name=None)
        )
        key = (first["Code"], first["Term"], timetable)
        if key not in representatives or first["Year"] > representatives[key][0]:
            representatives[key] = (first["Year"], section_id)
    selected_ids = {section_id for _, section_id in representatives.values()}
    return expanded_data[expanded_data["SectionId"].isin(selected_ids)].reset_index(drop=True)


def sections_from_frame(data: pd.DataFrame) -> tuple[Section, ...]:
    sections = []
    for section_id, rows in data.groupby("SectionId", sort=False):
        first = rows.iloc[0]
        meetings = tuple(
            Meeting(day, round(start * 60), round(end * 60), "" if pd.isna(room) else str(room))
            for day, start, end, room in rows[
                ["Days", "StartTimeFloat", "EndTimeFloat", "Building"]
            ].itertuples(index=False, name=None)
            if pd.notna(day)
        )
        instructor = first["Instructors"]
        sections.append(Section(
            int(section_id), first["Code"], first["Term"], meetings,
            "" if pd.isna(instructor) or instructor == "--" else str(instructor),
        ))
    return tuple(sections)
