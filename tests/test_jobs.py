import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from course_planner import planning_api
from course_planner.ingestion.itu import FetchError
from course_planner.ingestion.postgres import import_snapshot
from course_planner.jobs import store, worker
from course_planner.planning_data import load_planning_data


@pytest.fixture
def jobs_db(database):
    store.initialize_job_schema(database)
    return database


@pytest.fixture
def broker():
    url = os.environ.get("COURSE_PLANNER_TEST_AMQP_URL")
    if not url:
        pytest.skip("Set COURSE_PLANNER_TEST_AMQP_URL to run RabbitMQ integration tests")
    queue = f"planner_test_{uuid4().hex}"
    with worker.open_broker(url) as connection:
        channel = connection.channel()
        channel.queue_declare(queue=queue, durable=True)
        channel.confirm_delivery()
        try:
            yield channel, queue
        finally:
            channel.queue_delete(queue=queue)


def test_plan_job_is_pinned_idempotent_and_deduplicates_rabbitmq_redelivery(
    jobs_db, broker, snapshot, monkeypatch,
):
    @contextmanager
    def connection():
        yield jobs_db

    monkeypatch.setattr(planning_api, "connect", connection)
    monkeypatch.setattr(planning_api, "read_dataset", lambda ids=(): load_planning_data(jobs_db, ids))
    imported = import_snapshot(jobs_db, snapshot)
    term = {"year": 2026, "season": "Fall"}
    payload = {"start": term, "end": term, "completed_grades": {"BIL 100E": "DD"}}
    headers = {"Idempotency-Key": "same-request"}
    channel, queue = broker

    with TestClient(planning_api.app) as api:
        response = api.post("/v1/jobs/plans", headers=headers, json=payload)
        assert response.status_code == 202
        job_id = UUID(response.json()["id"])
        url = response.headers["Location"]
        updated = replace(snapshot, sections=(), observed_at=snapshot.observed_at + timedelta(minutes=1))
        import_snapshot(jobs_db, updated)
        assert api.post("/v1/jobs/plans", headers=headers, json=payload).json()["id"] == str(job_id)
        assert api.post("/v1/jobs/plans", headers=headers, json={**payload, "max_credits": 5}).status_code == 409
        assert store.dispatch_ready(jobs_db, lambda item: worker.publish_job(channel, item, queue=queue)) == 1
        # Simulate publish-before-commit replay by delivering the same job twice.
        worker.publish_job(channel, job_id, queue=queue)
        for _ in range(2):
            method, properties, body = channel.basic_get(queue=queue, auto_ack=False)
            assert properties.delivery_mode == 2
            worker.execute_job(jobs_db, UUID(body.decode("ascii")))
            channel.basic_ack(method.delivery_tag)
        job = api.get(url).json()
        assert job["status"] == "succeeded"
        assert job["attempts"] == 1
        assert job["result"]["snapshot_ids"] == [imported.snapshot_id]
        assert job["result"]["options"][0][0]["section"]["crn"] == 12345


def test_expired_worker_cannot_publish_results_after_a_new_claim(jobs_db):
    job = store.enqueue(jobs_db, "import", "lease", store.request_digest({}), {})
    stale = store.claim_job(jobs_db, job.id, lease_seconds=-1)
    new = store.claim_job(jobs_db, job.id)

    def must_not_run():
        pytest.fail("An expired worker ran its publication callback")

    assert not store.finish_job(jobs_db, stale, must_not_run)
    assert store.finish_job(jobs_db, new, lambda: {"snapshot_id": 1})
    result = store.get_job(jobs_db, job.id)
    assert result.status == "succeeded"
    assert result.attempts == 2


def test_import_job_retries_transient_failure_then_records_one_success(jobs_db, snapshot, monkeypatch):
    job = store.enqueue(jobs_db, "import", "retry", store.request_digest({}), {})

    def source_failure():
        raise FetchError("temporarily unavailable")

    monkeypatch.setattr(worker, "collect_snapshot", source_failure)
    worker.execute_job(jobs_db, job.id)
    retry = store.get_job(jobs_db, job.id)
    assert (retry.status, retry.attempts, retry.error) == ("queued", 1, "source_unavailable")

    jobs_db.execute("UPDATE planner_jobs SET ready_at = clock_timestamp() WHERE id = %s", (job.id,))
    monkeypatch.setattr(worker, "collect_snapshot", lambda: snapshot)
    worker.execute_job(jobs_db, job.id)
    worker.execute_job(jobs_db, job.id)

    result = store.get_job(jobs_db, job.id)
    assert (result.status, result.attempts, result.error) == ("succeeded", 2, None)
    assert jobs_db.execute("SELECT count(*) FROM itu_snapshots").fetchone() == (1,)
    assert jobs_db.execute("SELECT status FROM itu_import_runs ORDER BY id").fetchall() == [
        ("source_unavailable",), ("succeeded",),
    ]
