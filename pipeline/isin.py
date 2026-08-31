"""Sri Lankan treasury-bond ISINs: decode, synthesise, and verify.

Verified against every ISIN in the inspected volumes files (check digits
included) — see docs/DATA_NOTES.md. The layout of e.g. LKB00934F154:

    LK  B  009  34  F  15  4
    |   |   |   |   |   |   +-- standard ISIN (Luhn) check digit
    |   |   |   |   |   +----- maturity day (15)
    |   |   |   |   +--------- maturity month, A=Jan ... L=Dec (F=June)
    |   |   |   +------------- maturity year, 20YY (2034)
    |   |   +----------------- original tenor in years, zero-padded (9)
    |   +--------------------- B = treasury bond (bills use LKA...)
    +------------------------- country code

Why this matters: the daily summary's quote table has NO ISIN column, only
a tenor and a maturity date — which is exactly enough to rebuild the ISIN.
"""

import re
from datetime import date

BOND_ISIN_RE = re.compile(r"^LKB\d{3}\d{2}[A-L]\d{2}\d$")


def check_digit(body11: str) -> str:
    """Standard ISIN check digit (Luhn over letters expanded to two digits)."""
    digits = "".join(str(ord(ch) - 55) if ch.isalpha() else ch for ch in body11)
    total, double = 0, True  # rightmost digit gets doubled first
    for ch in reversed(digits):
        value = int(ch)
        if double:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        double = not double
    return str((10 - total % 10) % 10)


def build(tenor_years: int, maturity: date) -> str:
    """Tenor + maturity -> full 12-character bond ISIN, check digit included."""
    body = (f"LKB{tenor_years:03d}{maturity.year % 100:02d}"
            f"{chr(64 + maturity.month)}{maturity.day:02d}")
    return body + check_digit(body)


def decode(isin: str) -> tuple[int, date] | None:
    """Bond ISIN -> (tenor_years, maturity date); None if it doesn't parse
    or the check digit is wrong (a mangled cell, not a real ISIN)."""
    isin = isin.strip().upper()
    if not BOND_ISIN_RE.match(isin):
        return None
    if check_digit(isin[:11]) != isin[11]:
        return None
    tenor = int(isin[3:6])
    year = 2000 + int(isin[6:8])
    month = ord(isin[8]) - 64
    day = int(isin[9:11])
    try:
        return tenor, date(year, month, day)
    except ValueError:
        return None
