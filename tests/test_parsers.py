"""Parser tests against real committed fixtures (downloaded from the PDMO
site on 2026-08-31). Every expected value below was cross-checked by hand
against the file's own contents during sample inspection."""

from datetime import date
from pathlib import Path

import pytest

from pipeline import parse_daily, parse_trade_summary

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Daily summary (.xls)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def daily():
    return parse_daily.parse_daily_summary(FIXTURES / "daily_summary_2026-08-31.xls")


def test_daily_dates(daily):
    # Published Monday 31.08 with quotes FOR 31.08; the transaction data
    # covers Friday 28.08.
    assert daily.reporting_date == date(2026, 8, 31)
    assert daily.trading_date == date(2026, 8, 28)


def test_daily_quote_count(daily):
    # 92 live quote rows; the matured 22.50%2025A and 8.00%2025A linger in
    # the sheet with all-zero prices and must be skipped, not stored.
    assert len(daily.quotes) == 92
    assert daily.skipped_rows == 2


def test_daily_one_ordinary_bond(daily):
    # 11.70%2034A: maturity 15.10.2034 — the bond behind LKB00934J156.
    quote = next(q for q in daily.quotes
                 if q["maturity_date"] == date(2034, 10, 15))
    assert quote["coupon_pct"] == 11.70
    # Canonical label; ordinary bonds have no separate printed form to keep.
    assert quote["series_label"] == "11.70%2034A"
    assert quote["printed_label"] is None
    assert quote["bid_yield"] == pytest.approx(11.60, abs=0.001)
    assert quote["offer_yield"] == pytest.approx(11.43125, abs=0.001)
    assert quote["bid_price"] == pytest.approx(100.486, abs=0.001)


def test_daily_step_coupon_bond(daily):
    # A 2023-restructuring bond: several coupon steps chained in the label.
    # Every step is kept in the canonical form so two bonds differing only
    # in a later step stay distinct; the printed label is preserved too.
    quote = next(q for q in daily.quotes
                 if q["series_label"] == "12.40%7.50%5.00%2029A")
    assert quote["printed_label"] == "12.4%7.5%5%2029A"
    assert quote["coupon_pct"] == 12.4  # first step stands in as the coupon
    assert quote["maturity_date"] == date(2029, 3, 15)


def test_daily_yields_are_percent(daily):
    for quote in daily.quotes:
        for key in ("bid_yield", "offer_yield"):
            if quote[key] is not None:
                assert 1 < quote[key] < 30  # fractions would be < 1


# ---------------------------------------------------------------------------
# Volumes (.xls) — fixture chosen for its broken title
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def volumes():
    return parse_daily.parse_volumes(FIXTURES / "volumes_2025-12-19_typo_title.xls")


def test_volumes_derived_date_beats_typo_title(volumes):
    # The title inside this file says "12.18.2025" but the data is for
    # 19.12.2025 (the previous day's file also says 12.18 — one is a typo).
    # maturity - remaining_years*365 recovers the true date from the data.
    assert "12.18.2025" in volumes.title
    assert volumes.derived_date == date(2025, 12, 19)


def test_volumes_rows(volumes):
    assert len(volumes.rows) == 16
    first = volumes.rows[0]
    assert first["isin"] == "LKB00426E154"
    assert first["maturity_date"] == date(2026, 5, 15)
    assert first["volume_lkr"] == 495_000_000
    # File's own Total row says 10,390 mn.
    assert sum(r["volume_lkr"] for r in volumes.rows) == 10_390_000_000


# ---------------------------------------------------------------------------
# Trade summary (PDF)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def trades():
    return parse_trade_summary.parse(FIXTURES / "trade_summary_2026-08-28.pdf",
                                     label_date=date(2026, 8, 28))


def test_trades_date_and_count(trades):
    assert trades.obs_date == date(2026, 8, 28)
    # The PDF's own indicator block says 26 securities traded.
    assert len(trades.rows) == 26


def test_trades_total_volume_matches_pdf(trades):
    # Indicator block: Total Turnover 24,055 Rs.Mn.
    assert sum(r["volume_lkr"] for r in trades.rows) == 24_055_000_000


def test_trades_digit_grouping_repaired(trades):
    # This row prints as "2 ,800" in the PDF text layer.
    row = next(r for r in trades.rows if r["isin"] == "LKA09126K274")
    assert row["volume_lkr"] == 2_800_000_000
    assert row["n_trades"] == 5
    assert row["wavg_yield"] == 8.97


def test_trades_types(trades):
    kinds = {r["security_type"] for r in trades.rows}
    assert kinds == {"Tbill", "TBond"}


# ---------------------------------------------------------------------------
# Auction press releases (PDF) — two published shapes, one parser
# ---------------------------------------------------------------------------

def test_auction_table_wrapped_layout():
    """The January 2026 release is the awkward one: field names span three
    table rows and the column grid drifts between rows."""
    from pipeline import parse_auction
    auction = parse_auction.parse(FIXTURES / "auction_2026-01-12_wrapped.pdf")
    assert auction.kind == "auction"
    assert auction.auction_date == date(2026, 1, 12)
    assert auction.settlement_date == date(2026, 1, 16)
    assert len(auction.bonds) == 4

    first = auction.bonds[0]
    assert first["isin"] == "LKB00530C017"
    assert first["series_label"] == "9.50%2030A"   # printed "09.50%2030 'A'"
    assert first["coupon_pct"] == 9.50
    assert first["way_pct"] == 9.74
    assert first["offered_lkr"] == 50_000_000_000
    assert first["bids_lkr"] == 123_051_000_000

    # This auction was only partly filled — offered 75,000mn, accepted 54,791mn.
    partial = auction.bonds[2]
    assert partial["offered_lkr"] == 75_000_000_000
    assert partial["accepted_lkr"] == 54_791_000_000


def test_auction_issuance_prose_layout():
    """Issuance-window releases state ISINs and yields in sentences, with a
    line break falling inside the phrase the parser searches for."""
    from pipeline import parse_auction
    auction = parse_auction.parse(FIXTURES / "auction_2026-07-30_issuance.pdf")
    assert auction.kind == "issuance"
    assert auction.auction_date == date(2026, 7, 30)
    assert [b["isin"] for b in auction.bonds] == [
        "LKB00531B017", "LKB00934J156", "LKB01136H151", "LKB01237G019"]
    assert [b["way_pct"] for b in auction.bonds] == [11.90, 12.42, 12.91, 13.01]
    # Prose releases never print the series label.
    assert all(b["series_label"] is None for b in auction.bonds)


def test_auction_rejects_unrelated_pdf():
    from pipeline import parse_auction
    from pipeline.parse_daily import ParseError
    with pytest.raises(ParseError):
        parse_auction.parse(FIXTURES / "trade_summary_2026-08-28.pdf")


def test_auction_announcement():
    """Announcements are the richest reference source: series label beside
    ISIN, plus issue date, coupon dates and accrued interest, which exist
    nowhere else in the published data."""
    from pipeline import parse_auction
    announcement = parse_auction.parse_announcement(
        FIXTURES / "announcement_2026-08-25.pdf")
    assert announcement.auction_date == date(2026, 8, 25)
    assert announcement.settlement_date == date(2026, 9, 1)
    assert len(announcement.bonds) == 2

    first = announcement.bonds[0]
    assert first["isin"] == "LKB00530H016"
    assert first["series_label"] == "10.00%2030A"
    assert first["coupon_pct"] == 10.0
    assert first["issue_date"] == date(2025, 8, 1)
    assert first["coupon_dates"] == "02-01,08-01"   # printed "01 February & 01 August"
    assert first["offered_lkr"] == 30_000_000_000
    # Accrued 0.8424 per 100 == 5.00 * 31/184: actual/actual, semi-annual,
    # 31 days from the 01 August coupon to the 01 September settlement.
    assert first["accrued_per_100"] == pytest.approx(5.0 * 31 / 184, abs=1e-4)


def test_announcement_rejects_a_result_release():
    from pipeline import parse_auction
    from pipeline.parse_daily import ParseError
    with pytest.raises(ParseError):
        parse_auction.parse_announcement(FIXTURES / "auction_2026-01-12_wrapped.pdf")
