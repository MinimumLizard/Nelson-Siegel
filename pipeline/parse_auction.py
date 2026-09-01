"""Parser for Treasury bond auction press releases (PDF).

Two document shapes appear under the same index title, so the parser
decides from the content, not the link text:

* **auction result** — "TREASURY BOND AUCTION HELD ON 25 AUGUST 2026",
  a table laid out one COLUMN PER BOND (fields run down the side):

      Series                        10.00%2030 'A'   11.50%2035 'A'
      Date of Maturity              01 August 2030   15 March 2035
      ISINs                         LKB00530H016     LKB02035C155
      Coupon Rate (p.a.) (%)        10.00            11.50
      Amount Offered (Rs.Mn)        30,000           20,000
      Bids Received (Rs.Mn)         120,525          114,410
      Amount Accepted (Rs. Mn)      30,000           20,000
      Weighted Average Yield Rate   10.54            11.70

* **issuance window** — "TREASURY BOND ISSUANCE HELD ON 30 July 2026", a
  follow-up written as prose, naming the ISINs and their yields in
  sentences rather than a table.

Why this matters beyond the auction data itself: the auction result is the
only source that states a **series label together with its ISIN**
("10.00%2030 'A'" -> LKB00530H016). The daily quote sheet identifies bonds
by that same label but never gives the ISIN, so these releases are what let
quotes be tied to real securities.

Two layout hazards, both real (compare the August and January 2026
releases), shape the approach:

* field names wrap across several table rows, and the values can sit on a
  row of their own BETWEEN two halves of the label ("Coupon Rate" / values
  / "(p.a.) (%)"), so a row's label is read from the rows around it;
* in the prose releases a line break can fall inside the very phrase we
  search for ("Weighted Average\\nYield Rates of"), so prose is matched
  against whitespace-collapsed text.

The table grid also drifts between rows (in January 2026 the series labels
sit in columns 4/7/11/14 while the ISINs sit in 4/8/11/14), so values are
read as "the non-empty cells from the first bond column rightwards" rather
than from fixed positions. A release with two bonds and one with four are
then handled by the same code.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from pipeline import dates, db, isin as isin_mod
from pipeline.parse_daily import ParseError

log = logging.getLogger(__name__)

# Series label as printed in a release: "10.00%2030 'A'" (curly or straight
# quotes, optional spaces). The quote sheet writes the same bond as
# "10.00%2030A"; both normalise to one key — see normalise_series().
SERIES_RE = re.compile(
    r"(\d{1,2}(?:\.\d{1,2})?)\s*%\s*(20\d{2})\s*[‘’'\"]?\s*([A-Z])\s*[‘’'\"]?")

ISIN_RE = re.compile(r"LKB\d{5}[A-L]\d{3}")
NUMBER_RE = re.compile(r"^\d[\d,]*(?:\.\d+)?$")
SPELLED_DATE_RE = re.compile(r"\d{1,2}\s+[A-Za-z]+\s+\d{4}")


@dataclass
class Auction:
    auction_date: date
    kind: str                      # 'auction' (table) | 'issuance' (prose)
    settlement_date: date | None = None
    bonds: list[dict] = field(default_factory=list)


def normalise_series(label: str) -> str | None:
    """Canonical form of a series label, comparable across sources.

    "10.00%2030 'A'" (press release) and "10.00%2030A" (quote sheet) both
    become "10.00%2030A". Returns None if the text isn't a series label.
    """
    match = SERIES_RE.search(dates.collapse_ws(label))
    if not match:
        return None
    coupon, year, series = match.groups()
    return f"{float(coupon):.2f}%{year}{series}"


def _clean(cell) -> str:
    return "" if cell is None else dates.collapse_ws(str(cell))


def _to_number(text):
    text = _clean(text)
    if not NUMBER_RE.match(text):
        return None
    return float(text.replace(",", ""))


def _mn(text):
    """A "Rs.Mn" figure from a release -> integer rupees."""
    value = _to_number(text)
    return db.lkr_from_millions(value) if value is not None else None


def _bond_columns(table) -> tuple[int, list[str]]:
    """Where the bond data starts, and the ISINs, from the row of ISINs.

    Only the FIRST bond column is returned, not one index per bond: the
    grid drifts between rows (in the January 2026 release the series
    labels sit in columns 4/7/11/14 while the ISINs sit in 4/8/11/14), so
    values are read as "the non-empty cells from here rightwards" instead
    of from fixed column positions.
    """
    for row in table:
        found = [(index, ISIN_RE.search(_clean(cell)).group(0))
                 for index, cell in enumerate(row)
                 if ISIN_RE.search(_clean(cell))]
        if found:
            return found[0][0], [isin for _, isin in found]
    return -1, []


def _labels_of(table, row_index, first_column, span) -> str:
    """Left-hand label text of a row, optionally including its neighbours."""
    parts = []
    for index in range(max(0, row_index - span), min(len(table), row_index + span + 1)):
        parts += [_clean(cell) for cell in table[index][:first_column]]
    return dates.collapse_ws(" ".join(part for part in parts if part))


def _field_rows(table, first_column, count):
    """Rows carrying bond values, as (own_label, nearby_label, values).

    `values` are the non-empty cells from `first_column` rightwards, which
    survives the column drift described in _bond_columns. Two labels are
    kept because a wrapped field name lives on the neighbouring rows
    ("Coupon Rate" / values / "(p.a.) (%)") while an unwrapped one sits on
    the value row itself — and matching on neighbours first would let one
    field claim the next field's row.
    """
    rows = []
    for index, row in enumerate(table):
        values = [_clean(cell) for cell in row[first_column:] if _clean(cell)]
        if not values:
            continue
        rows.append((_labels_of(table, index, first_column, 0),
                     _labels_of(table, index, first_column, 1),
                     values[:count]))
    return rows


def _find_field(rows, keyword, converter):
    """Values of the row for `keyword`; exact-label rows win over wrapped.

    Pass 1 matches a row's own label, so in a release where every field
    name sits on its own value row nothing can be misattributed. Pass 2
    falls back to the neighbouring rows' text, which is what makes the
    wrapped layout work.
    """
    for label_index in (0, 1):
        for row in rows:
            if keyword.lower() in row[label_index].lower():
                return [converter(value) for value in row[2]]
    return None


def parse(path) -> Auction:
    """Read one press release into an Auction.

    Raises ParseError if the file is not a bond auction/issuance release.
    """
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            tables = [table for page in pdf.pages for table in page.extract_tables()]
    except Exception as error:
        raise ParseError(f"cannot open as PDF: {error}") from error

    flat = dates.collapse_ws(text)  # line breaks fall inside key phrases
    headline = next((line for line in text.splitlines()
                     if re.search(r"TREASURY BOND (AUCTION|ISSUANCE)", line, re.I)), None)
    if headline is None:
        raise ParseError("not a treasury bond auction/issuance release")

    auction_date = dates.parse_spelled(headline)
    if auction_date is None:
        raise ParseError(f"no auction date in headline {headline!r}")

    result = Auction(
        auction_date=auction_date,
        kind="issuance" if re.search(r"ISSUANCE", headline, re.I) else "auction",
    )
    settlement = re.search(r"date of settlement is ([^.]+)", flat, re.I)
    result.settlement_date = dates.parse_spelled(settlement.group(1)) if settlement else None

    table = next((candidate for candidate in tables if _bond_columns(candidate)[1]), None)
    if table is not None:
        _parse_table(table, result, path)
    else:
        _parse_prose(flat, result)

    if not result.bonds:
        raise ParseError("no bond ISINs found in the release")
    return result


def _parse_table(table, result: Auction, path) -> None:
    first_column, isins = _bond_columns(table)
    rows = _field_rows(table, first_column, len(isins))

    labels = _find_field(rows, "Series", normalise_series) or []
    maturities = _find_field(rows, "Maturity", dates.parse_spelled) or []
    coupons = _find_field(rows, "Coupon Rate", _to_number) or []
    offered = _find_field(rows, "Amount Offered", _mn) or []
    bids = _find_field(rows, "Bids Received", _mn) or []
    accepted = _find_field(rows, "Amount Accepted", _mn) or []
    yields = _find_field(rows, "Weighted Average", _to_number) or []

    def pick(values, index):
        return values[index] if index < len(values) else None

    for index, isin in enumerate(isins):
        _, maturity = isin_mod.decode(isin)
        printed = pick(maturities, index)
        if printed and printed != maturity:
            log.warning("%s: printed maturity %s disagrees with %s — trusting the ISIN",
                        path, printed, isin)
        result.bonds.append({
            "isin": isin,
            "series_label": pick(labels, index),
            "maturity_date": maturity,
            "coupon_pct": pick(coupons, index),
            "way_pct": pick(yields, index),
            "offered_lkr": pick(offered, index),
            "bids_lkr": pick(bids, index),
            "accepted_lkr": pick(accepted, index),
        })


def _parse_prose(flat: str, result: Auction) -> None:
    """Issuance-window releases: ISINs and yields stated in sentences."""
    isins = []
    for candidate in ISIN_RE.findall(flat):
        if candidate not in isins and isin_mod.decode(candidate):
            isins.append(candidate)

    # "... at the Weighted Average Yield Rates of 10.54% and 11.70%, ..."
    # NB: the yields themselves contain full stops, so the text after the
    # phrase is taken by length rather than up to the next period.
    yields = []
    match = re.search(r"Weighted Average\s+Yield Rates?\s+of", flat, re.I)
    if match:
        window = flat[match.end():match.end() + 300]
        yields = [float(value) for value in re.findall(r"(\d{1,2}\.\d{1,2})\s*%", window)]

    for index, isin in enumerate(isins):
        _, maturity = isin_mod.decode(isin)
        result.bonds.append({
            "isin": isin,
            "series_label": None,       # prose releases never print the series
            "maturity_date": maturity,
            "coupon_pct": None,
            "way_pct": yields[index] if index < len(yields) else None,
            "offered_lkr": None,
            "bids_lkr": None,
            "accepted_lkr": None,
        })
