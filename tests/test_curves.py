"""Curve-fitting tests: the maths on synthetic data where the answer is
known, and the day-level fit against a small hand-built database."""

import datetime as dt

import numpy as np
import pytest

from curves import fit as curve_fit
from curves import nelson_siegel as ns
from pipeline import db, isin


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


# ---------------------------------------------------------------------------
# Late-arriving trades — the curve's only out-of-sample check
# ---------------------------------------------------------------------------

def _quoted_day(conn, obs_date, count=12):
    """A day with enough quotes to fit, and nothing else.

    ISINs are built rather than hard-coded so the check digits are real and
    `bonds` gets a genuine maturity for each.
    """
    day = dt.date.fromisoformat(obs_date)
    for index in range(count):
        maturity = dt.date(day.year + 2 + index, 6, 15)
        bond = isin.build(2 + index, maturity)
        coupon = 10.0 + index * 0.25
        db.upsert_bond(conn, bond, coupon, maturity.isoformat(), 2 + index, obs_date,
                       series_label=f"{coupon:.2f}%{maturity.year}A")
        level = 10.0 + index * 0.18
        db.upsert_quote(conn, obs_date, bond, bid_yield=level + 0.08,
                        offer_yield=level - 0.08, bid_price=100.0,
                        offer_price=100.0, raw_ref="t")
    conn.commit()
    return [isin.build(2 + i, dt.date(day.year + 2 + i, 6, 15)) for i in range(count)]


def test_a_day_fitted_before_its_trades_arrive_is_refitted_when_they_do(tmp_path):
    """The trade file is published AFTER the quote sheet it belongs with, so a
    curve fitted the evening quotes land has nothing to check itself against.
    Only unfitted days used to be revisited, which dropped those trades for
    good — and did it silently, exactly when the check mattered most."""
    conn = db.connect(tmp_path / "late.sqlite")
    bonds = _quoted_day(conn, "2026-09-16")
    curve_fit.fit_day(conn, "2026-09-16")
    conn.commit()
    assert conn.execute("SELECT n_trades FROM curve_fits").fetchone()["n_trades"] == 0
    assert curve_fit.dates_with_late_trades(conn) == set()      # nothing has arrived yet

    # The next morning the trade summary for that day lands.
    db.upsert_trade_summary(conn, "2026-09-16", bonds[0], "TBond",
                            None, None, None, None, 11.90, 1_000_000_000, 3, "t")
    conn.commit()
    assert curve_fit.dates_with_late_trades(conn) == {"2026-09-16"}
    assert "2026-09-16" in curve_fit.available_dates(conn, only_new=True)

    curve_fit.fit_day(conn, "2026-09-16")
    conn.commit()
    row = conn.execute("SELECT n_trades, trade_bias_bp FROM curve_fits").fetchone()
    assert row["n_trades"] == 1
    assert row["trade_bias_bp"] is not None
    # Settled: the day must not be refitted on every subsequent run.
    assert curve_fit.dates_with_late_trades(conn) == set()
    assert "2026-09-16" not in curve_fit.available_dates(conn, only_new=True)


def test_an_unusable_trade_does_not_cause_a_refit_every_run(tmp_path):
    """A trade in a bond the curve excludes leaves n_trades at 0 legitimately.
    Comparing against `load_trades` rather than a raw row count keeps that day
    settled instead of refitting it forever."""
    conn = db.connect(tmp_path / "late.sqlite")
    _quoted_day(conn, "2026-09-16")
    db.upsert_bond(conn, "LKB00931E153", 12.4, "2031-05-15", 9, "2026-09-16",
                   series_label="12.40%7.50%5.00%2031A")     # step-coupon: excluded
    db.upsert_trade_summary(conn, "2026-09-16", "LKB00931E153", "TBond",
                            None, None, None, None, 11.5, 1_000_000_000, 1, "t")
    conn.commit()
    curve_fit.fit_day(conn, "2026-09-16")
    conn.commit()
    assert conn.execute("SELECT n_trades FROM curve_fits").fetchone()["n_trades"] == 0
    assert curve_fit.dates_with_late_trades(conn) == set()


def test_trade_check_age_flags_a_stale_check(tmp_path):
    """The newest day legitimately has no trades. A check several days old is
    a fault, and must not be shown as though it were today's reading."""
    conn = db.connect(tmp_path / "late.sqlite")
    for day in ("2026-09-10", "2026-09-16"):
        bonds = _quoted_day(conn, day)
        curve_fit.fit_day(conn, day)
    db.upsert_trade_summary(conn, "2026-09-10", bonds[0], "TBond",
                            None, None, None, None, 11.90, 1_000_000_000, 3, "t")
    conn.commit()
    curve_fit.fit_day(conn, "2026-09-10")
    conn.commit()

    fresh = curve_fit.trade_check_age(conn, "2026-09-11")
    assert fresh["obs_date"] == "2026-09-10" and fresh["days_old"] == 1
    assert not fresh["stale"]

    stale = curve_fit.trade_check_age(conn, "2026-09-17")
    assert stale["days_old"] == 7 and stale["stale"]
    assert curve_fit.trade_check_age(db.connect(tmp_path / "empty.sqlite"), "2026-09-17") is None


# ---------------------------------------------------------------------------
# Calibration must not move the model by being measured
# ---------------------------------------------------------------------------

def test_calibrating_lambda_does_not_store_it(tmp_path):
    """Measuring lambda used to persist it as a side effect, so any diagnostic
    that asked what lambda would be silently changed the model underneath a
    stored history of residuals. Storing is now `--calibrate`'s own step."""
    conn = db.connect(tmp_path / "cal.sqlite")
    for day in ("2026-09-10", "2026-09-11"):
        _quoted_day(conn, day)
    before = curve_fit.get_lambda(conn)

    chosen = curve_fit.calibrate_lambda(conn, ["2026-09-10", "2026-09-11"])
    assert curve_fit.get_lambda(conn) == before      # untouched by measuring

    curve_fit.store_lambda(conn, chosen)
    assert curve_fit.get_lambda(conn) == pytest.approx(chosen)


def test_calibration_prefers_a_steadier_lambda_over_a_marginally_better_fit(tmp_path):
    """The grid minimum is the wrong objective: on the real cross-section it
    buys 2% of pooled error and doubles beta0's largest daily move. The rule
    takes the smallest lambda within STABILITY_TOLERANCE of the best."""
    conn = db.connect(tmp_path / "cal.sqlite")
    _quoted_day(conn, "2026-09-10")
    dates = ["2026-09-10"]

    chosen = curve_fit.calibrate_lambda(conn, dates)
    quotes, _ = curve_fit.load_day(conn, "2026-09-10")
    tau = np.array([q["tau"] for q in quotes])
    observed = np.array([q["yield"] for q in quotes])
    weights = curve_fit.weights_from_spreads(quotes)
    totals = {float(lam): ns.fit_fixed(tau, observed, lam, weights)[2]
              for lam in ns.LAMBDA_GRID}
    floor = min(totals.values())

    # Within tolerance of the best, and nothing smaller is.
    assert totals[chosen] <= floor * (1 + curve_fit.STABILITY_TOLERANCE)
    smaller = [lam for lam in totals if lam < chosen]
    assert all(totals[lam] > floor * (1 + curve_fit.STABILITY_TOLERANCE) for lam in smaller)
