"""Series labels — the key that ties a daily quote to a real ISIN.

The daily quote sheet names bonds only by series label ("10.00%2030A").
Auction press releases print the same label beside the ISIN
("10.00%2030 'A'"  ->  LKB00530H016). Normalising both to one canonical
string is therefore what lets quotes be joined to securities.

The two sources differ in punctuation, in zero padding ("09.50" vs "9.50"),
and in whether the series letter is quoted, so canonical form fixes all
three. Step-coupon bonds from the 2023 restructuring chain several rates
before the year ("12%9%2027A", "12.4%7.5%5%2029A"); every rate is kept, so
two bonds differing only in a later step stay distinct.

    "10.00%2030A"       -> "10.00%2030A"
    "10.00%2030 'A'"    -> "10.00%2030A"
    "09.50%2030 'A'"    -> "9.50%2030A"
    "12.4%7.5%5%2029A"  -> "12.40%7.50%5.00%2029A"
"""

import re

from pipeline import dates

# One or more "<rate>%" groups, then the maturity year and series letter.
# The letter may be wrapped in straight or curly quotes, or absent.
LABEL_RE = re.compile(
    r"((?:\d{1,2}(?:\.\d{1,2})?\s*%\s*)+)(20\d{2})\s*[‘’'\"]?\s*([A-Z])?\s*[‘’'\"]?")

RATE_RE = re.compile(r"\d{1,2}(?:\.\d{1,2})?")


def normalise(label) -> str | None:
    """Canonical form of a printed series label, or None if not one."""
    if not label:
        return None
    match = LABEL_RE.search(dates.collapse_ws(str(label)))
    if not match:
        return None
    return canonical(match.group(1), match.group(2), match.group(3))


def canonical(rates_text: str, year: str, letter: str | None) -> str:
    """Build the canonical label from its parts."""
    rates = [f"{float(rate):.2f}" for rate in RATE_RE.findall(rates_text)]
    return "%".join(rates) + f"%{year}" + (letter or "")


def coupon_steps(label) -> list[float]:
    """Every coupon rate in a label; one entry unless it is step-coupon."""
    if not label:
        return []
    match = LABEL_RE.search(dates.collapse_ws(str(label)))
    if not match:
        return []
    return [float(rate) for rate in RATE_RE.findall(match.group(1))]
