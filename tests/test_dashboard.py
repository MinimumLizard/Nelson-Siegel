"""Dashboard tests: the page must build from a database and say true things."""

import datetime as dt

import pandas as pd
import pytest

from dashboard import build
from pipeline import db
from signals import run as signal_run


@pytest.fixture
def seeded(tmp_path):
    """A small but complete database: bonds, quotes, trades, auctions, signals.

    The trades matter: the dashboard only lists bonds that actually trade,
    so a fixture without them renders (correctly) empty. So do the auctions:
    the core book is "benchmark AND trading", and switch candidates need two
    core legs, so a fixture with no auctions has an empty core book.
    """
    conn = db.connect(tmp_path / "dash.sqlite")
    bonds = [("LKB00527I150", 2.0, "2027-09-15", "10.00%2027A"),
             ("LKB00428B156", 3.0, "2028-02-15", "11.00%2028A"),
             ("LKB00934F154", 8.0, "2034-06-15", "10.75%2034A"),
             ("LKB01136H151", 10.0, "2036-08-15", "10.85%2036A")]
    for index in range(45):
        day = (pd.Timestamp("2026-01-01") + pd.tseries.offsets.BDay(index)).date().isoformat()
        for position, (isin, tau, maturity, label) in enumerate(bonds):
            db.upsert_bond(conn, isin, 10.0, maturity, 5, day, series_label=label)
            db.upsert_quote(conn, day, isin, bid_yield=10.0 + position + 0.08,
                            offer_yield=10.0 + position - 0.08,
                            bid_price=100.0, offer_price=100.0, raw_ref="t")
            conn.execute(
                """INSERT INTO curve_residuals (obs_date, isin, source, tau_years,
                       observed_yield, fitted_yield, residual_bp, weight)
                   VALUES (?, ?, 'quote', ?, 10.0, 10.0, ?, 1.0)""",
                # Each bond wanders differently, so pair spreads have real
                # variance — identical noise would give every pair sd 0.
                (day, isin, tau,
                 (position * 3.0) + ((index * (position + 2)) % 7) * 1.5))
            # Real trading, so the liquidity filter lets these bonds through.
            db.upsert_trade_summary(conn, day, isin, "TBond", None, None, None,
                                    None, 10.0 + position, 2_000_000_000, 4, "t")
        conn.execute(
            """INSERT INTO curve_fits (obs_date, beta0, beta1, beta2, lambda_years,
                   n_quotes, rmse_bp, n_trades, trade_rmse_bp, trade_bias_bp, fitted_at)
               VALUES (?, 12.0, -3.0, 1.0, 2.822, 4, 9.5, ?, 20.0, ?, '2026-01-01')""",
            (day, 2 if index % 2 else 0, 15.0 if index % 2 else None))
    # Two of the four are current auction benchmarks, which is what puts them
    # in the core book. They sit 7 years apart, so the pair only becomes a
    # switch candidate under the rule that any two auctioned bonds pair.
    for isin in ("LKB00428B156", "LKB01136H151"):
        db.upsert_auction(conn, "2026-01-20", isin, "auction", "2026-01-22",
                          11.0, 1_000_000_000, 2_500_000_000, 1_000_000_000, "t")
    conn.commit()
    signal_run.rebuild(conn)
    return conn


def test_gather_picks_the_latest_day(seeded):
    data = build.gather(seeded)
    assert data is not None
    assert data["obs_date"] == max(
        row["obs_date"] for row in seeded.execute("SELECT obs_date FROM curve_fits"))
    assert data["fit"]["n_quotes"] == 4
    assert data["coverage"]["days"] == 45


def test_gather_returns_none_without_curves(tmp_path):
    assert build.gather(db.connect(tmp_path / "empty.sqlite")) is None


def test_trade_bias_falls_back_to_the_last_day_that_had_trades(seeded):
    """The trade summary lags the daily report, so the newest curve often has
    no trades; the page must show the last real reading, not a blank."""
    data = build.gather(seeded)
    page = build.render(data)
    if data["fit"]["trade_bias_bp"] is None:
        assert data["last_checked"] is not None
        assert data["last_checked"]["obs_date"] in page
    assert "vs executed trades" in page


def test_page_is_self_contained_and_themed(seeded):
    page = build.render(build.gather(seeded))
    assert page.startswith("<!doctype html>")
    # No external requests: everything inline.
    assert "http://" not in page and "https://" not in page
    assert "prefers-color-scheme: dark" in page
    assert '<svg viewBox=' in page


def test_page_reports_the_cost_caveat(seeded):
    """The honest framing is part of the deliverable, not decoration."""
    page = build.render(build.gather(seeded))
    # Every switch row carries one of the three verdicts...
    assert any(verdict in page
               for verdict in ("clears costs", "below costs", "weak signal"))
    # ...and the standing caveats are always on the page, not just when a
    # given day happens to produce a marginal candidate. Compared on
    # whitespace-collapsed text, since the source wraps these sentences.
    flat = " ".join(page.split())
    assert "do not clear their own costs" in flat
    assert "trailing 60-day window" in flat
    assert "not a forecast of what it pays" in flat


def test_untradeable_bonds_are_hidden(seeded):
    """A quote nobody can deal on is not an opportunity."""
    day = build.gather(seeded)["obs_date"]
    seeded.execute(
        """UPDATE observations SET bid_yield = 20.0, offer_yield = 10.0
            WHERE obs_date = ? AND isin = 'LKB00527I150'""", (day,))
    seeded.commit()
    data = build.gather(seeded)
    assert data["hidden"] >= 1
    assert all(row["isin"] != "LKB00527I150" for row in data["signals"])


def test_rows_survive_a_missing_spread(seeded):
    """A bond with no two-way quote must still render a COMPLETE row.

    An earlier version put the whole row inside a conditional, so a missing
    bid-offer silently dropped the other four cells.
    """
    data = build.gather(seeded)
    rows = build._signal_rows(data["signals"], {}, cheap=True)  # no spreads at all
    assert rows.count("<tr>") == rows.count("</tr>") >= 1
    assert "–" in rows                       # placeholder for the unknown b/o
    assert rows.count("<td>") >= 4 * rows.count("<tr>")  # every cell present


def test_core_book_leads_with_the_benchmarks_that_trade(seeded):
    """The page's whole point: what you can actually deal in, first."""
    data = build.gather(seeded)
    assert {row["isin"] for row in data["core"]} == {"LKB00428B156", "LKB01136H151"}
    assert all(row["isin"] not in data["core_isins"] for row in data["others"])
    page = build.render(data)
    assert "Core book" in page
    # The auction facts a decision needs are on the row, not just implied.
    assert "cover" in page and "auction" in page
    assert "2.5×" in page                      # bid-to-cover of the last auction


def test_switch_candidates_need_two_core_legs(seeded):
    """A pair is only a trade if you can deal in BOTH sides."""
    data = build.gather(seeded)
    assert data["switches"]                    # the fixture has a core pair
    for row in data["switches"]:
        assert {row["isin_a"], row["isin_b"]} <= data["core_isins"]


def test_a_bond_is_never_both_cheap_and_rich(seeded):
    """Slicing N rows off each end of a shorter list overlapped in the middle
    and printed the same bond under both headings."""
    data = build.gather(seeded)
    signals = data["signals"]
    assert len(signals) < 2 * 6                # short enough to have overlapped
    cheap = build._signal_rows(signals, data["spreads"], cheap=True)
    rich = build._signal_rows(signals, data["spreads"], cheap=False)
    for row in signals:
        label = row["series_label"]
        assert not (label in cheap and label in rich), label
    # And every row is on the side its heading claims.
    for row in signals:
        side = cheap if row["zscore"] >= 0 else rich
        assert row["series_label"] in side
