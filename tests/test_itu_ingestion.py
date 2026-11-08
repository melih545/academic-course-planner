import json

import pytest

from course_planner.ingestion import itu


def _table(headers, rows):
    header = "".join(f"<th>{cell}</th>" for cell in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table class='datalist'><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def _schedule(rows):
    return _table(
        ("CRN", "Ders Kodu", "Öğretim Yöntemi", "Öğretim Üyesi", "Bina", "Derslik",
         "Kontenjan", "Yazılan", "Dersi Alabilen Programlar", "Gün", "Saat"),
        rows,
    )


def _section(crn, code, days, times, programs="ENDE_LS"):
    return (crn, code, "Fiziksel", "Örnek", "BINA", "101", "30", "26", programs, days, times)


def test_section_parser_retains_all_meetings_and_separates_unknown_eligibility():
    data = _schedule([
        _section(10001, "END 201E", "Pazartesi<br>Çarşamba", "08:30/09:29<br>10:30/12:29"),
        _section(10001, "END 201E", "Cuma", "09:30/10:29"),
        _section(10002, "END 201E", "-", "-/-", "-"),
        _section(10003, "END 101E", "Salı", "13:30/15:29"),
    ])

    first, second = itu.parse_sections(data, {"END 201E"}, "source")

    assert first.allowed_programs == ("ENDE_LS",)
    assert first.meetings == (
        itu.Meeting(0, 510, 569), itu.Meeting(2, 630, 749), itu.Meeting(4, 570, 629),
    )
    assert second.allowed_programs is None
    assert second.meetings == ()


def test_unexpected_schedule_is_distinct_from_a_valid_empty_table():
    assert itu.parse_sections(_schedule([]), {"END 201E"}, "source") == ()
    with pytest.raises(itu.SourceError, match="Expected table"):
        itu.parse_sections("<h1>Unavailable</h1>", {"END 201E"}, "source")
    with pytest.raises(itu.SourceError, match="day/time"):
        itu.parse_sections(
            _schedule([_section(10001, "END 201E", "Pazartesi<br>Salı", "08:30/09:29")]),
            {"END 201E"}, "source",
        )


def test_collect_preserves_plan_requirements_and_source_provenance():
    elective = "/public/DersPlan/_DersGrupSearch?grupId=9&dersTipiId=20"
    plan_tables = []
    for semester in range(1, 9):
        links = f"<a>END {100 + semester}E</a>"
        if semester == 8:
            links += "<a>END 108</a>"
        rows = [(links, "Örnek ders", "English", "Z", "3,5", "5", "3", "0", "0", "TM")]
        if semester == 7:
            rows.append((
                f'<a href="{elective}">Dersler</a>', "Fonksiyonel seçmeli", "", "S",
                "3 / 4", "5 / 6", "", "", "", "MTF",
            ))
        table = _table(
            ("Ders Kodu", "Ders Adı", "Ders Dili", "Z/S", "Kredi", "AKTS",
             "Teo.", "Uyg.", "Lab.", "Türü"),
            rows,
        )
        plan_tables.append(table.replace("<thead>", f"<thead><h2>{semester}. Yarıyıl</h2>"))
    prefix = (
        "<h1>Endüstri Mühendisliği (İngilizce) Lisans Ders Planı</h1>"
        "<h2>Endüstri Mühendisliği 2021-2022 Güz Dönemi Sonrası</h2>"
    )
    schedule_url = f"{itu.SCHEDULE}/DersProgramSearch?{itu.LEVEL}&dersBransKoduId=15"
    rules_url = f"{itu.BASE}/public/GenelTanimlamalar/OnsartAra?DersBransKoduId=15"
    sources = {
        itu.TERM_URL: json.dumps({"aktifDonem": "2026-2027 Güz Dönemi"}),
        itu.PLAN_INDEX: f'<a href="/public/DersPlan/DersPlanDetay/{itu.PLAN_ID}">Detay</a>',
        itu.PLAN_URL: prefix + "".join(plan_tables),
        itu.BASE + elective: _table(
            ("Ders", "Dil", "Kredi", "AKTS", "Teo.", "Uyg.", "Lab."),
            [
                ("END 301E<br>Elective", "English", "3", "5", "3", "0", "0"),
                ("JPN<br>Japanese I", "Japanese", "3", "5", "3", "0", "0"),
            ],
        ),
        itu.BRANCH_URL: '[{"bransKoduId":15,"dersBransKodu":"END"}]',
        itu.RULES_INDEX: '<select id="DersBransKoduId"><option value="15">END</option>'
        '<option value="33">ING</option></select>',
        schedule_url: _schedule([_section(10001, "END 102E", "Salı", "08:30/10:29")]),
        rules_url: _table(
            ("Ders Kodu", "Ders Adı", "Ders Önşartları", "Başarılan Kredi/Sınıf Önşartı"),
            [("<a>END 102E</a>", "Örnek", "END 101E MIN. DD", "2. sınıf")],
        ),
    }

    snapshot = itu.collect_snapshot(sources.__getitem__)

    assert (snapshot.program, snapshot.term_year, snapshot.season) == ("ENDE_LS", 2026, "Fall")
    assert snapshot.requirements[-1].course_codes == ("END 108E", "END 108")
    assert snapshot.requirements[-2].elective_group == "9"
    assert snapshot.requirements[-2].unresolved_options == ("JPN — Japanese I",)
    assert snapshot.rules == (itu.Rule("END 102E", "END 101E MIN. DD", "2. sınıf", rules_url),)
    assert set(snapshot.source_urls) == set(sources)


def test_course_detail_fallback_requires_matching_course_and_explicit_rule_fields():
    detail = _table(("Ders Kodu", "Ders Adı"), [("MEK205 - MEK205E", "Mechanics")])
    detail += _table(("Önşartlar", "Değer"), [
        ("Ders Önşartları", "( MAT 102E MIN. DD ) Veya ( MAT 104E MIN. DD )"),
        ("Başarılan Kredi/Sınıf Önşartı", "Yok"),
    ])
    assert itu._parse_course_rule(detail, "MEK 205E", "detail") == itu.Rule(
        "MEK 205E", "( MAT 102E MIN. DD ) Veya ( MAT 104E MIN. DD )", "Yok", "detail"
    )
    assert itu._parse_course_rule(detail, "MEK 207E", "detail") is None
    assert itu._parse_course_rule(detail.replace("Ders Önşartları", "unknown"), "MEK 205E", "detail") is None
