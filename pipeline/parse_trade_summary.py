"""Parser for the secondary-market trade summary PDFs (the only real PDFs).

One page, produced by print-to-PDF from Excel. pdfplumber's table
extraction handles it well; the residual hazards are cosmetic:

* number cells contain stray spaces from digit grouping ("2 ,800", "6 50");
* the title's numeric date ("...Trade Summary* 28.08.2026") is day-first in
  every sample, but after the volumes-title fiasco we don't take chances:
  both readings are computed and the index label (or plausibility) decides.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from pipeline import db, dates
from pipeline.parse_daily import ParseError

log = logging.getLogger(__name__)


@dataclass
class TradeSummary:
    obs_date: date
    rows: list[dict] = field(default_factory=list)


def _clean_number(cell) -> float | None:
    """'2 ,800' -> 2800.0; '-'/None/'' -> None."""
    if cell is None:
        return None
    text = re.sub(r"[ ,]", "", str(cell))
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    return float(text)


def _resolve_title_date(text: str, label_date: date | None) -> date:
    """Pick the report date from the page title, defensively."""
    match = re.search(r"Trade Summary\D{0,5}(\d{1,2}\.\d{1,2}\.\d{4})", text)
    if match:
        day_first, month_first = dates.parse_ambiguous_numeric(match.group(1))
        for candidate in (day_first, month_first):
            if candidate and candidate == label_date:
                return candidate
        if day_first:  # every sample so far is day-first
            return day_first
        if month_first:
            return month_first
    spelled = dates.parse_spelled(text)
    if spelled:
        return spelled
    if label_date:
        return label_date
    raise ParseError("no report date found in the PDF title or index label")


def parse(path, label_date: date | None = None) -> TradeSummary:
    """Extract per-ISIN executed trades. Yields are already in percent."""
    import pdfplumber  # local import: keeps xls-only workflows import-light

    try:
        pdf = pdfplumber.open(path)
    except Exception as error:
        raise ParseError(f"cannot open as PDF: {error}") from error

    with pdf:
        page_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        result = TradeSummary(_resolve_title_date(page_text, label_date))

        for page in pdf.pages:
            for table in page.extract_tables():
                _collect_rows(table, result, path)

    if not result.rows:
        raise ParseError("no ISIN table found in the PDF")
    return result


def _collect_rows(table, result: TradeSummary, path) -> None:
    """Append the rows of a table if (and only if) it is the ISIN table."""
    header_index = None
    for index, row in enumerate(table):
        cells = [dates.collapse_ws(cell or "") for cell in row]
        if any(cell.upper() == "ISIN" for cell in cells):
            header_index = index
            break
    if header_index is None:
        return

    # Column order is fixed in every sample:
    # No | ISIN | Tenure | Type | Open | Close | High | Low | WAvg | Volume | Trades
    # ...but cell BOUNDARIES drift: older PDFs fuse the row number and ISIN
    # into one cell ("1LKA36426K135") and leave the ISIN column empty. So
    # rows are read by content, not position: the ISIN is found by pattern
    # anywhere in the row, and the numbers are anchored on the Tbill/TBond
    # type cell, which reliably sits between the tenure and the yields.
    for row in table[header_index + 1:]:
        cells = [dates.collapse_ws(cell or "") for cell in row]
        joined = re.sub(r"\s", "", "".join(cells))
        isin_match = re.search(r"LK[A-Z]\d{5}[A-L]\d{3}", joined)
        if not isin_match:
            continue  # totals row, blank padding, etc.
        raw_isin = isin_match.group(0)
        type_index = next((index for index, cell in enumerate(cells)
                           if cell.lower() in ("tbill", "tbond")), None)
        if type_index is None:
            log.warning("%s: no security type for %s — dropped", path, raw_isin)
            continue
        # [open, close, high, low, wavg, volume_mn, n_trades] after the type
        numbers = [_clean_number(cell) for cell in cells[type_index + 1:]]
        numbers = [n for n in numbers if n is not None]
        if len(numbers) < 7:
            log.warning("%s: short trade row for %s — dropped", path, raw_isin)
            continue
        open_y, close_y, high_y, low_y, wavg, volume_mn, n_trades = numbers[:7]
        result.rows.append({
            "isin": raw_isin,
            "security_type": cells[type_index],
            "open_yield": open_y,
            "close_yield": close_y,
            "high_yield": high_y,
            "low_yield": low_y,
            "wavg_yield": wavg,
            "volume_lkr": db.lkr_from_millions(volume_mn),
            "n_trades": int(n_trades),
        })
