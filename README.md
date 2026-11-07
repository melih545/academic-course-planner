# Course Planner

Course Planner is a Streamlit application that builds semester-by-semester schedule options for an Industrial Engineering curriculum using bundled historical lecture data and optimization-based planning.

## What It Does

- Reads a curriculum definition from `data/course_plans.csv`
- Preprocesses historical lecture openings from `data/all_lectures.csv`
- Uses historical sections with usable timetables and keeps all meetings of a section together
- Generates multiple schedule options with PySCIPOpt
- Lets you confirm one semester at a time and continue planning forward

## Tech Stack

- Python 3.12+
- Streamlit for the UI
- pandas for preprocessing
- matplotlib for schedule visualization
- PySCIPOpt for optimization

## Project Structure

```text
course_planner/
├── app.py
├── data/
│   ├── all_lectures.csv
│   └── course_plans.csv
├── src/
│   └── course_planner/
│       ├── constants.py
│       ├── optimization.py
│       ├── preprocessing.py
│       ├── ui.py
│       └── visualization.py
├── tests/
│   ├── fixtures/
│   ├── conftest.py
│   ├── test_preprocessing.py
│   └── test_processing_pipeline.py
├── pyproject.toml
└── requirements.txt
```

## Getting Started

Install runtime dependencies:

```bash
pip install -r requirements.txt
```

Run the app:

```bash
streamlit run app.py
```

For development and tests:

```bash
pip install -e .[dev]
pytest
```

## Data Files

The repository keeps its default CSV inputs under `data/` so the project can be run immediately after installing dependencies.

- `data/course_plans.csv`: curriculum and prerequisite definitions
- `data/all_lectures.csv`: historical lecture and timetable data

## Known Limitations

- The planner depends on the availability of PySCIPOpt in the local environment.
- The bundled planner uses four analytic electives, four functional electives, and one ITB course. Completed courses and confirmed plan terms count toward those totals.
- Some curriculum rows contain a link prompt in place of prerequisite rules. The planner reports those rules as unavailable when they block a required course or elective target. A plan that places a prerequisite in an earlier term assumes the student will earn the required grade.
- Bundled data is static, so results are only as current as the committed CSV snapshots.

## Roadmap

- Add stricter validation around curriculum edge cases
- Improve optimizer diagnostics for infeasible plans
- Expand automated coverage around the scheduling model
