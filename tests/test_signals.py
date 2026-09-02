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


# ---------------------------------------------------------------------------
# Liquidity — which bonds are worth reporting a dislocation in at all
# ---------------------------------------------------------------------------

def _seed_liquidity(conn, isin, days, per_day_lkr=1_000_000_000, end="2026-09-01"):
    day = pd.Timestamp(end)
    for index in range(days):
        stamp = (day - pd.tseries.offsets.BDay(index)).date().isoformat()
        db.upsert_trade_summary(conn, stamp, isin, "TBond", None, None, None,
                                None, 11.0, per_day_lkr, 2, "t")


def test_illiquid_bond_is_not_tradeable(tmp_path):
    """The core fix: a bond that barely trades must not head the list, however
    large its z-score. Stale quotes jump when refreshed, so illiquid bonds
    score HIGHEST precisely where they are least dealable."""
    from signals import liquidity
    conn = db.connect(tmp_path / "liq.sqlite")
    _seed_liquidity(conn, "LKB00934F154", days=1)     # traded once
    _seed_liquidity(conn, "LKB01237G019", days=25)    # genuinely liquid
    conn.commit()

    facts = liquidity.profile(conn, "2026-09-01")
    assert not liquidity.is_tradeable(facts.get("LKB00934F154"))
    assert liquidity.is_tradeable(facts.get("LKB01237G019"))


def test_benchmark_clears_a_lower_bar_but_not_a_waiver(tmp_path):
    """Freshly auctioned paper is dealable before its trade record catches
    up — but a benchmark that never trades is still not tradeable."""
    from signals import liquidity
    conn = db.connect(tmp_path / "liq.sqlite")
    _seed_liquidity(conn, "LKB00531B017", days=4)     # thin, but auctioned
    conn.commit()
    for isin in ("LKB00531B017", "LKB01136H151"):     # the second never trades
        db.upsert_auction(conn, "2026-07-30", isin, "announced", "2026-08-03",
                          None, 1_000_000_000, None, None, "t")
    conn.commit()

    facts = liquidity.profile(conn, "2026-09-01")
    assert facts["LKB00531B017"]["is_benchmark"]
    assert liquidity.is_tradeable(facts["LKB00531B017"])      # 4 days >= 3
    assert not liquidity.is_tradeable(facts.get("LKB01136H151"))  # never traded


def test_liquidity_window_ignores_the_future(tmp_path):
    """Scoring a historical date must use only what was known then."""
    from signals import liquidity
    conn = db.connect(tmp_path / "liq.sqlite")
    _seed_liquidity(conn, "LKB00934F154", days=20, end="2026-09-01")
    conn.commit()
    early = liquidity.profile(conn, "2026-07-01")
    assert early.get("LKB00934F154") is None or early["LKB00934F154"]["days_traded"] == 0


def test_stale_bond_drops_out_as_the_window_moves(tmp_path):
    from signals import liquidity
    conn = db.connect(tmp_path / "liq.sqlite")
    _seed_liquidity(conn, "LKB00934F154", days=20, end="2026-03-01")
    conn.commit()
    assert liquidity.is_tradeable(liquidity.profile(conn, "2026-03-01").get("LKB00934F154"))
    # Six months on, the same trades are outside the 60-day window.
    assert not liquidity.is_tradeable(liquidity.profile(conn, "2026-09-01").get("LKB00934F154"))


def test_quote_is_not_attributed_on_maturity_alone():
    """Two series can mature on the same day — a step-coupon restructuring
    bond and an ordinary one both mature 15 Jan 2033. Attributing one's
    quotes to the other silently merges two bonds' price histories, so the
    coupon has to agree even when only one bond matches the maturity."""
    import datetime as dt
    from pipeline import ingest

    ordinary = {"isin": "LKB01533A154", "series_label": "11.20%2033",
                "maturity_date": "2033-01-15", "coupon_pct": 11.2}
    lookup = ({}, {}, {"2033-01-15": [ordinary]})

    good = {"series_label": "11.20%2033A", "maturity_date": dt.date(2033, 1, 15),
            "coupon_pct": 11.2}
    assert ingest._resolve_isin(lookup, good) == ("LKB01533A154", "maturity+coupon")

    # The step-coupon bond: same maturity, quite different coupon.
    stepped = {"series_label": "12.40%7.50%5.00%2033A",
               "maturity_date": dt.date(2033, 1, 15), "coupon_pct": 12.4}
    assert ingest._resolve_isin(lookup, stepped) == (None, "unresolved")
