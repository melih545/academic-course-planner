from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import pandas as pd

from .curriculum import ELECTIVE_MINIMUMS
from .preprocessing import process_lecture_history, sections_from_frame, split_years
from .schedule import Section, Term

MAX_CSV_MB = 5
MAX_CSV_ROWS = 50_000
CATALOG_COLUMNS = {"Course Code", "Course Name", "Credits", "Prerequisites", "Semester", "Type"}
HISTORY_COLUMNS = {
    "Kod", "Ders", "Eğitmen(ler)", "Gün", "Saat", "Bina",
    "Kayıtlı", "Kontenjan", "Bölüm sınırlaması", "Term",
}


@dataclass(frozen=True)
class Dataset:
    catalog: pd.DataFrame
    sections: tuple[Section, ...]
    first_term: Term
    last_term: Term

    @classmethod
    def from_frames(
        cls, catalog: pd.DataFrame, history: pd.DataFrame, department: str | None = "ENDE"
    ) -> Dataset:
        terms = [
            Term(year, season)
            for year, season in split_years(history[["Term"]])
            .drop_duplicates()[["Year", "Term"]]
            .itertuples(index=False, name=None)
        ]
        terms.sort(key=lambda term: term.position)
        sections = sections_from_frame(process_lecture_history(history, catalog, department))
        return cls(catalog, sections, terms[0], terms[-1])


def parse_uploaded_dataset(
    catalog_csv: bytes, history_csv: bytes, department: str | None
) -> Dataset:
    catalog = _read_csv(catalog_csv, "Curriculum", CATALOG_COLUMNS)
    history = _read_csv(history_csv, "Lecture history", HISTORY_COLUMNS)
    for column in ("Course Code", "Course Name", "Semester", "Type"):
        catalog[column] = catalog[column].astype("string").str.strip()
        if catalog[column].fillna("").eq("").any():
            raise ValueError(f"Curriculum column {column} must contain a value on every row.")
    if not catalog["Semester"].str.fullmatch(r"[1-9]\d*(?:\..*)?").all():
        raise ValueError("Curriculum Semester must begin with a positive semester number.")
    if not catalog["Type"].isin({"Main", *ELECTIVE_MINIMUMS}).all():
        raise ValueError("Curriculum Type must be Main, Elective Analytic, Elective Functional, or ITB.")
    credits = pd.to_numeric(catalog["Credits"], errors="coerce")
    if not (credits.ge(0) & credits.lt(float("inf"))).all():
        raise ValueError("Curriculum Credits must be finite, non-negative numbers.")
    catalog["Credits"] = credits
    conflicts = (
        catalog.groupby("Course Code")[["Type", "Prerequisites", "Credits"]]
        .nunique(dropna=False).gt(1).any(axis=1)
    )
    if conflicts.any():
        raise ValueError(f"Conflicting curriculum rows for {', '.join(conflicts[conflicts].index[:5])}.")

    raw_terms = history["Term"].astype("string").str.strip()
    years = raw_terms.str.extract(r"^(\d{4}) - (\d{4}) (?:Güz|Bahar|Yaz) Dönemi$")
    valid_years = pd.to_numeric(years[1]) - pd.to_numeric(years[0])
    if not valid_years.eq(1).fillna(False).all():
        raise ValueError("Lecture history Term must follow a format such as 2025 - 2026 Güz Dönemi.")
    history["Term"] = raw_terms
    history["Kod"] = history["Kod"].astype("string").str.strip()
    dataset = Dataset.from_frames(catalog, history, department)
    if not dataset.sections:
        raise ValueError("No usable lecture sections match the curriculum and department filter.")
    return dataset


def _read_csv(content: bytes, label: str, required_columns: set[str]) -> pd.DataFrame:
    if len(content) > MAX_CSV_MB * 1024 * 1024:
        raise ValueError(f"{label} CSV must be at most {MAX_CSV_MB} MiB.")
    try:
        data = pd.read_csv(BytesIO(content), encoding="utf-8-sig", nrows=MAX_CSV_ROWS + 1)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError) as error:
        raise ValueError(f"{label} must be a valid, comma-separated UTF-8 CSV.") from error
    if len(data) > MAX_CSV_ROWS:
        raise ValueError(f"{label} CSV must contain at most {MAX_CSV_ROWS:,} rows.")
    if missing := required_columns - set(data.columns):
        raise ValueError(f"{label} CSV is missing columns: {', '.join(sorted(missing))}.")
    if data.empty:
        raise ValueError(f"{label} CSV must contain at least one data row.")
    return data
