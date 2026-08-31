"""Date parsing for the PDMO sources — small, but where most of the danger is.

Sample inspection (docs/DATA_NOTES.md) found four different ways a date can
reach us, in descending order of trustworthiness:

1. Excel serial numbers in cells (Main Menu dates, maturity dates) — typed
   data, reliable apart from the odd corrupted cell.
2. Index-page labels ("as at 19.12.2025", "> 31.08.2026") — consistently
   day-first, but with stray spaces inside ("16 .04.2026", "26.01.202 6").
3. The volumes-file arithmetic: maturity − remaining_years × 365 lands on
   the true observation date exactly (the sheet computes remaining years as
   days/365) — a derived date we use to validate the labels.
4. Free-text titles inside files — the WORST source: "28 August 2026",
   "16 .01.2026" (day-first) and "12.18.2025" (month-first!) all appear,
   plus at least one outright typo. Only used as a last resort.

All functions return datetime.date or None; nothing here raises on bad
input, because one malformed date must never kill a backfill run.
"""

import re
from datetime import date, timedelta

# Excel's day zero (the 1900 date system, as used by these workbooks).
EXCEL_EPOCH = date(1899, 12, 30)

# The archive starts late 2025 and bonds mature out to ~2050: any serial
# outside this window is a corrupted cell, not a real date.
SANE_MIN = date(2000, 1, 1)
SANE_MAX = date(2070, 1, 1)

MONTH_NAMES = {name.lower(): number for number, name in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=0)}


def collapse_ws(text: str) -> str:
    """Squash all whitespace runs to single spaces ("16 .04.2026" stays
    broken though — digit-level repair happens in the parsers below)."""
    return " ".join(str(text).split())


def from_excel_serial(value) -> date | None:
    """Excel serial number -> date, or None if absent/corrupted."""
    try:
        serial = int(float(value))
    except (TypeError, ValueError):
        return None
    parsed = EXCEL_EPOCH + timedelta(days=serial)
    if not (SANE_MIN <= parsed <= SANE_MAX):
        return None
    return parsed


def _build(day: int, month: int, year: int) -> date | None:
    if year < 100:
        year += 2000
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    if not (SANE_MIN <= parsed <= SANE_MAX):
        return None
    return parsed


def parse_dmy(text: str) -> date | None:
    """First day-first numeric date in `text` ("19.12.2025", "> 16 .04.2026").

    Whitespace inside the date is dropped before matching, which is how the
    index pages' split-digit dates ("26.01.202 6") come out right. Use ONLY
    on sources verified to be day-first (the index pages).
    """
    squeezed = re.sub(r"\s+", "", str(text))
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", squeezed)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    return _build(day, month, year)


def parse_spelled(text: str) -> date | None:
    """Dates with a spelled-out month: "28 August 2026", "15 June 2026"."""
    match = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})",
                      collapse_ws(text))
    if not match:
        return None
    month = MONTH_NAMES.get(match.group(2).lower())
    if not month:
        return None
    return _build(int(match.group(1)), month, int(match.group(3)))


def parse_ambiguous_numeric(text: str) -> tuple[date | None, date | None]:
    """A numeric date of unknown order -> (day-first reading, month-first).

    The volumes titles mix "16.01.2026" (day-first) with "12.18.2025"
    (month-first), so a caller gets BOTH readings and must decide using
    outside evidence (the derived date, or the index label). A reading that
    is impossible (month 18) comes back as None.
    """
    squeezed = re.sub(r"\s+", "", str(text))
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", squeezed)
    if not match:
        return (None, None)
    first, second, year = (int(part) for part in match.groups())
    return (_build(first, second, year), _build(second, first, year))
