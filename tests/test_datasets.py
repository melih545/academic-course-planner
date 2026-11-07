from pathlib import Path

import pandas as pd
import pytest

from course_planner.curriculum import Curriculum
from course_planner.datasets import parse_uploaded_dataset
from course_planner.schedule import Term

FIXTURES = Path(__file__).parent / "fixtures"


def test_upload_accepts_the_bundled_schema_and_keeps_data_coverage():
    catalog = (FIXTURES / "course_plans_sample.csv").read_bytes()
    history = (FIXTURES / "all_lectures_sample.csv").read_bytes()

    dataset = parse_uploaded_dataset(catalog, history, "ENDE")

    assert dataset.first_term == Term(2021, "Fall")
    assert dataset.last_term == Term(2023, "Spring")
    assert len(dataset.sections) == 2
    assert len(next(section.meetings for section in dataset.sections if section.code == "END 101E")) == 2
    with pytest.raises(ValueError, match="department filter"):
        parse_uploaded_dataset(catalog, history, "OTHER")
    assert len(parse_uploaded_dataset(catalog, history, None).sections) == 2


def test_upload_reports_missing_columns_and_invalid_periods():
    catalog = (FIXTURES / "course_plans_sample.csv").read_bytes()
    history = (FIXTURES / "all_lectures_sample.csv").read_bytes()

    with pytest.raises(ValueError, match="Curriculum CSV is missing columns"):
        parse_uploaded_dataset(b"Credits\n1\n", history, None)
    invalid = history.replace(b"2021 - 2022", b"2021 - 2023")
    with pytest.raises(ValueError, match="Lecture history Term"):
        parse_uploaded_dataset(catalog, invalid, None)


def test_upload_rejects_conflicting_course_rows():
    catalog = pd.read_csv(FIXTURES / "course_plans_sample.csv")
    duplicate = catalog.iloc[[0]].assign(Credits=50)
    history = (FIXTURES / "all_lectures_sample.csv").read_bytes()

    with pytest.raises(ValueError, match="Conflicting curriculum rows for END 101E"):
        parse_uploaded_dataset(pd.concat([catalog, duplicate]).to_csv(index=False).encode(), history, None)


def test_uploaded_curriculum_can_have_its_own_elective_targets():
    catalog = pd.read_csv(FIXTURES / "course_plans_sample.csv")
    catalog.loc[0, "Type"] = "ITB"
    curriculum = Curriculum.from_frame(catalog, {"ITB": 2})

    assert curriculum.elective_deficits(set()) == {"ITB": 2}
    assert curriculum.elective_deficits({"END 101E"}) == {"ITB": 1}
