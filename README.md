# Course Planner

Course Planner builds semester-by-semester schedules for an Industrial Engineering curriculum. It uses PySCIPOpt to enforce course requirements, prerequisite order and timetable conflicts, then lets you compare options and confirm a plan one term at a time.

![Two example schedule options and their semester paths](docs/example-schedules.png)

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run these commands from the repository root. The project targets Python 3.12.

```bash
uv sync --locked
uv run --locked streamlit run app.py
```

Run the same lint and test checks as CI.

```bash
uv run --locked ruff check .
uv run --locked pytest -q
```

## Try the example

1. Select **Load example plan** to use a simulated transcript with MAT 103E and MAT 104E left to schedule.
2. Compare two options and confirm the current term. The planner updates the graduation summary and calculates the next term.
3. Open **Export a confirmed timetable (.ics)**, choose the actual first and last teaching dates, and download the calendar. Events follow the weekly timetable in Istanbul time.

For your own plan, enter completed courses, any applicable grade overrides and a planning window. A maximum credit load is a hard constraint. A preferred free day ranks plans that are otherwise equivalent. You can also exclude summer terms.

## How planning works

- Each historical section is selected as a unit with all of its weekly meetings.
- The curriculum determines which courses are required. The bundled elective targets are four analytic, four functional and one ITB course. Completed courses and confirmed terms count toward those targets.
- Prerequisites support minimum grades and grouped alternatives. A course planned in an earlier term satisfies a prerequisite provisionally, subject to earning the required grade.
- The solver prefers fewer semesters and fewer courses, then applies any free-day preference. It can return feasible options found before the search limit. Missing sections and unreadable prerequisite rules are reported separately.

## Data and CSV uploads

The bundled [curriculum](data/course_plans.csv) and [lecture archive](data/all_lectures.csv) are historical snapshots. The archive contains 12,194 rows from 2017 Summer through 2022 Fall. Future timetables are projections from that archive. The repository history does not record the original extraction or redistribution terms for the CSVs. The [MIT license](LICENSE) applies to the application code. For official curriculum information, consult the [İTÜ Registrar's ENDE_LS course-plan listing](https://obs.itu.edu.tr/public/DersPlan/DersPlanlariList?planTipiKodu=lisans&programKodu=ENDE_LS).

Choose **Use my CSV files** to replace either input in the current session. Use comma-separated UTF-8 files up to 5 MiB and 50,000 rows each. The bundled CSVs and [small test fixtures](tests/fixtures) show the formats.

| File | Required columns |
| --- | --- |
| Curriculum | `Semester`, `Course Code`, `Course Name`, `Credits`, `Prerequisites`, `Type` |
| Lecture history | `Kod`, `Ders`, `Eğitmen(ler)`, `Gün`, `Saat`, `Bina`, `Kayıtlı`, `Kontenjan`, `Bölüm sınırlaması`, `Term` |

Use terms such as `2025 - 2026 Güz Dönemi`. When several rows belong to the same section, include a shared `CRN` so the planner can group their meetings. You can set the department filter and elective targets for the selected data.

## Import published İTÜ data

The ingestion command collects the [ENDE_LS plan](https://obs.itu.edu.tr/public/DersPlan/DersPlanDetay/1551), its elective groups, listed prerequisites and the [active undergraduate timetable](https://obs.itu.edu.tr/public/DersProgram). The planning API reads imported snapshots. The CSV planner remains available as a separate mode.

```bash
docker compose up -d --wait db
docker compose run --rm ingest
docker compose run --rm db-test
```

In WSL without Docker Desktop integration, use `docker.exe compose -f "$(wslpath -w "$PWD/compose.yaml")"` in place of `docker compose`.

The importer prints the active term, snapshot ID, record counts and whether it created a new snapshot. Reimporting identical normalized content reuses its snapshot ID, even when the collection time changes. A change in source data, such as updated enrollment, creates a new version. A failed scrape or database transaction leaves the last active version available. PostgreSQL stores the data in the Compose volume `db_data`.

The import preserves full periods, CRNs, weekly meetings, section eligibility, course alternatives and source URLs. A missing entry in the prerequisite list means the rule is unlisted. Elective options without a full course code are recorded separately on their requirement. To see the current snapshot for each imported period, run the following command.

```bash
docker compose exec db psql -U course_planner -d course_planner -c 'SELECT program, term_year, season, snapshot_id, observed_at FROM itu_active_snapshots ORDER BY observed_at DESC'
```

`db-test` runs the full test suite, including PostgreSQL tests against deterministic fixtures in a separate database schema. The tests cover duplicate imports, rollback on invalid data and protection against an older collection replacing the active version.

## Run the published-data planner

```bash
docker compose up -d --build api ui
docker compose run --rm ingest
docker compose run --rm db-test
```

Open Streamlit at http://localhost:8501 and select **Published İTÜ data**. If the tab was already open during an import, use **Refresh published data**. Open http://localhost:8000/docs for the FastAPI contract. `GET /v1/catalogue` lists the available snapshots, and `POST /v1/plans` accepts a term window and completed grades. Pass the returned `snapshot_ids` to replay a calculation against the same data.

A published term only uses the exact sections from its snapshot. A later term can use an older same-season snapshot as a labelled forecast. Missing coverage, absent sections and unmet prerequisites are reported separately. Unlisted or unsupported eligibility rules are reported as missing evidence. In the published UI, only confirm ING 100 eligibility if you have verified it with İTÜ.

## Monitor the data pipeline

```bash
docker compose --profile monitoring up -d --build api prometheus grafana
docker compose run --rm --build ingest
```

Prometheus scrapes the planning API at `/metrics`. Open Grafana at http://localhost:3000 and select **Course Planner**. The local default login is `admin` with password `course_planner`. Set `COURSE_PLANNER_GRAFANA_PASSWORD` before the first start to choose a different password.

The dashboard shows solver outcomes and latency, data age, the last successful import and import outcomes. Import outcomes are recorded in PostgreSQL so they remain available after the importer exits. Application events are emitted as JSON without completed-course inputs.

## Code layout

The Streamlit entry point is [app.py](app.py). The [planning package](src/course_planner) contains the CSV pipeline, curriculum rules, scheduling domain, solver, and calendar export. [Tests](tests) cover the planner and the application flow with bundled data.
