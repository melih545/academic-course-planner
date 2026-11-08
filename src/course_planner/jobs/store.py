"""PostgreSQL-backed job state, durable dispatch and leased execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from typing import Literal
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

type JobKind = Literal["plan", "import"]
type JobStatus = Literal["queued", "running", "succeeded", "failed"]
MAX_ATTEMPTS = 3

_COLUMNS = "id, kind, status, attempts, created_at, updated_at, result, error"


class IdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class Job:
    id: UUID
    kind: JobKind
    status: JobStatus
    attempts: int
    created_at: datetime
    updated_at: datetime
    result: dict | None
    error: str | None


@dataclass(frozen=True)
class Claim:
    id: UUID
    token: UUID
    kind: JobKind
    attempts: int
    payload: dict


def initialize_job_schema(connection: Connection) -> None:
    schema = files("course_planner.jobs").joinpath("schema.sql").read_text(encoding="utf-8")
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(328423051894264)")
        connection.execute(schema)


def request_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def existing_job(connection: Connection, kind: JobKind, key: str, digest: str) -> Job | None:
    row = connection.execute(
        f"SELECT {_COLUMNS}, request_digest FROM planner_jobs WHERE kind = %s AND idempotency_key = %s",
        (kind, key),
    ).fetchone()
    if row is None:
        return None
    if row[-1] != digest:
        raise IdempotencyConflict("This idempotency key was already used with a different request.")
    return Job(*row[:-1])


def enqueue(connection: Connection, kind: JobKind, key: str, digest: str, payload: dict) -> Job:
    with connection.transaction():
        row = connection.execute(
            f"""
            INSERT INTO planner_jobs (id, kind, idempotency_key, request_digest, payload)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (kind, idempotency_key) DO NOTHING
            RETURNING {_COLUMNS}
            """, (uuid4(), kind, key, digest, Jsonb(payload)),
        ).fetchone()
        if row is not None:
            return Job(*row)
        return existing_job(connection, kind, key, digest)


def get_job(connection: Connection, job_id: UUID) -> Job | None:
    row = connection.execute(f"SELECT {_COLUMNS} FROM planner_jobs WHERE id = %s", (job_id,)).fetchone()
    return Job(*row) if row else None


def dispatch_ready(connection: Connection, publish: Callable[[UUID], None], *, limit: int = 20) -> int:
    with connection.transaction():
        connection.execute("""
            UPDATE planner_jobs SET status = 'failed', error = 'Worker lease expired after final attempt.',
                claim_token = NULL, lease_until = NULL, updated_at = clock_timestamp()
            WHERE status = 'running' AND lease_until <= clock_timestamp() AND attempts >= %s
        """, (MAX_ATTEMPTS,))
        jobs = connection.execute("""
            SELECT id FROM planner_jobs
            WHERE ((status = 'queued' AND ready_at <= clock_timestamp())
                OR (status = 'running' AND lease_until <= clock_timestamp()))
                AND dispatch_after <= clock_timestamp() AND attempts < %s
            ORDER BY ready_at, created_at
            LIMIT %s FOR UPDATE SKIP LOCKED
        """, (MAX_ATTEMPTS, limit)).fetchall()
        for (job_id,) in jobs:
            publish(job_id)
            connection.execute("""
                UPDATE planner_jobs SET dispatch_after = clock_timestamp() + interval '30 seconds'
                WHERE id = %s
            """, (job_id,))
    return len(jobs)


def claim_job(connection: Connection, job_id: UUID, *, lease_seconds: int = 300) -> Claim | None:
    token = uuid4()
    with connection.transaction():
        row = connection.execute("""
            UPDATE planner_jobs SET status = 'running', attempts = attempts + 1, claim_token = %s,
                lease_until = clock_timestamp() + (%s * interval '1 second'), updated_at = clock_timestamp()
            WHERE id = %s AND attempts < %s
              AND ((status = 'queued' AND ready_at <= clock_timestamp())
                OR (status = 'running' AND lease_until <= clock_timestamp()))
            RETURNING kind, attempts, payload
        """, (token, lease_seconds, job_id, MAX_ATTEMPTS)).fetchone()
        return Claim(job_id, token, *row) if row else None


def finish_job(connection: Connection, claim: Claim, persist: Callable[[], dict]) -> bool:
    with connection.transaction():
        if not _owns_job(connection, claim):
            return False
        result = persist()
        connection.execute("""
            UPDATE planner_jobs SET status = 'succeeded', result = %s, error = NULL, claim_token = NULL,
                lease_until = NULL, updated_at = clock_timestamp() WHERE id = %s
        """, (Jsonb(result), claim.id))
    return True


def fail_job(
    connection: Connection, claim: Claim, message: str, *, retryable: bool,
    audit: Callable[[], None] | None = None,
) -> bool:
    with connection.transaction():
        if not _owns_job(connection, claim):
            return False
        if audit:
            audit()
        retry = retryable and claim.attempts < MAX_ATTEMPTS
        connection.execute("""
            UPDATE planner_jobs SET status = %s, error = %s, claim_token = NULL, lease_until = NULL,
                ready_at = clock_timestamp() + (%s * interval '1 second'),
                dispatch_after = clock_timestamp(), updated_at = clock_timestamp()
            WHERE id = %s
        """, ("queued" if retry else "failed", message, 2**claim.attempts if retry else 0, claim.id))
    return True


def _owns_job(connection: Connection, claim: Claim) -> bool:
    return connection.execute("""
        SELECT 1 FROM planner_jobs WHERE id = %s AND status = 'running'
            AND claim_token = %s AND lease_until > clock_timestamp() FOR UPDATE
    """, (claim.id, claim.token)).fetchone() is not None
