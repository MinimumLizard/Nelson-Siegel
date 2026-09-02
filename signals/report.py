"""Show today's relative-value signals.

    python -m signals.report                    # latest day
    python -m signals.report --date 2026-08-28 --top 8

The report leads with the CORE book — current auction benchmarks that are
genuinely trading, which is the paper a decision can be executed in. Bonds
that trade but are not on the run follow; the rest of the market is fitted
into the curve (it needs the whole cross-section to have a shape) but is
not listed, because a dislocation you cannot deal on is not an opportunity.

That ordering exists because ranking on z-score alone selects for the
opposite: an illiquid bond is quoted from stale marks that jump when
refreshed, so its residual moves in steps and its z-score is large, while a
liquid benchmark is quoted continuously and barely moves.

Every line puts the opportunity next to what it costs to take. The gap is
how far a bond (or a pair) sits from its own recent norm; the cost is what
crossing the bid-offer would take out of it. Over this sample a |z| above 2
reverted about 6bp in ten days and above 3 about 12bp, while the median
bond's bid-offer is 16bp — so most signals do NOT clear their own costs as
a standalone trade, and the table says so rather than leaving it implied.
"""

import argparse

from pipeline import db
from signals import liquidity

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
    facts = liquidity.profile(conn, obs_date)
    tradeable = {isin for isin, spread in spreads.items()
                 if spread <= MAX_TRADEABLE_SPREAD_BP
                 and liquidity.is_tradeable(facts.get(isin))}
    all_rows = conn.execute(
        """SELECT s.*, b.series_label, b.maturity_date
             FROM bond_signals s JOIN bonds b USING(isin)
            WHERE s.obs_date = ? ORDER BY s.zscore DESC""", (obs_date,)).fetchall()
    rows = [row for row in all_rows if row["isin"] in tradeable]
    hidden = len(all_rows) - len(rows)
    if not rows:
        print("no signals for this date (a bond needs ~30 days of history first)")
        return

    def bond_table(title, subset, show_auction=False):
        """One tier's rows, cheapest first. Empty subsets print nothing."""
        if not subset:
            return
        print(f"\n{title}")
        header = (f"  {'series':<18}{'resid':>8}{'norm':>8}{'gap':>8}{'z':>7}"
                  f"{'b/o':>6}{'Rs bn':>8}{'days':>6}")
        print(header + (f"{'auction':>10}{'cover':>7}" if show_auction else ""))
        for row in subset:
            spread = spreads.get(row["isin"])
            fact = facts.get(row["isin"], {})
            line = (f"  {(row['series_label'] or row['isin']):<18}"
                    f"{row['residual_bp']:>+8.1f}{row['mean_bp']:>+8.1f}"
                    f"{row['dislocation_bp']:>+8.1f}{row['zscore']:>7.1f}"
                    f"{(f'{spread:.0f}' if spread else '-'):>6}"
                    f"{fact.get('turnover_lkr', 0) / 1e9:>8.1f}"
                    f"{fact.get('days_traded', 0):>6}")
            if show_auction:
                since = fact.get("days_since_auction")
                marker = (f"{since}d" + ("*" if fact.get("post_auction") else "")
                          if since is not None else "-")
                cover = f"{fact['bid_to_cover']:.1f}x" if fact.get("bid_to_cover") else "-"
                line += f"{marker:>10}{cover:>7}"
            print(line)

    def both_ends(subset, count):
        """The cheap and the rich end of a tier, never the same bond twice.

        Split on the SIGN of the z-score rather than by slicing `count` rows
        off each end. A z-score is the gap divided by a positive standard
        deviation, so its sign is the direction: every bond lands under the
        heading that is true of it, and a tier with fewer than 2*count bonds
        can no longer appear in both lists, as end-slicing made it.
        """
        cheap = [row for row in subset if row["zscore"] >= 0]
        rich = [row for row in subset if row["zscore"] < 0]
        bond_table("cheap to their own norm", cheap[:count])
        bond_table("rich to their own norm", list(reversed(rich[-count:])))

    core = [row for row in rows if liquidity.is_core(facts.get(row["isin"]))]
    other = [row for row in rows if not liquidity.is_core(facts.get(row["isin"]))]

    # The core book is printed in FULL, however long, rather than trimmed to
    # the cheap and rich ends. It is only ever a handful of bonds, it is the
    # book a decision is actually executed in, and where a benchmark sits in
    # the middle of the pack is itself information.
    print(f"\n=== CORE BOOK — {len(core)} auction benchmark(s), actually trading ===")
    if core:
        bond_table("cheapest at the top, richest at the bottom", core, show_auction=True)
    else:
        print("  none today: no current benchmark cleared the trading floor of "
              f"{liquidity.BENCHMARK_MIN_DAYS} days in the last {liquidity.WINDOW_DAYS}")
    if any(facts.get(row["isin"], {}).get("post_auction") for row in core):
        print("\n  * still inside the post-auction window: over the 15 auctions in "
              "this data\n    bonds sat about 6bp cheap to their own norm for a "
              "fortnight afterwards.")
    _building(conn, obs_date, facts)

    if other:
        print(f"\n=== ALSO TRADING — {len(other)} liquid, but not on the run ===")
        both_ends(other, max(top - 2, 3))

    # Switches are restricted to two core legs. A pair is only a trade if you
    # can deal in BOTH sides, and the core book is where that is true.
    switches = [row for row in conn.execute(
        """SELECT s.*, ba.series_label AS label_a, bb.series_label AS label_b
             FROM switch_signals s
             JOIN bonds ba ON ba.isin = s.isin_a
             JOIN bonds bb ON bb.isin = s.isin_b
            WHERE s.obs_date = ? ORDER BY ABS(s.zscore) DESC""", (obs_date,))
        if {row["isin_a"], row["isin_b"]} <= {r["isin"] for r in core}][:top]
    if switches:
        print("\nSWITCH CANDIDATES — both legs in the core book; buy the cheap, "
              "sell the rich")
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
          "10-day reversion at this z\nRs bn / days = turnover and days traded in "
          f"the last {liquidity.WINDOW_DAYS} | auction = days since it was last sold"
          f"\n* = inside the {liquidity.POST_AUCTION_DAYS}-day post-auction window")
    if hidden:
        print(f"{hidden} scored bond(s) not shown: quoted wider than "
              f"{MAX_TRADEABLE_SPREAD_BP:.0f}bp, or traded on fewer than "
              f"{liquidity.MIN_DAYS_TRADED} of the last {liquidity.WINDOW_DAYS} days.\n"
              "They stay IN the curve — it needs the whole cross-section to have a\n"
              "shape — but a dislocation you cannot deal on is not an opportunity.")


def _building(conn, obs_date, facts) -> None:
    """Tradeable bonds that have no z-score yet, listed rather than hidden.

    A freshly auctioned benchmark is the most tradeable paper on the screen
    and the least likely to have 30 days of residual history, so silently
    dropping it hides exactly what matters most. Naming it, with the history
    it has so far, is more useful than an empty row.
    """
    scored = {row["isin"] for row in conn.execute(
        "SELECT isin FROM bond_signals WHERE obs_date = ?", (obs_date,))}
    quoted = {row["isin"] for row in conn.execute(
        """SELECT isin FROM observations WHERE obs_date = ? AND source = 'pdmo_daily'
             AND bid_yield IS NOT NULL""", (obs_date,))}
    waiting = [(isin, fact) for isin, fact in facts.items()
               if isin in quoted and isin not in scored and liquidity.is_tradeable(fact)]
    if not waiting:
        return
    labels = {row["isin"]: row["series_label"]
              for row in conn.execute("SELECT isin, series_label FROM bonds")}
    print("\nNOT SCORED YET — tradeable, but short of the 30 days a z-score needs")
    for isin, fact in sorted(waiting, key=lambda kv: -kv[1]["turnover_lkr"]):
        days = conn.execute(
            """SELECT COUNT(*) c FROM curve_residuals
                WHERE isin = ? AND source = 'quote' AND obs_date <= ?""",
            (isin, obs_date)).fetchone()["c"]
        tag = " (benchmark)" if fact["is_benchmark"] else ""
        print(f"  {(labels.get(isin) or isin) + tag:<26}"
              f"{liquidity.describe(fact)}; {days} quote days so far")


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
