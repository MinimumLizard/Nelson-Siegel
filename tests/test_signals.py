"""Signal tests. The first one matters most: a z-score must never have seen
the observation it is scoring, or the stored history is not a backtest."""

import numpy as np
import pandas as pd
import pytest

from pipeline import db
from signals import report as signal_report
from signals import run as signal_run
from signals import zscore


def _residuals(values, isin="LKB00934F154", tau=8.0, start="2026-01-01"):
    dates = pd.bdate_range(start, periods=len(values))
    return pd.DataFrame({"obs_date": dates, "isin": isin,
                         "residual_bp": values, "tau_years": tau})


def test_zscore_never_sees_the_value_it_scores():
    """The window ends the day BEFORE, so a spike cannot flatten its own z."""
    settled = [9.0, 11.0] * 20                 # mean 10, sd 1
    frame = _residuals(settled + [40.0])       # 30bp spike on the last day
    signals = zscore.bond_signals(frame)
    last = signals.iloc[-1]

    # The window holds only the pre-spike days, so its mean is still 10.
    assert last["mean_bp"] == pytest.approx(10.0)
    assert last["residual_bp"] == pytest.approx(40.0)
    # Including the spike would have pulled the mean up and the z down.
    assert last["zscore"] == pytest.approx(30.0 / last["sd_bp"], rel=1e-6)
    assert last["zscore"] > 25


def test_window_statistics_match_a_hand_computation():
    rng = np.random.default_rng(0)
    values = list(rng.normal(5.0, 3.0, 45))
    signals = zscore.bond_signals(_residuals(values))
    last = signals.iloc[-1]

    window = pd.Series(values[:-1]).tail(zscore.WINDOW_DAYS)
    assert last["mean_bp"] == pytest.approx(window.mean())
    assert last["sd_bp"] == pytest.approx(window.std())
    assert last["zscore"] == pytest.approx((values[-1] - window.mean()) / window.std())


def test_no_signal_until_enough_history():
    signals = zscore.bond_signals(_residuals([1.0, 2.0, 3.0] * 5))  # 15 days
    assert signals.empty


def test_near_zero_spread_does_not_manufacture_signals():
    """A bond pinned at one value has ~0 sd; dividing by it would give
    enormous z-scores out of rounding noise."""
    values = [10.0] * 40 + [10.2]
    signals = zscore.bond_signals(_residuals(values))
    assert signals.empty or signals["zscore"].isna().all()


def test_pairs_limited_to_nearby_maturities():
    frame = pd.concat([
        _residuals([1.0] * 40, isin="LKB00527I150", tau=2.0),
        _residuals([2.0] * 40, isin="LKB00934F154", tau=3.5),   # 1.5y apart
        _residuals([3.0] * 40, isin="LKB02039H156", tau=13.0),  # far away
    ])
    pairs = zscore.candidate_pairs(frame)
    assert ("LKB00527I150", "LKB00934F154") in pairs
    assert all("LKB02039H156" not in pair for pair in pairs)


def test_switch_spread_and_sign():
    """Positive z means A got cheap versus B — the trade is buy A, sell B."""
    wobble = [-1.0, 1.0] * 20                  # spread varies, so sd > 0
    frame = pd.concat([
        _residuals(wobble + [25.0], isin="LKB00527I150", tau=2.0),
        _residuals([0.0] * 41, isin="LKB00934F154", tau=3.0),
    ])
    switches = zscore.switch_signals(frame)
    last = switches.sort_values("obs_date").iloc[-1]
    assert last["isin_a"] == "LKB00527I150"     # sorted order
    assert last["spread_bp"] == pytest.approx(25.0)
    assert last["zscore"] > 0                   # A cheap vs B


def test_expected_capture_is_stepped_by_z():
    assert signal_report._expected_capture(0.5) == 0.0
    assert signal_report._expected_capture(-2.5) == signal_report.EXPECTED_REVERSION_BP[2.0]
    assert signal_report._expected_capture(4.0) == signal_report.EXPECTED_REVERSION_BP[3.0]


def test_rebuild_is_idempotent(tmp_path):
    """Signals are recomputed wholesale, so running twice must not double up."""
    conn = db.connect(tmp_path / "signals.sqlite")
    for index in range(45):
        day = (pd.Timestamp("2026-01-01") + pd.tseries.offsets.BDay(index)).date().isoformat()
        for isin, tau, base in (("LKB00527I150", 2.0, 5.0), ("LKB00934F154", 3.0, -5.0)):
            db.upsert_bond(conn, isin, 10.0, "2030-01-01", 5, day)
            conn.execute(
                """INSERT INTO curve_residuals (obs_date, isin, source, tau_years,
                       observed_yield, fitted_yield, residual_bp, weight)
                   VALUES (?, ?, 'quote', ?, 10.0, 10.0, ?, 1.0)""",
                (day, isin, tau, base + (index % 7)))
    conn.commit()

    first = signal_run.rebuild(conn)
    second = signal_run.rebuild(conn)
    assert first == second
    stored = conn.execute("SELECT COUNT(*) c FROM bond_signals").fetchone()["c"]
    assert stored == first["bond_signals"]
