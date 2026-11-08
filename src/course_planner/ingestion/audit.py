"""Persist import outcomes so one-shot jobs remain visible after exit."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from psycopg import Connection

type ImportStatus = Literal["succeeded", "invalid_source", "source_unavailable", "database_error"]


def record_import(
    connection: Connection, started_at: datetime, status: ImportStatus, snapshot_id: int | None = None
) -> None:
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO itu_import_runs (started_at, finished_at, status, snapshot_id)
            VALUES (%s, clock_timestamp(), %s, %s)
            """, (started_at, status, snapshot_id),
        )
