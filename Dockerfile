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
RUN uv sync --locked
COPY app.py ./
COPY data ./data
COPY tests ./tests
ENV MPLBACKEND=Agg
CMD ["python", "-m", "pytest", "-q"]

FROM runtime AS ingest
ENTRYPOINT ["python", "-m", "course_planner.ingestion"]

FROM runtime AS api
CMD ["uvicorn", "course_planner.planning_api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS ui
RUN uv sync --locked --no-dev
COPY app.py ./
COPY data ./data
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]

FROM prom/prometheus:v3.5.0 AS prometheus
COPY ops/prometheus.yml /etc/prometheus/prometheus.yml

FROM grafana/grafana:12.1.0 AS grafana
COPY ops/grafana/provisioning /etc/grafana/provisioning
COPY ops/grafana/dashboards /etc/grafana/dashboards
