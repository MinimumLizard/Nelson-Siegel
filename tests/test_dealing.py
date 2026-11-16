"""Dealing-level tests: the forecast must follow the rule it claims, use
nothing from the future, and be scored by the same code that makes it."""

import numpy as np
import pandas as pd
import pytest

from pipeline import db
from signals import dealing
from signals import report as signal_report


def _gaps(columns: dict) -> pd.DataFrame:
    """Gaps in bp, one row per trading day, NaN where a bond did not trade."""
    length = len(next(iter(columns.values())))
    return pd.DataFrame(columns, index=[f"d{i:02d}" for i in range(length)])


NAN = float("nan")
# Five bonds trading at +10 on the last two days make the market gap +10.
MARKET = {name: [NAN] * 8 + [10.0, 10.0] for name in "ABCDE"}


def test_a_bond_that_traded_on_the_latest_day_deals_where_it_dealt():
    gaps = _gaps(MARKET | {"X": [NAN] * 9 + [30.0]})
    table = dealing.adjustments(gaps, last=9)
    assert table.at["X", "own_age_days"] == 0
    assert table.at["X", "adjustment_bp"] == pytest.approx(30.0)


def test_an_older_trade_counts_for_less_and_the_market_for_more():
    # X last traded four trading days before the latest: two half-lives.
    gaps = _gaps(MARKET | {"X": [NAN] * 5 + [30.0] + [NAN] * 4})
    table = dealing.adjustments(gaps, last=9)
    assert table.at["X", "own_age_days"] == 4
    assert table.at["X", "own_weight"] == pytest.approx(0.25)
    assert table.at["X", "adjustment_bp"] == pytest.approx(0.25 * 30.0 + 0.75 * 10.0)


def test_a_bond_that_has_never_traded_gets_the_market_gap():
    gaps = _gaps(MARKET | {"X": [NAN] * 10})
    assert dealing.adjustments(gaps, last=9).at["X", "adjustment_bp"] == pytest.approx(10.0)


def test_one_off_market_ticket_does_not_become_the_forecast():
    """2026-09-24's 10.70%2035A printed 68bp through its quote at an agreed
    level. Uncapped, that would be the next day's forecast for the bond."""
    gaps = _gaps(MARKET | {"X": [NAN] * 9 + [-58.0]})
    adjustment = dealing.adjustments(gaps, last=9).at["X", "adjustment_bp"]
    assert adjustment == pytest.approx(10.0 - dealing.MAX_OWN_OFFSET_BP)


def test_too_few_trades_is_no_forecast_rather_than_a_guess():
    gaps = _gaps({"A": [NAN] * 9 + [10.0], "B": [NAN] * 10})
    assert dealing.adjustments(gaps, last=9) is None


def _seed(conn, days, gap_bp=20.0, bonds="ABCDEF"):
    """`days` trading days on which every bond quotes 10.00% and trades
    `gap_bp` above it."""
    dates = [d.date().isoformat() for d in pd.bdate_range("2026-01-01", periods=days)]
    for day in dates:
        for isin in bonds:
            for source, level in (("quote", 10.0), ("trade", 10.0 + gap_bp / 100.0)):
                conn.execute(
                    """INSERT INTO curve_residuals (obs_date, isin, source, tau_years,
                           observed_yield, fitted_yield, residual_bp, weight)
                       VALUES (?, ?, ?, 5.0, ?, 10.0, 0.0, 1.0)""",
                    (day, isin, source, level))
    conn.commit()
    return dates


def test_the_forecast_never_uses_a_trade_from_after_its_date(tmp_path):
    conn = db.connect(tmp_path / "deal.sqlite")
    dates = _seed(conn, 12)
    before = dealing.forecast(conn, dates[5])
    # Later days now print somewhere completely different.
    conn.execute("""UPDATE curve_residuals SET observed_yield = 15.0
                     WHERE source = 'trade' AND obs_date > ?""", (dates[5],))
    conn.commit()
    after = dealing.forecast(conn, dates[5])
    assert after["bonds"] == before["bonds"]
    assert before["bonds"]["A"]["expected_yield"] == pytest.approx(10.20)
    assert before["trades_through"] == dates[5] and before["fresh"]


def test_the_backtest_scores_the_same_rule_the_page_shows(tmp_path, monkeypatch):
    """A market that always trades 20bp over its screen: the screen is 20bp
    out every time, and the forecast, knowing only earlier trades, is not."""
    monkeypatch.setattr(dealing, "BACKTEST_START", 5)
    conn = db.connect(tmp_path / "back.sqlite")
    _seed(conn, 30)
    scored = dealing.backtest(conn)
    summary = dealing.summarise(scored)
    assert summary[1]["screen_mae_bp"] == pytest.approx(20.0)
    assert summary[1]["mae_bp"] == pytest.approx(0.0, abs=1e-9)
    assert summary[2]["mae_bp"] == pytest.approx(0.0, abs=1e-9)
    assert np.isfinite(scored["error"]).all()


def test_the_report_names_the_dealing_level_and_how_stale_it_is(tmp_path):
    conn = db.connect(tmp_path / "line.sqlite")
    dates = _seed(conn, 12)
    # The last day's trade file has not arrived.
    conn.execute("DELETE FROM curve_residuals WHERE source = 'trade' AND obs_date = ?",
                 (dates[-1],))
    conn.commit()
    deals = dealing.forecast(conn, dates[-1])
    assert not deals["fresh"] and deals["trades_through"] == dates[-2]
    line = signal_report.describe_dealing(deals, dates[-1])
    assert "20bp above the quote mid" in line
    assert "not in yet" in line
    # Carry is entered at the market's part of the forecast, for every bond.
    assert signal_report.entry_gap(deals, None) == pytest.approx(20.0)


def test_the_backtest_cannot_see_the_day_it_is_scoring(tmp_path, monkeypatch):
    """A market whose gap flips between 0 and 20bp every day. Knowing only
    yesterday, the forecast is 20bp out every single time; knowing the day
    before yesterday it is exactly right. A backtest that peeked at the
    day being scored would report a perfect score for both."""
    monkeypatch.setattr(dealing, "BACKTEST_START", 5)
    conn = db.connect(tmp_path / "flip.sqlite")
    dates = _seed(conn, 30, gap_bp=0.0)
    for index, day in enumerate(dates):
        if index % 2:
            conn.execute("""UPDATE curve_residuals SET observed_yield = 10.20
                             WHERE source = 'trade' AND obs_date = ?""", (day,))
    conn.commit()
    summary = dealing.summarise(dealing.backtest(conn))
    assert summary[1]["mae_bp"] == pytest.approx(20.0)
    assert summary[2]["mae_bp"] == pytest.approx(0.0, abs=1e-9)
