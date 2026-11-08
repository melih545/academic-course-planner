"""Collect the public İTÜ ÖBS curriculum and the active undergraduate timetable."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, Tag

BASE = "https://obs.itu.edu.tr"
PROGRAM = "ENDE_LS"
PLAN_ID = 1551
PLAN_URL = f"{BASE}/public/DersPlan/DersPlanDetay/{PLAN_ID}"
PLAN_INDEX = f"{BASE}/public/DersPlan/DersPlanlariList?planTipiKodu=lisans&programKodu={PROGRAM}"
SCHEDULE = f"{BASE}/public/DersProgram"
LEVEL = "programSeviyeTipiAnahtari=LS"
TERM_URL = f"{SCHEDULE}/GetAktifDonemByProgramSeviye?{LEVEL}"
BRANCH_URL = f"{SCHEDULE}/SearchBransKoduByProgramSeviye?{LEVEL}"
RULES_INDEX = f"{BASE}/public/GenelTanimlamalar/DersOnsartList"
ING100_SOURCE = "https://ninova.itu.edu.tr/tr/dersler/yabanci-diller-yuksekokulu/25503/ing-100/form"
ING100_ELIGIBILITY = "English proficiency eligibility must be confirmed."
CODE = re.compile(r"[A-Z]{2,8} [0-9]{3,4}[A-Z]{0,4}")
TERM_LABEL = re.compile(r"(\d{4})\s*-\s*(\d{4})\s+(Güz|Bahar|Yaz) Dönemi")
DAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
EMPTY = {"", "-", "--", "---", "-/-", "--/--"}
TIME = re.compile(r"(\d{1,2}):(\d{2})\s*/\s*(\d{1,2}):(\d{2})")


class SourceError(ValueError):
    """The public source was unavailable or returned an unexpected structure."""


@dataclass(frozen=True)
class Course:
    code: str
    title: str
    language: str
    credits: Decimal
    ects: Decimal


@dataclass(frozen=True)
class Requirement:
    semester: int
    position: int
    category: str
    title: str
    course_codes: tuple[str, ...]
    elective_group: str | None = None
    unresolved_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    code: str
    course_rule: str
    standing_rule: str
    source_url: str


@dataclass(frozen=True, order=True)
class Meeting:
    weekday: int
    start_minute: int
    end_minute: int


@dataclass(frozen=True)
class Section:
    crn: int
    course_code: str
    instruction: str
    instructor: str
    building: str
    room: str
    quota: int | None
    enrolled: int | None
    allowed_programs: tuple[str, ...] | None
    meetings: tuple[Meeting, ...]
    source_url: str
    no_prerequisite_reported: bool = False


@dataclass(frozen=True)
class Snapshot:
    program: str
    plan_id: int
    plan_title: str
    term_label: str
    term_year: int
    season: str
    observed_at: datetime
    courses: tuple[Course, ...]
    requirements: tuple[Requirement, ...]
    rules: tuple[Rule, ...]
    sections: tuple[Section, ...]
    source_urls: tuple[str, ...]


def fetch_public(url: str) -> str:
    if not _is_obs(url):
        raise SourceError(f"Unexpected source URL: {url}")
    request = Request(
        url,
        headers={
            "User-Agent": "AcademicCoursePlanner/1.0 (public curriculum and timetable importer)",
            "Accept-Language": "tr",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                if not _is_obs(response.url):
                    raise SourceError(f"Unexpected redirect from {url}")
                limit = 5_000_000
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise SourceError(f"Response too large: {url}")
                return body.decode("utf-8-sig")
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise SourceError(f"HTTP {error.code}: {url}") from error
            retry_after = error.headers.get("Retry-After")
            if retry_after and (not retry_after.isdigit() or int(retry_after) > 30):
                raise SourceError(f"Source requested a later retry: {url}") from error
            sleep(max(2**attempt, int(retry_after or 0)))
        except (URLError, OSError, HTTPException) as error:
            if attempt == 2:
                raise SourceError(f"Request failed: {url}") from error
            sleep(2**attempt)
        except UnicodeError as error:
            raise SourceError(f"Expected a UTF-8 response: {url}") from error
    raise AssertionError("Request retry loop ended unexpectedly")


def _is_obs(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == "obs.itu.edu.tr"


def collect_snapshot(fetch: Callable[[str], str] = fetch_public) -> Snapshot:
    observed_at = datetime.now(UTC)
    sources = set()

    def get(url: str) -> str:
        if sources and fetch is fetch_public:
            sleep(0.5)
        sources.add(url)
        return fetch(url)

    label, year, season = parse_term(get(TERM_URL))
    index = BeautifulSoup(get(PLAN_INDEX), "html.parser")
    if not index.select_one(f'a[href="/public/DersPlan/DersPlanDetay/{PLAN_ID}"]'):
        raise SourceError(f"Plan {PLAN_ID} is no longer listed for {PROGRAM}")
    title, courses, requirements = _parse_plan(get(PLAN_URL), get)
    branches = sorted({course.code.split()[0] for course in courses})
    schedule_branches = _schedule_branches(get(BRANCH_URL))
    rule_branches = _rule_branches(get(RULES_INDEX))
    codes = {course.code for course in courses}
    sections = {}
    rules = {}
    for branch in branches:
        if branch in schedule_branches:
            url = f"{SCHEDULE}/DersProgramSearch?{LEVEL}&dersBransKoduId={schedule_branches[branch]}"
            for section in parse_sections(get(url), codes, url):
                _add_unique(sections, section.crn, section, "section CRN")
        if branch in rule_branches:
            params = urlencode({"DersBransKoduId": rule_branches[branch]})
            url = f"{BASE}/public/GenelTanimlamalar/OnsartAra?{params}"
            for rule in _parse_rules(get(url), codes, url):
                _add_unique(rules, rule.code, rule, "prerequisite rule")
    if not sections or not any(PROGRAM in (section.allowed_programs or ()) for section in sections.values()):
        raise SourceError(f"No eligible {PROGRAM} sections found in the published term")
    if parse_term(get(TERM_URL)) != (label, year, season):
        raise SourceError("The active term changed during collection. Retry the import.")
    applicable = {}
    for section in sections.values():
        if PROGRAM in (section.allowed_programs or ()) and section.quota and section.meetings:
            applicable.setdefault(section.course_code, []).append(section)
    for code, candidates in applicable.items():
        if code not in rules and all(section.no_prerequisite_reported for section in candidates):
            rules[code] = Rule(code, "", "", candidates[0].source_url)
    if "ING 100" in codes:
        rules["ING 100"] = Rule("ING 100", "", ING100_ELIGIBILITY, ING100_SOURCE)
    return Snapshot(
        PROGRAM, PLAN_ID, title, label, year, season, observed_at,
        tuple(sorted(courses, key=lambda course: course.code)), tuple(requirements),
        tuple(rules[code] for code in sorted(rules)),
        tuple(sections[crn] for crn in sorted(sections)), tuple(sorted(sources)),
    )


def parse_term(body: str) -> tuple[str, int, str]:
    try:
        label = json.loads(body)["aktifDonem"]
        match = TERM_LABEL.fullmatch(label)
    except (ValueError, KeyError, TypeError) as error:
        raise SourceError("Missing active undergraduate term") from error
    if not match or int(match[2]) != int(match[1]) + 1:
        raise SourceError(f"Unrecognized undergraduate term: {label!r}")
    season = {"Güz": "Fall", "Bahar": "Spring", "Yaz": "Summer"}[match[3]]
    return label, int(match[1] if season == "Fall" else match[2]), season


def _parse_plan(body: str, fetch: Callable[[str], str]) -> tuple[str, list[Course], list[Requirement]]:
    soup = BeautifulSoup(body, "html.parser")
    heading = soup.find("h1", string=re.compile(r"Endüstri Mühendisliği \(İngilizce\) Lisans Ders Planı"))
    edition = heading.find_next("h2") if heading else None
    if edition is None or "2021-2022" not in _text(edition):
        raise SourceError(f"Unexpected curriculum title for plan {PLAN_ID}")

    courses: dict[str, Course] = {}
    requirements = []
    semesters = []
    for table in soup.select("table.datalist"):
        if not _has_header(table, "Ders Kodu", "Z/S", "Kredi", "AKTS"):
            continue
        semester_heading = table.find("h2")
        match = re.fullmatch(r"(\d+)\.\s+Yarıyıl", _text(semester_heading)) if semester_heading else None
        if not match:
            raise SourceError("Missing semester heading in curriculum")
        semester = int(match[1])
        semesters.append(semester)
        rows = table.select("tbody > tr")
        if not rows:
            raise SourceError(f"Empty semester {semester} in curriculum")
        for position, row in enumerate(rows, start=1):
            cells = row.find_all("td", recursive=False)
            if len(cells) != 10 or _text(cells[3]) not in {"Z", "S"}:
                raise SourceError(f"Malformed curriculum row in semester {semester}")
            title = _text(cells[1])
            category = _text(cells[9])
            unresolved = ()
            if _text(cells[3]) == "Z":
                alternatives = [
                    _course(_text(link), title, _text(cells[2]), _text(cells[4]), _text(cells[5]))
                    for link in cells[0].find_all("a")
                ]
                group = None
            else:
                link = cells[0].find("a", href=True)
                path = link["href"] if link else ""
                group_match = re.fullmatch(r"/public/DersPlan/_DersGrupSearch\?grupId=(\d+)&dersTipiId=\d+", path)
                if not group_match:
                    raise SourceError(f"Missing elective group link in semester {semester}")
                group = group_match[1]
                alternatives, unresolved = _parse_electives(fetch(urljoin(BASE, path)))
            if not alternatives:
                raise SourceError(f"No courses for {title!r} in semester {semester}")
            for course in alternatives:
                existing = courses.setdefault(course.code, course)
                if (existing.credits, existing.ects) != (course.credits, course.ects):
                    raise SourceError(f"Inconsistent credits for {course.code}")
            requirements.append(Requirement(
                semester, position, category, title,
                tuple(dict.fromkeys(course.code for course in alternatives)), group, unresolved,
            ))
    if semesters != list(range(1, 9)):
        raise SourceError("Expected all eight semesters of the ENDE_LS curriculum")
    return _text(edition), list(courses.values()), requirements


def _parse_electives(body: str) -> tuple[list[Course], tuple[str, ...]]:
    soup = BeautifulSoup(body, "html.parser")
    table = _table(soup, "Ders", "Dil", "Kredi", "AKTS")
    courses = []
    unresolved = []
    for row in table.select("tbody > tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 7:
            raise SourceError("Malformed elective group row")
        parts = list(cells[0].stripped_strings)
        if len(parts) < 2:
            raise SourceError("Missing code or title in elective group")
        code, *title_parts = parts
        title = " ".join(title_parts)
        if re.fullmatch(r"[A-Z]{2,8}", code):
            unresolved.append(f"{code} — {title}")
            continue
        courses.append(_course(code, title, *(_text(cell) for cell in cells[1:4])))
    return courses, tuple(sorted(set(unresolved)))


def _course(code: str, title: str, language: str, credits: str, ects: str) -> Course:
    if not CODE.fullmatch(code) or not title:
        raise SourceError(f"Malformed course: {code!r}")
    try:
        values = [Decimal(value.replace(",", ".")) for value in (credits, ects)]
        if any(not value.is_finite() or value < 0 for value in values):
            raise InvalidOperation
    except InvalidOperation as error:
        raise SourceError(f"Invalid credit values for {code}") from error
    return Course(code, title, language, *values)


def _schedule_branches(body: str) -> dict[str, int]:
    try:
        branches = {row["dersBransKodu"]: int(row["bransKoduId"]) for row in json.loads(body)}
    except (ValueError, KeyError, TypeError) as error:
        raise SourceError("Invalid schedule branch list") from error
    if "END" not in branches:
        raise SourceError("Endüstri Mühendisliği branch missing from the schedule list")
    return branches


def _rule_branches(body: str) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    branches = {
        _text(option): option["value"]
        for option in soup.select("#DersBransKoduId option[value]")
        if option["value"].isdigit()
    }
    if "ING" not in branches:
        raise SourceError("Invalid prerequisite branch list")
    return branches


def _parse_rules(body: str, codes: set[str], source: str) -> list[Rule]:
    soup = BeautifulSoup(body, "html.parser")
    table = _table(soup, "Ders Kodu", "Ders Önşartları", "Başarılan Kredi/Sınıf Önşartı")
    rules = []
    for row in table.select("tbody > tr"):
        cells = row.find_all("td")
        if len(cells) != 4:
            raise SourceError(f"Malformed prerequisite row: {source}")
        link = cells[0].find("a")
        code = _text(link) if link else _text(cells[0])
        if code in codes:
            rules.append(Rule(code, _text(cells[2]), _text(cells[3]), source))
    return rules


def parse_sections(body: str, codes: set[str], source: str) -> tuple[Section, ...]:
    soup = BeautifulSoup(body, "html.parser")
    table = _table(soup, "CRN", "Ders Kodu", "Gün", "Saat", "Dersi Alabilen Programlar")
    headers = [_text(cell) for cell in table.select("thead tr")[0].find_all(["th", "td"])]
    needed = (
        "CRN", "Ders Kodu", "Öğretim Yöntemi", "Öğretim Üyesi", "Bina", "Derslik",
        "Kontenjan", "Yazılan", "Dersi Alabilen Programlar", "Gün", "Saat",
    )
    if set(needed) - set(headers):
        raise SourceError(f"Missing timetable columns: {source}")
    sections: dict[int, Section] = {}
    for row in table.select("tbody > tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(headers):
            raise SourceError(f"Malformed timetable row: {source}")
        values = dict(zip(headers, map(_text, cells)))
        code = values["Ders Kodu"]
        if code not in codes:
            continue
        if not values["CRN"].isdigit():
            raise SourceError(f"Missing CRN for {code}: {source}")
        crn = int(values["CRN"])
        programs = values["Dersi Alabilen Programlar"]
        allowed = tuple(sorted(set(programs.replace(",", " ").split()))) if programs not in EMPTY else None
        section = Section(
            crn, code, values["Öğretim Yöntemi"], values["Öğretim Üyesi"], values["Bina"],
            values["Derslik"], _count(values["Kontenjan"]), _count(values["Yazılan"]), allowed,
            _meetings(values["Gün"], values["Saat"], crn), source,
            values.get("Ders Önşartları") == "-"
            and values.get("Başarılan Kredi/Sınıf Önşartı") == "-",
        )
        if crn in sections:
            other = sections[crn]
            if replace(other, meetings=()) != replace(section, meetings=()):
                raise SourceError(f"Conflicting rows for CRN {crn}: {source}")
            section = replace(section, meetings=tuple(sorted(set(other.meetings + section.meetings))))
        sections[crn] = section
    return tuple(sections.values())


def _count(value: str) -> int | None:
    if value in EMPTY:
        return None
    if not value.isdigit():
        raise SourceError(f"Invalid timetable count: {value!r}")
    return int(value)


def _meetings(days: str, times: str, crn: int) -> tuple[Meeting, ...]:
    if days in EMPTY and times in EMPTY:
        return ()
    day_tokens = days.split()
    ranges = list(TIME.finditer(times))
    if not ranges or TIME.sub("", times).strip() or len(day_tokens) != len(ranges):
        raise SourceError(f"Unexpected day/time layout for CRN {crn}: {days!r} / {times!r}")
    meetings = set()
    for day, match in zip(day_tokens, ranges, strict=True):
        hour, minute, end_hour, end_minute = map(int, match.groups())
        start, end = hour * 60 + minute, end_hour * 60 + end_minute
        if day not in DAYS or max(minute, end_minute) >= 60 or not 0 <= start < end <= 1440:
            raise SourceError(f"Invalid meeting for CRN {crn}")
        meetings.add(Meeting(DAYS.index(day), start, end))
    return tuple(sorted(meetings))


def _text(tag: Tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split())


def _has_header(table: Tag, *required: str) -> bool:
    return set(required) <= {_text(cell) for cell in table.select("thead th, thead td")}


def _table(soup: BeautifulSoup, *headers: str) -> Tag:
    for table in soup.find_all("table"):
        if _has_header(table, *headers):
            return table
    raise SourceError(f"Expected table with columns {', '.join(headers)}")


def _add_unique(target: dict, key: str | int, value: object, label: str) -> None:
    existing = target.setdefault(key, value)
    if existing != value:
        raise SourceError(f"Conflicting {label}: {key}")
