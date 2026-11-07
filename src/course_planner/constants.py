from __future__ import annotations

from pathlib import Path

GRADE_CREDITS = {1: 30, 2: 60, 3: 95}
SEMESTER_LST = ["Spring", "Summer", "Fall"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
ALL_LECTURES_PATH = DATA_DIR / "all_lectures.csv"
COURSE_PLANS_PATH = DATA_DIR / "course_plans.csv"

DAY_MAP = {
    "Pazartesi": 0,
    "Salı": 1,
    "Çarşamba": 2,
    "Perşembe": 3,
    "Cuma": 4,
}
WEEKDAYS = list(DAY_MAP.keys())

DISPLAY_START_HOUR = 8
DISPLAY_END_HOUR = 22
MAX_SOLUTIONS = 5
