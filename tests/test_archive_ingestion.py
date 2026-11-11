import csv
import json
from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO

import pytest

from course_planner.ingestion.archive import CSV_COLUMNS, _lab_sections, collect_archive
from course_planner.ingestion.itu import Meeting, SourceError
from course_planner.ingestion.postgres import import_snapshot


def archive_fetch(url: str) -> str:
    if url.endswith("meta.json"):
        return json.dumps({
            "slug": "2023-2024-bahar", "term": "2023-2024 Bahar Dönemi",
            "source": "itu-archive", "scrapedAt": "2026-08-12T13:56:46Z", "sections": 2,
        })
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=sorted(CSV_COLUMNS))
    writer.writeheader()
    row = {
        "CRN": "20001", "Ders Kodu": "END 201E", "Seviye": "LS", "Gün": "Salı | Perşembe",
        "Saat": "09:30/10:29 | 10:30/11:29", "Kontenjan": "30", "Alabilen Programlar": "END, ENDE",
    }
    writer.writerow(row)
    writer.writerow({**row, "CRN": "20002", "Alabilen Programlar": "END"})
    return output.getvalue()


def test_archive_validates_term_and_preserves_program_and_meetings(snapshot):
    imported = collect_archive("2023-2024-bahar", snapshot.plan_title, snapshot.courses, archive_fetch)

    assert (imported.term_year, imported.season, imported.source_kind) == (2024, "Spring", "archive")
    (section,) = imported.sections
    assert section.crn == 20001
    assert section.allowed_programs == ("END", "ENDE_LS")
    assert section.meetings == (Meeting(1, 570, 629), Meeting(3, 630, 689))
    assert section.source_url in imported.source_urls
    assert not imported.rules

    def incomplete_fetch(url: str) -> str:
        return archive_fetch(url).replace('"sections": 2', '"sections": 3')

    with pytest.raises(SourceError, match="Incomplete archive export"):
        collect_archive("2023-2024-bahar", snapshot.plan_title, snapshot.courses, incomplete_fetch)


def test_archive_import_is_idempotent_and_published_takes_precedence(database, snapshot):
    archive = collect_archive("2023-2024-bahar", snapshot.plan_title, snapshot.courses, archive_fetch)
    first = import_snapshot(database, archive)
    second = import_snapshot(database, archive)
    assert first.created and not second.created
    assert first.snapshot_id == second.snapshot_id
    assert database.execute("SELECT count(*) FROM itu_sections").fetchone() == (1,)
    assert database.execute("SELECT count(*) FROM itu_meetings").fetchone() == (2,)

    published = replace(
        snapshot, term_year=archive.term_year, season=archive.season,
        term_label=archive.term_label, observed_at=datetime(2024, 4, 15, tzinfo=UTC),
    )
    official = import_snapshot(database, published)
    assert official.active_snapshot_id == official.snapshot_id
    assert import_snapshot(database, archive).active_snapshot_id == official.snapshot_id


def test_lab_archive_restores_eligible_section_with_legacy_times():
    csv_text = (
        'CRN,Course Code,Major Restriction,Day,Time,Capacity,Enrolled,Teaching Method,Instructor,Building,Room\n'
        '20528,FIZ 102EL,"END, ENDE",Pazartesi,0930/1129,30,25,,Instructor,BINA,101\n'
        '20529,FIZ 102EL,END,Pazartesi,0930/1129,30,25,,Instructor,BINA,101\n'
    )
    (section,) = _lab_sections(csv_text, "ENDE", "source")
    assert section.crn == 20528
    assert "ENDE_LS" in section.allowed_programs
    assert section.meetings == (Meeting(0, 570, 689),)
