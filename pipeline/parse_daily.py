"""Parsers for the two daily .xls workbooks (never mutates anything on disk).

Both parsers take a path to a cached file and return plain data
(dataclasses of dates + lists of dicts). They know nothing about the
database — pipeline/ingest.py decides what to do with the results. That
split keeps them re-runnable against the cache forever and trivially
testable against committed fixtures.

Everything defensive in here answers a quirk observed in real files
(docs/DATA_NOTES.md): headers are located by content instead of position,
yields arriving as fractions are converted to percent, matured bonds'
all-zero rows are skipped, corrupted maturity cells fall back to the
ISIN-encoded maturity, and the volumes observation date is *derived* from
the remaining-years arithmetic because the file's own title lies.
"""

import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

import xlrd

from pipeline import dates, db, isin

log = logging.getLogger(__name__)


class ParseError(Exception):
    """A file we cannot make sense of. Callers record it, never crash."""


# Series labels in the quote sheet: "11.25%2026A" for ordinary bonds, but the
# 2023-restructuring step-coupon bonds chain several rates: "12%9%2027A",
# "12.4%7.5%5%2029A". One or more NUM% segments, then maturity year + letter.
SERIES_RE = re.compile(r"((?:\d{1,2}(?:\.\d{1,2})?\s*%\s*)+)(20\d{2})\s*([A-Z])?")


@dataclass
class DailySummary:
    trading_date: date          # the business day the transaction data covers
    reporting_date: date        # publication date; the quotes are FOR this day
    update_serial: float | None # raw Excel timestamp of the last save
    quotes: list[dict] = field(default_factory=list)
    skipped_rows: int = 0       # matured/zero rows we intentionally dropped


@dataclass
class Volumes:
    derived_date: date | None   # median of maturity - remaining_years*365
    title: str                  # raw title line, for the parse log
    rows: list[dict] = field(default_factory=list)


def _open_workbook(path):
    try:
        return xlrd.open_workbook(str(path))
    except Exception as error:  # xlrd raises many types; all mean "not ours"
        raise ParseError(f"cannot open as .xls: {error}") from error


def _labelled_serial(sheet, label: str) -> float | None:
    """Find a row whose text contains `label` and return its last number.

    The Main Menu sheet lays dates out as [' REPORTING DATE', ..., 46265.0];
    scanning by label survives the row moving around.
    """
    for row_number in range(sheet.nrows):
        values = sheet.row_values(row_number)
        text = " ".join(str(v) for v in values if isinstance(v, str))
        if label.lower() in text.lower():
            numbers = [v for v in values if isinstance(v, (int, float)) and v]
            if numbers:
                return float(numbers[-1])
    return None


def parse_daily_summary(path) -> DailySummary:
    """Extract the per-bond two-way quotes and the authoritative dates."""
    book = _open_workbook(path)
    try:
        menu = book.sheet_by_name("Main Menu")
        quotes_sheet = book.sheet_by_name("QuotesTBond")
    except xlrd.XLRDError as error:
        raise ParseError(f"expected sheets missing: {error}") from error

    trading = dates.from_excel_serial(_labelled_serial(menu, "DATE OF TRADING"))
    reporting = dates.from_excel_serial(_labelled_serial(menu, "REPORTING DATE"))
    if reporting is None:
        raise ParseError("no REPORTING DATE on the Main Menu sheet")
    if trading is None:
        log.warning("%s: no DATE OF TRADING; using reporting date", path)
        trading = reporting
    result = DailySummary(trading, reporting,
                          _labelled_serial(menu, "UPDATE"))

    columns = _quote_columns(quotes_sheet)
    for row_number in range(quotes_sheet.nrows):
        values = quotes_sheet.row_values(row_number)
        series_match = None
        for cell in values:
            if isinstance(cell, str):
                series_match = SERIES_RE.search(cell)
                if series_match:
                    break
        if not series_match:
            continue

        quote = _parse_quote_row(values, columns, series_match, reporting, path)
        if quote is None:
            result.skipped_rows += 1
        else:
            result.quotes.append(quote)

    if not result.quotes:
        raise ParseError("QuotesTBond sheet yielded no bond rows")
    return result


def _quote_columns(sheet) -> dict[str, int]:
    """Locate the quote table's columns by their header text."""
    wanted = {  # our name -> phrase that identifies the header cell
        "tenor": "maturity period",
        "maturity": "maturity date",
        "days": "days to maturity",
        "bid_price": "average buying price",
        "offer_price": "average selling price",
    }
    for row_number in range(min(sheet.nrows, 15)):
        values = sheet.row_values(row_number)
        found: dict[str, int] = {}
        yields: list[int] = []
        for column, cell in enumerate(values):
            if not isinstance(cell, str):
                continue
            text = dates.collapse_ws(cell).lower()
            for name, phrase in wanted.items():
                if phrase in text:
                    found[name] = column
            if text == "yield":
                yields.append(column)
        if len(found) == len(wanted) and len(yields) >= 2:
            # The two Yield columns sit right of their price columns.
            found["bid_yield"] = min(c for c in yields if c > found["bid_price"])
            found["offer_yield"] = min(c for c in yields if c > found["offer_price"])
            return found
    raise ParseError("could not locate the QuotesTBond header row")


def _to_percent(value) -> float | None:
    """Yields arrive as fractions (0.0932 = 9.32%); convert and sanity-check."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    if value > 1:  # would mean a 100%+ yield — format drift, refuse to guess
        log.warning("yield cell %r larger than 1 (not a fraction?) — dropped", value)
        return None
    return round(value * 100, 6)


def _parse_quote_row(values, columns, series_match, reporting, path) -> dict | None:
    def cell(name):
        column = columns[name]
        return values[column] if column < len(values) else None

    bid_price, offer_price = cell("bid_price"), cell("offer_price")
    bid_yield = _to_percent(cell("bid_yield"))
    offer_yield = _to_percent(cell("offer_yield"))
    if bid_yield is None and offer_yield is None:
        return None  # matured bonds linger with all-zero rows — drop them

    # Maturity: the typed serial normally; if the cell is corrupted, rebuild
    # it from the reporting date + days-to-maturity column.
    maturity = dates.from_excel_serial(cell("maturity"))
    if maturity is None:
        days = cell("days")
        if isinstance(days, (int, float)) and days > 0:
            maturity = reporting + timedelta(days=int(days))
            log.warning("%s: bad maturity cell for %s; derived %s from "
                        "days-to-maturity", path, series_match.group(0), maturity)
    if maturity is None:
        return None

    # Step-coupon bonds carry several rates ("12%9%2027A"): record the first
    # step as coupon_pct and keep the full label so nothing is lost.
    #
    # NO ISIN here, deliberately. The sheet's "Maturity Period (Years)"
    # column does NOT match the tenor digits inside real ISINs (verified:
    # LKB00934F154 encodes 9, the column says 8), so an ISIN cannot be
    # rebuilt from this sheet. ingest.py joins each quote to a real ISIN —
    # learned from the volumes/trade-summary files — via maturity + coupon.
    coupon_steps = re.findall(r"\d{1,2}(?:\.\d{1,2})?", series_match.group(1))
    label = dates.collapse_ws(series_match.group(0))

    return {
        "coupon_pct": float(coupon_steps[0]),
        "series_label": label if len(coupon_steps) > 1 else None,
        "maturity_date": maturity,
        "bid_price": float(bid_price) if isinstance(bid_price, (int, float)) and bid_price > 0 else None,
        "offer_price": float(offer_price) if isinstance(offer_price, (int, float)) and offer_price > 0 else None,
        "bid_yield": bid_yield,
        "offer_yield": offer_yield,
    }


def parse_volumes(path) -> Volumes:
    """Extract per-ISIN traded volumes and derive the observation date.

    The observation date is NOT taken from the title: titles flip between
    day-first and month-first and contain at least one plain typo. Instead,
    every row satisfies maturity - remaining_years*365 = observation date
    (the sheet computes remaining years as days/365), so the median across
    rows recovers the date from the data itself.
    """
    book = _open_workbook(path)
    sheet = book.sheet_by_index(0)

    header_row, isin_column = None, None
    title = ""
    for row_number in range(min(sheet.nrows, 8)):
        for column, cell in enumerate(sheet.row_values(row_number)):
            if isinstance(cell, str):
                if "volumes on" in cell.lower():
                    title = dates.collapse_ws(cell)
                if cell.strip().upper() == "ISIN":
                    header_row, isin_column = row_number, column
        if header_row is not None:
            break
    if header_row is None:
        raise ParseError("no ISIN header row found")
    # Columns sit in fixed order right of ISIN: maturity, remaining, amount.
    remaining_column, amount_column = isin_column + 2, isin_column + 3

    result = Volumes(derived_date=None, title=title)
    derived: list[date] = []
    for row_number in range(header_row + 1, sheet.nrows):
        values = sheet.row_values(row_number)
        raw_isin = values[isin_column] if isin_column < len(values) else ""
        if not isinstance(raw_isin, str) or not raw_isin.strip():
            continue
        if "total" in raw_isin.lower():
            break
        decoded = isin.decode(raw_isin)
        if decoded is None:
            log.warning("%s: unrecognised ISIN %r — row dropped", path, raw_isin)
            continue
        _, maturity = decoded  # ISIN-encoded maturity: immune to bad cells

        amount = values[amount_column] if amount_column < len(values) else None
        if not isinstance(amount, (int, float)) or amount <= 0:
            log.warning("%s: unusable amount %r for %s — row dropped",
                        path, amount, raw_isin)
            continue

        remaining = values[remaining_column] if remaining_column < len(values) else None
        if isinstance(remaining, (int, float)) and 0 < remaining < 50:
            derived.append(maturity - timedelta(days=round(remaining * 365)))

        result.rows.append({
            "isin": raw_isin.strip(),
            "maturity_date": maturity,
            "volume_lkr": db.lkr_from_millions(float(amount)),
        })

    if derived:
        result.derived_date = statistics.median_low(derived)
    if not result.rows:
        raise ParseError(f"no volume rows parsed (title: {title!r})")
    return result
