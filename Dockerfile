FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.12-slim AS runtime

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM runtime AS test
RUN uv sync --locked --no-editable
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q", "tests/test_itu_ingestion.py", "tests/test_ingestion_postgres.py"]

FROM runtime AS ingest
ENTRYPOINT ["python", "-m", "course_planner.ingestion"]
