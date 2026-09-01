"""Curve-fitting tests: the maths on synthetic data where the answer is
known, and the day-level fit against a small hand-built database."""

import datetime as dt

import numpy as np
import pytest

from curves import fit as curve_fit
from curves import nelson_siegel as ns
from pipeline import db


# ---------------------------------------------------------------------------
# The maths
# ---------------------------------------------------------------------------

TAU = np.array([0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20])


def test_recovers_known_parameters():
    betas, lam = np.array([12.0, -3.0, 2.0]), 2.5
    y = ns.predict(TAU, betas, lam)
    recovered, fitted, _ = ns.fit_fixed(TAU, y, lam)
    assert recovered == pytest.approx(betas, abs=1e-9)
    assert ns.weighted_rmse_bp(y, fitted) < 1e-6


def test_slope_factor_limit_at_zero():
    # f1 = (1-exp(-x))/x is 0/0 at tau=0; its limit is 1, and a NaN here
    # would poison the whole fit.
    slope, curvature = ns.factors(0.0, 2.5)
    assert slope == pytest.approx(1.0)
    assert curvature == pytest.approx(0.0)


def test_factors_vanish_at_long_maturities():
    # Both factors decay, which is why beta0 reads as the long-run level.
    slope, curvature = ns.factors(200.0, 2.5)
    assert slope < 0.02 and abs(curvature) < 0.02


def test_fixed_lambda_is_deterministic():
    y = ns.predict(TAU, [11.0, -2.0, 1.0], 3.0) + 0.01 * np.sin(TAU)
    first = ns.fit_fixed(TAU, y, 3.0)[0]
    second = ns.fit_fixed(TAU, y, 3.0)[0]
    assert first == pytest.approx(second, abs=0.0)


def test_weighting_pulls_the_fit_toward_tight_quotes():
    """A wide-spread outlier must not drag the curve the way a tight one would."""
    y = ns.predict(TAU, [12.0, -3.0, 2.0], 2.5)
    polluted = y.copy()
    polluted[4] += 2.0  # a 200bp bad quote

    trusted = np.ones_like(y)
    trusted[4] = 0.01   # ...but quoted with a very wide spread
    weighted, _, _ = ns.fit_fixed(TAU, polluted, 2.5, trusted)
    unweighted, _, _ = ns.fit_fixed(TAU, polluted, 2.5)

    truth = np.array([12.0, -3.0, 2.0])
    assert np.abs(weighted - truth).sum() < np.abs(unweighted - truth).sum()


def test_fit_needs_enough_bonds():
    with pytest.raises(ValueError):
        ns.fit_fixed([1.0, 2.0], [10.0, 11.0], 2.5)


# ---------------------------------------------------------------------------
# Weights from bid-offer spreads
# ---------------------------------------------------------------------------

def test_wide_spreads_get_less_weight():
    quotes = [{"spread_bp": 10.0}, {"spread_bp": 20.0}, {"spread_bp": 100.0}]
    weights = curve_fit.weights_from_spreads(quotes)
    assert weights[0] > weights[1] > weights[2]
    # 1/spread^2: doubling the spread quarters the weight.
    assert weights[0] / weights[1] == pytest.approx(4.0, rel=1e-6)


def test_missing_spread_falls_back_to_the_median():
    quotes = [{"spread_bp": 10.0}, {"spread_bp": 10.0}, {"spread_bp": None}]
    weights = curve_fit.weights_from_spreads(quotes)
    assert weights[2] == pytest.approx(weights[0])


def test_zero_spread_cannot_dominate():
    quotes = [{"spread_bp": 0.0}] + [{"spread_bp": 20.0}] * 5
    weights = curve_fit.weights_from_spreads(quotes)
    assert np.isfinite(weights).all()


# ---------------------------------------------------------------------------
# One day, end to end, against a database built for the test
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded(tmp_path):
    """A day of quotes lying exactly on a known curve, plus one traded bond."""
    conn = db.connect(tmp_path / "curves.sqlite")
    obs_date, betas, lam = "2026-08-28", [12.0, -3.0, 1.0], 2.822
    day = dt.date.fromisoformat(obs_date)

    for index, tau in enumerate([0.5, 1, 2, 3, 4, 6, 8, 10, 12]):
        isin = f"LKB009{30 + index}F15X"          # shape only; never decoded here
        maturity = day + dt.timedelta(days=round(tau * 365.25))
        yield_pct = float(ns.predict([tau], betas, lam)[0])
        db.upsert_bond(conn, isin, 10.0, maturity.isoformat(), 9, obs_date,
                       series_label=f"10.00%{maturity.year}A")
        db.upsert_quote(conn, obs_date, isin,
                        bid_yield=yield_pct + 0.08, offer_yield=yield_pct - 0.08,
                        bid_price=100.0, offer_price=100.0, raw_ref="test")
        if index == 3:  # one bond also traded, 20bp cheap to the curve
            db.upsert_trade_summary(conn, obs_date, isin, "TBond", None, None, None,
                                    None, yield_pct + 0.20, 1_000_000_000, 3, "test")
    conn.commit()
    return conn, obs_date, betas, lam


def test_fit_day_recovers_the_curve(seeded):
    conn, obs_date, betas, lam = seeded
    summary = curve_fit.fit_day(conn, obs_date, lam)
    assert summary["n_quotes"] == 9
    assert summary["rmse_bp"] < 0.5
    assert summary["beta0"] == pytest.approx(betas[0], abs=0.02)


def test_trades_are_held_out_and_measured(seeded):
    conn, obs_date, _, lam = seeded
    summary = curve_fit.fit_day(conn, obs_date, lam)
    # The traded bond was placed 20bp above the curve and must be reported
    # as such — and it must NOT have been fitted (n_quotes counts quotes only).
    assert summary["n_trades"] == 1
    assert summary["trade_bias_bp"] == pytest.approx(20.0, abs=1.0)
    assert summary["n_quotes"] == 9


def test_residual_sign_convention(seeded):
    """Positive residual means the bond yields MORE than the curve: cheap."""
    conn, obs_date, _, lam = seeded
    curve_fit.fit_day(conn, obs_date, lam)
    row = conn.execute(
        "SELECT * FROM curve_residuals WHERE obs_date=? AND source='trade'",
        (obs_date,)).fetchone()
    assert row["observed_yield"] > row["fitted_yield"]
    assert row["residual_bp"] > 0


def test_refitting_a_day_replaces_its_residuals(seeded):
    conn, obs_date, _, lam = seeded
    curve_fit.fit_day(conn, obs_date, lam)
    curve_fit.fit_day(conn, obs_date, lam)
    count = conn.execute("SELECT COUNT(*) c FROM curve_residuals").fetchone()["c"]
    assert count == 10  # 9 quotes + 1 trade, not doubled
