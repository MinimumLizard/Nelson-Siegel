"""Show today's relative-value signals.

    python -m signals.report                    # latest day
    python -m signals.report --date 2026-08-28 --top 8

Every line puts the opportunity next to what it costs to take. The gap is
how far a bond (or a pair) sits from its own recent norm; the cost is what
crossing the bid-offer would take out of it. Over this sample a |z| above 2
reverted about 6bp in ten days and above 3 about 12bp, while the median
bond's bid-offer is 16bp — so most signals do NOT clear their own costs as
a standalone trade, and the table says so rather than leaving it implied.
"""

import argparse

from pipeline import db

# Empirical, measured on this sample by `python -m signals.validate`:
# mean absolute reversion of a pair spread over the following 10 days.
EXPECTED_REVERSION_BP = {2.0: 5.8, 3.0: 11.7}

# A quote wider than this is not a price you can deal on, so a "signal" in
# it is not an opportunity. The widest quotes in this data run to several
# hundred basis points and would otherwise top the cheap list every day.
MAX_TRADEABLE_SPREAD_BP = 50.0


def _expected_capture(zscore: float) -> float:
    magnitude = abs(zscore)
    if magnitude >= 3.0:
        return EXPECTED_REVERSION_BP[3.0]
    if magnitude >= 2.0:
        return EXPECTED_REVERSION_BP[2.0]
    return 0.0


def latest_date(conn) -> str | None:
    row = conn.execute("SELECT MAX(obs_date) AS d FROM bond_signals").fetchone()
    return row["d"] if row else None


def spreads_on(conn, obs_date: str) -> dict:
    """Each bond's bid-offer that day, in bp — the cost of touching it."""
    return {row["isin"]: (row["bid_yield"] - row["offer_yield"]) * 100.0
            for row in conn.execute(
                """SELECT isin, bid_yield, offer_yield FROM observations
                    WHERE obs_date = ? AND source = 'pdmo_daily'
                      AND bid_yield IS NOT NULL AND offer_yield IS NOT NULL""",
                (obs_date,))}


def show(conn, obs_date: str, top: int) -> None:
    fit = conn.execute("SELECT * FROM curve_fits WHERE obs_date = ?", (obs_date,)).fetchone()
    print(f"\nLKR government bond relative value — {obs_date}")
    if fit:
        line = (f"curve: {fit['n_quotes']} bonds, fit RMSE {fit['rmse_bp']:.1f}bp, "
                f"lambda {fit['lambda_years']:.2f}y")
        if fit["n_trades"]:
            line += (f"; vs {fit['n_trades']} trades bias {fit['trade_bias_bp']:+.1f}bp")
        print(line)

    spreads = spreads_on(conn, obs_date)
    tradeable = {isin for isin, spread in spreads.items()
                 if spread <= MAX_TRADEABLE_SPREAD_BP}
    all_rows = conn.execute(
        """SELECT s.*, b.series_label, b.maturity_date
             FROM bond_signals s JOIN bonds b USING(isin)
            WHERE s.obs_date = ? ORDER BY s.zscore DESC""", (obs_date,)).fetchall()
    rows = [row for row in all_rows if row["isin"] in tradeable]
    excluded = len(all_rows) - len(rows)
    if excluded:
        print(f"({excluded} bond(s) hidden: bid-offer wider than "
              f"{MAX_TRADEABLE_SPREAD_BP:.0f}bp, not a dealable price)")
    if not rows:
        print("no signals for this date (a bond needs ~30 days of history first)")
        return

    def bond_table(title, subset):
        print(f"\n{title}")
        print(f"  {'series':<22}{'resid':>8}{'norm':>8}{'gap':>8}{'z':>7}{'b/o':>7}")
        for row in subset:
            spread = spreads.get(row["isin"])
            print(f"  {row['series_label'] or row['isin']:<22}"
                  f"{row['residual_bp']:>+8.1f}{row['mean_bp']:>+8.1f}"
                  f"{row['dislocation_bp']:>+8.1f}{row['zscore']:>7.1f}"
                  f"{(f'{spread:.0f}' if spread else '-'):>7}")

    bond_table("CHEAP — yields more than its own recent norm", rows[:top])
    bond_table("RICH — yields less than its own recent norm", list(reversed(rows[-top:])))

    switches = [row for row in conn.execute(
        """SELECT s.*, ba.series_label AS label_a, bb.series_label AS label_b
             FROM switch_signals s
             JOIN bonds ba ON ba.isin = s.isin_a
             JOIN bonds bb ON bb.isin = s.isin_b
            WHERE s.obs_date = ? ORDER BY ABS(s.zscore) DESC""", (obs_date,))
        if row["isin_a"] in tradeable and row["isin_b"] in tradeable][:top]
    if switches:
        print("\nSWITCH CANDIDATES — buy the cheap leg, sell the rich leg")
        print(f"  {'buy':<20}{'sell':<20}{'gap':>8}{'z':>7}{'cost':>7}{'exp':>7}  verdict")
        for row in switches:
            # Positive z: A is cheap versus B. Negative: the other way round.
            if row["zscore"] >= 0:
                buy, sell = (row["label_a"] or row["isin_a"]), (row["label_b"] or row["isin_b"])
                buy_isin, sell_isin = row["isin_a"], row["isin_b"]
            else:
                buy, sell = (row["label_b"] or row["isin_b"]), (row["label_a"] or row["isin_a"])
                buy_isin, sell_isin = row["isin_b"], row["isin_a"]
            # Crossing the spread on both legs costs roughly half of each.
            cost = sum(spreads.get(i, 0.0) for i in (buy_isin, sell_isin)) / 2.0
            capture = _expected_capture(row["zscore"])
            verdict = ("clears costs" if capture > cost
                       else "below costs" if capture else "weak signal")
            print(f"  {buy:<20}{sell:<20}{abs(row['dislocation_bp']):>8.1f}"
                  f"{row['zscore']:>7.1f}{cost:>7.0f}{capture:>7.1f}  {verdict}")

    print("\ngap = distance from its own recent norm, bp | z = in its own standard "
          "deviations\ncost = half the bid-offer on each leg | exp = historical "
          "10-day reversion at this z")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="which day to show (default: the latest)")
    parser.add_argument("--top", type=int, default=6, help="rows per section")
    args = parser.parse_args()

    conn = db.connect()
    obs_date = args.date or latest_date(conn)
    if not obs_date:
        raise SystemExit("no signals stored — run: python -m signals.run")
    show(conn, obs_date, args.top)


if __name__ == "__main__":
    main()
