from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .preprocessing import process_lecture_history, sections_from_frame, split_years
from .schedule import Section, Term


@dataclass(frozen=True)
class Dataset:
    catalog: pd.DataFrame
    sections: tuple[Section, ...]
    first_term: Term
    last_term: Term

    @classmethod
    def from_frames(cls, catalog: pd.DataFrame, history: pd.DataFrame) -> Dataset:
        terms = [
            Term(year, season)
            for year, season in split_years(history[["Term"]])
            .drop_duplicates()[["Year", "Term"]]
            .itertuples(index=False, name=None)
        ]
        terms.sort(key=lambda term: term.position)
        sections = sections_from_frame(process_lecture_history(history, catalog))
        return cls(catalog, sections, terms[0], terms[-1])
