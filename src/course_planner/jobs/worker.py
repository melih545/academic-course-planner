"""Dispatch durable jobs via RabbitMQ and acknowledge only after persistence."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import sleep
from uuid import UUID

import pika
import psycopg
from prometheus_client import CollectorRegistry, start_http_server

from ..ingestion.__main__ import classify_failure, summary
from ..ingestion.audit import record_import
from ..ingestion.itu import FetchError, SourceError, collect_snapshot
from ..ingestion.postgres import import_snapshot, initialize_schema
from ..planning_api import PlanInput, solve_plan
from ..planning_data import connect, load_planning_data
from ..telemetry import PLAN_RUNS, SOLVE_DURATION, event
from .store import (
    claim_job,
    dispatch_ready,
    fail_job,
    finish_job,
    initialize_job_schema,
)

QUEUE = "course_planner.jobs"


def open_broker(url: str):
    params = pika.URLParameters(url)
    params.heartbeat = 60
    params.blocked_connection_timeout = 10
    params.socket_timeout = 5
    return pika.BlockingConnection(params)


def publish_job(channel, job_id: UUID, *, queue: str = QUEUE) -> None:
    channel.basic_publish(
        exchange="", routing_key=queue, body=str(job_id).encode("ascii"), mandatory=True,
        properties=pika.BasicProperties(delivery_mode=2, message_id=str(job_id)),
    )


def execute_job(connection: psycopg.Connection, job_id: UUID) -> None:
    claim = claim_job(connection, job_id)
    if claim is None:
        return
    started = datetime.now(UTC)
    try:
        if claim.kind == "plan":
            payload = PlanInput.model_validate(claim.payload)
            result = solve_plan(load_planning_data(connection, payload.snapshot_ids), payload)
            saved = finish_job(connection, claim, lambda: result.model_dump(mode="json"))
        else:
            snapshot = collect_snapshot()

            def persist():
                result = import_snapshot(connection, snapshot)
                record_import(connection, started, "succeeded", result.snapshot_id)
                return summary(snapshot, result)

            saved = finish_job(connection, claim, persist)
        if saved:
            event("job_completed", job_id=job_id, kind=claim.kind, attempt=claim.attempts)
    except (SourceError, psycopg.Error) as error:
        category = classify_failure(error)
        audit = None
        if claim.kind == "import":
            def audit():
                record_import(connection, started, category)
        fail_job(connection, claim, category, retryable=isinstance(error, (FetchError, psycopg.OperationalError)),
                 audit=audit)
        event("job_attempt_failed", level=logging.WARNING, job_id=job_id, kind=claim.kind,
              attempt=claim.attempts, reason=category)
    except Exception as error:
        fail_job(connection, claim, "The job could not be executed.", retryable=False)
        event("job_attempt_failed", level=logging.ERROR, job_id=job_id, kind=claim.kind,
              attempt=claim.attempts, error_type=type(error).__name__)


def run_job(job_id: UUID) -> None:
    with connect() as connection:
        connection.autocommit = True
        execute_job(connection, job_id)


def serve(url: str) -> None:
    broker = open_broker(url)
    try:
        channel = broker.channel()
        channel.queue_declare(queue=QUEUE, durable=True)
        channel.confirm_delivery()
        with connect() as connection, ThreadPoolExecutor(max_workers=1) as executor:
            connection.autocommit = True
            event("worker_ready", queue=QUEUE)
            while True:
                dispatch_ready(connection, lambda job_id: publish_job(channel, job_id))
                method, _, body = channel.basic_get(queue=QUEUE, auto_ack=False)
                if method is None:
                    broker.process_data_events(time_limit=1)
                    continue
                try:
                    job_id = UUID(body.decode("ascii"))
                except (ValueError, UnicodeError):
                    channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                    continue
                task = executor.submit(run_job, job_id)
                while not task.done():
                    broker.process_data_events(time_limit=1)
                task.result()
                channel.basic_ack(delivery_tag=method.delivery_tag)
    finally:
        if broker.is_open:
            broker.close()


def main() -> None:
    url = os.environ["COURSE_PLANNER_AMQP_URL"]
    registry = CollectorRegistry()
    registry.register(PLAN_RUNS)
    registry.register(SOLVE_DURATION)
    start_http_server(9101, registry=registry)
    while True:
        try:
            with connect() as connection:
                initialize_schema(connection)
                initialize_job_schema(connection)
            serve(url)
        except (pika.exceptions.AMQPError, psycopg.Error, OSError) as error:
            event("worker_reconnecting", level=logging.WARNING, error_type=type(error).__name__)
            sleep(2)


if __name__ == "__main__":
    main()
