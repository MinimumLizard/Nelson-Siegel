"""Carry, funding and the quote-to-trade gap.

These decide what a position EARNS while it is held, which for a levered
hold-to-maturity book matters more than a mean reversion that may not
arrive. The tests below pin the two things easiest to get quietly wrong:
that no rate from after the day being scored is ever used, and that the
duration adjustment is what discriminates between bonds.
"""

import datetime as dt

import pytest

from pipeline import db, isin
from signals import carry, execution

BOND = "LKB00934F154"          # 9y bond maturing 2034-06-15


def _bill(conn, obs_date, days_out, yield_pct, volume=1_000_000_000, tenor=364):
    """Record an executed bill trade maturing `days_out` days after obs_date."""
    maturity = dt.date.fromisoformat(obs_date) + dt.timedelta(days=days_out)
    db.upsert_trade_summary(conn, obs_date, isin.build_bill(tenor, maturity),
                            "Tbill", None, None, None, None, yield_pct, volume, 1, "t")


# ---------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------

def test_funding_is_the_twelve_month_bill_plus_the_desk_spread(tmp_path):
    conn = db.connect(tmp_path / "c.sqlite")
    _bill(conn, "2026-09-01", 360, 9.75)
    conn.commit()
    money = carry.funding_rate(conn, "2026-09-02")
    assert money["bill_pct"] == pytest.approx(9.75)
    assert money["rate_pct"] == pytest.approx(9.75 + carry.FUNDING_SPREAD_BP / 100.0)
    assert money["stale_days"] == 1


def test_funding_ignores_bills_away_from_the_one_year_point(tmp_path):
    """A 91-day bill is not a 12-month funding rate, however recently it traded."""
    conn = db.connect(tmp_path / "c.sqlite")
    _bill(conn, "2026-09-01", 60, 8.50, tenor=91)      # too short
    _bill(conn, "2026-09-01", 700, 11.00, tenor=364)   # too long
    conn.commit()
    assert carry.funding_rate(conn, "2026-09-02") is None

    _bill(conn, "2026-09-01", 300, 9.60)
    conn.commit()
    assert carry.funding_rate(conn, "2026-09-02")["bill_pct"] == pytest.approx(9.60)


def test_funding_never_uses_a_rate_from_after_the_day_being_scored(tmp_path):
    conn = db.connect(tmp_path / "c.sqlite")
    _bill(conn, "2026-09-01", 360, 9.00)
    _bill(conn, "2026-09-10", 360, 12.00)     # a later, much higher print
    conn.commit()
    assert carry.funding_rate(conn, "2026-09-02")["bill_pct"] == pytest.approx(9.00)


def test_funding_volume_weights_several_prints(tmp_path):
    """Bills near the one-year point print on about half of days, and usually
    only one at a time, so a single trade must not set the whole book's rate."""
    conn = db.connect(tmp_path / "c.sqlite")
    _bill(conn, "2026-08-31", 360, 10.00, volume=3_000_000_000)
    _bill(conn, "2026-09-01", 359, 9.00, volume=1_000_000_000)
    conn.commit()
    money = carry.funding_rate(conn, "2026-09-02")
    assert money["n_bills"] == 2
    assert money["bill_pct"] == pytest.approx(9.75)     # 3:1 weighted


def test_no_bill_means_no_carry_rather_than_a_guess(tmp_path):
    conn = db.connect(tmp_path / "c.sqlite")
    assert carry.funding_rate(conn, "2026-09-02") is None
    assert carry.profile(conn, "2026-09-02") == {}


# ---------------------------------------------------------------------------
# Price and duration
# ---------------------------------------------------------------------------

def test_a_bond_at_its_coupon_prices_at_par():
    price, _ = carry.price_and_duration(10.0, 10.0, 5.0)
    assert price == pytest.approx(100.0, abs=1e-6)


def test_duration_is_below_maturity_and_rises_with_it():
    _, short = carry.price_and_duration(11.0, 11.0, 4.0)
    _, long = carry.price_and_duration(11.0, 11.0, 10.0)
    assert short < 4.0 and long < 10.0        # coupons pull it in
    assert long > short
    # A lower coupon pays less back early, so it lasts longer.
    _, low_coupon = carry.price_and_duration(11.0, 5.0, 10.0)
    assert low_coupon > long


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------

@pytest.fixture
def priced(tmp_path):
    conn = db.connect(tmp_path / "c.sqlite")
    db.upsert_bond(conn, BOND, 11.0, "2034-06-15", 9, "2026-09-02")
    conn.execute(
        """INSERT INTO curve_fits (obs_date, beta0, beta1, beta2, lambda_years,
               n_quotes, rmse_bp, fitted_at)
           VALUES ('2026-09-02', 12.0, -3.0, 1.0, 2.822, 1, 7.0, '2026-09-02')""")
    conn.execute(
        """INSERT INTO curve_residuals (obs_date, isin, source, tau_years,
               observed_yield, fitted_yield, residual_bp, weight)
           VALUES ('2026-09-02', ?, 'quote', 7.8, 11.40, 11.40, 0.0, 1.0)""", (BOND,))
    _bill(conn, "2026-09-01", 360, 9.00)
    conn.commit()
    return conn


def test_carry_is_the_entry_yield_less_funding(priced):
    facts = carry.profile(priced, "2026-09-02")[BOND]
    funding = 9.00 + carry.FUNDING_SPREAD_BP / 100.0
    assert facts["carry_bp"] == pytest.approx((11.40 - funding) * 100.0)
    assert facts["duration"] > 0
    assert facts["per_duration_bp"] == pytest.approx(
        (facts["carry_bp"] + facts["roll_bp"]) / facts["duration"])


def test_the_entry_gap_shifts_carry_but_not_the_ranking(priced):
    """The measured quote-to-trade gap is one number for the whole market, so
    it moves every bond's carry by the same amount. That is the reason it is
    applied as a level shift and not as a per-bond adjustment."""
    db.upsert_bond(priced, "LKB01035F159", 10.0, "2035-06-15", 10, "2026-09-02")
    priced.execute(
        """INSERT INTO curve_residuals (obs_date, isin, source, tau_years,
               observed_yield, fitted_yield, residual_bp, weight)
           VALUES ('2026-09-02', 'LKB01035F159', 'quote', 8.8, 11.60, 11.60, 0.0, 1.0)""")
    priced.commit()

    plain = carry.profile(priced, "2026-09-02")
    shifted = carry.profile(priced, "2026-09-02", entry_gap_bp=10.0)
    for bond in plain:
        assert shifted[bond]["carry_bp"] == pytest.approx(plain[bond]["carry_bp"] + 10.0)
    order = lambda facts: sorted(facts, key=lambda b: facts[b]["per_duration_bp"])
    assert order(plain) == order(shifted)


# ---------------------------------------------------------------------------
# The quote-to-trade gap
# ---------------------------------------------------------------------------

def _pair(conn, obs_date, isin_, tau, quote_yield, trade_yield):
    for source, observed in (("quote", quote_yield), ("trade", trade_yield)):
        conn.execute(
            """INSERT INTO curve_residuals (obs_date, isin, source, tau_years,
                   observed_yield, fitted_yield, residual_bp, weight)
               VALUES (?, ?, ?, ?, ?, 0.0, 0.0, 1.0)""",
            (obs_date, isin_, source, tau, observed))


def test_gap_is_positive_when_trades_print_cheaper_than_the_screen(tmp_path):
    conn = db.connect(tmp_path / "c.sqlite")
    for day in range(1, 13):
        _pair(conn, f"2026-09-{day:02d}", BOND, 7.8, 11.40, 11.55)
    conn.commit()
    measured = execution.gap(conn, "2026-09-12")
    assert measured["gap_bp"] == pytest.approx(15.0)
    assert measured["n"] == 12
    assert "cheaper than" in execution.describe(measured)


def test_gap_uses_only_the_trailing_window(tmp_path):
    """The window ends on the day being scored, so a historical day is judged
    on what was knowable then — and old regimes drop out as it moves."""
    conn = db.connect(tmp_path / "c.sqlite")
    for day in range(1, 13):                       # old regime, far cheaper
        _pair(conn, f"2026-06-{day:02d}", BOND, 7.8, 11.40, 12.40)
    for day in range(1, 13):                       # current regime
        _pair(conn, f"2026-09-{day:02d}", BOND, 7.8, 11.40, 11.45)
    conn.commit()
    assert execution.gap(conn, "2026-09-12")["gap_bp"] == pytest.approx(5.0)
    assert execution.gap(conn, "2026-06-12")["gap_bp"] == pytest.approx(100.0)


def test_gap_is_none_when_too_little_traded(tmp_path):
    conn = db.connect(tmp_path / "c.sqlite")
    _pair(conn, "2026-09-01", BOND, 7.8, 11.40, 11.55)
    conn.commit()
    assert execution.gap(conn, "2026-09-02") is None
    assert "too few trades" in execution.describe(None)


def test_bill_isins_decode_and_round_trip():
    built = isin.build_bill(364, dt.date(2027, 8, 27))
    assert built == "LKA36427H279"
    assert isin.decode_bill(built) == (364, dt.date(2027, 8, 27))
    assert isin.decode_bill("LKB00934F154") is None      # a bond, not a bill
    assert isin.decode_bill("LKA36427H270") is None      # bad check digit
