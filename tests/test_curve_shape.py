"""Curve-shape tests: a spread must follow the bonds, not just the model,
and its history must be scored the same honest way as every other signal."""

import pandas as pd
import pytest

from pipeline import db
from signals import curve_shape
from signals import report as signal_report
from signals import run as signal_run

FLAT = {"beta0": 10.0, "beta1": 0.0, "beta2": 0.0, "lambda_years": 2.82}


def _quotes(rows):
    return pd.DataFrame(rows, columns=["tau_years", "residual_bp", "weight"])


def test_a_kink_the_curve_cannot_follow_still_shows():
    """April-May 2026 in miniature: the bonds near 2 years cheap to the curve,
    those near 5 years rich. The curve alone reads 2s5s as flat; the bonds
    say it is 40bp inverted, and the spread must say so too."""
    quotes = _quotes([(1.9, 20.0, 1.0), (2.1, 20.0, 1.0),
                      (4.9, -20.0, 1.0), (5.1, -20.0, 1.0)])
    two = curve_shape.point_yield(FLAT, quotes, 2.0)
    five = curve_shape.point_yield(FLAT, quotes, 5.0)
    assert (five - two) * 100.0 == pytest.approx(-40.0, abs=1.0)


def test_a_point_with_no_bond_anywhere_near_is_the_plain_curve():
    """Not the nearest bond's residual: a 30-year bond says nothing about
    where 2 years should be."""
    quotes = _quotes([(30.0, 50.0, 1.0)])
    assert curve_shape.point_yield(FLAT, quotes, 2.0) == pytest.approx(10.0)


def _seed(conn, days=45):
    """A sloping curve and four bonds, the 10-year drifting steeper daily."""
    for index in range(days):
        day = (pd.Timestamp("2026-01-01") + pd.tseries.offsets.BDay(index)).date().isoformat()
        conn.execute(
            """INSERT INTO curve_fits (obs_date, beta0, beta1, beta2, lambda_years,
                   n_quotes, rmse_bp, n_trades, fitted_at)
               VALUES (?, 12.0, -3.0, 1.0, 2.82, 4, 5.0, 0, 'x')""", (day,))
        for isin, tau, residual in (("A", 2.0, 0.0), ("B", 5.0, 0.0),
                                    ("C", 8.0, 0.0), ("D", 10.0, index * 0.5),
                                    ("E", 12.0, 0.0)):
            conn.execute(
                """INSERT INTO curve_residuals (obs_date, isin, source, tau_years,
                       observed_yield, fitted_yield, residual_bp, weight)
                   VALUES (?, ?, 'quote', ?, 10.0, 10.0, ?, 1.0)""",
                (day, isin, tau, residual + (index % 3)))
    conn.commit()


def test_every_day_is_stored_and_the_z_never_sees_today(tmp_path):
    conn = db.connect(tmp_path / "shape.sqlite")
    _seed(conn)
    signal_run.rebuild(conn)
    rows = pd.read_sql_query(
        "SELECT * FROM curve_spreads WHERE pair = '2s10s' ORDER BY obs_date", conn)

    # History is kept from the first day, z-score or not.
    assert len(rows) == 45
    assert rows["zscore"].iloc[:30].isna().all()
    # The last day's norm is the window BEFORE it.
    last = rows.iloc[-1]
    window = rows["spread_bp"].iloc[:-1].tail(60)
    assert last["mean_bp"] == pytest.approx(window.mean())
    # The 10-year drifts steeper every day, so today is above its norm.
    assert last["zscore"] > 0
    assert conn.execute("SELECT COUNT(DISTINCT pair) c FROM curve_spreads"
                        ).fetchone()["c"] == len(curve_shape.PAIRS)


def test_one_bad_quote_is_not_counted_as_the_spread_coming_back(tmp_path):
    """A spread that is wrong for one day and right the next has not
    reverted — nobody could have traded the wrong day. Measured from the
    signal day itself, every such blip would score as a full capture."""
    conn = db.connect(tmp_path / "blip.sqlite")
    rows = []
    for index in range(120):
        blip = index % 4 == 0
        rows.append({"obs_date": f"d{index:03d}", "pair": "2s5s",
                     "spread_bp": 130.0 if blip else 100.0,
                     "zscore": 3.0 if blip else 0.0})
    stored = curve_shape.measure_reversion(conn, pd.DataFrame(rows))
    assert stored["2s5s"] == pytest.approx(0.0)


def test_the_report_puts_each_spread_beside_its_cost(tmp_path, capsys):
    conn = db.connect(tmp_path / "report.sqlite")
    _seed(conn)
    signal_run.rebuild(conn)
    day = conn.execute("SELECT MAX(obs_date) d FROM curve_spreads").fetchone()["d"]
    signal_report._curve_shape(conn, day)
    printed = capsys.readouterr().out
    assert "CURVE SHAPE" in printed
    for short, long in curve_shape.PAIRS:
        assert curve_shape.pair_name(short, long) in printed
    # Too little history to measure reversion yet, and the report says so
    # rather than calling the spread weak.
    assert "not measured yet" in printed
