import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from course_planner.ingestion import itu
from course_planner.ingestion.postgres import initialize_schema


@pytest.fixture
def database():
    dsn = os.environ.get("COURSE_PLANNER_TEST_DSN")
    if not dsn:
        pytest.skip("Set COURSE_PLANNER_TEST_DSN to run PostgreSQL integration tests")
    schema = sql.Identifier(f"ingest_test_{uuid4().hex}")
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        try:
            connection.execute(sql.SQL("SET search_path TO {}").format(schema))
            initialize_schema(connection)
            yield connection
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema))


@pytest.fixture
def snapshot():
    source = "https://obs.itu.edu.tr/public/DersProgram"
    return itu.Snapshot(
        "ENDE_LS", 1551, "Curriculum", "2026-2027 Güz Dönemi", 2026, "Fall",
        datetime(2026, 11, 7, tzinfo=UTC),
        (itu.Course("END 201E", "Python", "English", Decimal(2), Decimal("5.5")),),
        (itu.Requirement(4, 1, "TM", "Python", ("END 201E",)),),
        (itu.Rule("END 201E", "BIL 100E MIN. DD", "", source),),
        (itu.Section(
            12345, "END 201E", "Fiziksel", "Örnek", "BINA", "101", 30, 25, ("ENDE_LS",),
            (itu.Meeting(0, 510, 569), itu.Meeting(2, 630, 749)), source,
        ),),
        (source,),
    )
