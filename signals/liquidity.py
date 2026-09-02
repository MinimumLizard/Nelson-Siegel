"""How tradeable each bond actually is — and why the signals need to know.

Ranking purely by z-score selects for the WRONG bonds. An illiquid bond is
quoted from stale marks that jump when they are finally refreshed, so its
residual moves in big steps and its z-score is large; a liquid benchmark is
quoted tightly and continuously, so its residual barely moves and it never
reaches the top of the list. Measured on this data the effect is stark: the
bonds heading the cheap list had traded on 1 to 5 of the previous 60 days,
while the single most-traded bond in the market (Rs 80bn over 60 days) never
appeared at all.

So a dislocation is only reported when the bond has actually been trading,
and every row carries its turnover so the size of the opportunity can be
judged against how much of it is real.

"Benchmark" here means a bond named in a recent auction announcement — the
paper the PDMO is currently issuing, which is where the depth is.
"""

import datetime as dt

WINDOW_DAYS = 60          # trailing window for turnover and days traded
MIN_DAYS_TRADED = 10      # under this in the window, treat as untradeable
BENCHMARK_MIN_DAYS = 3    # a current benchmark clears a lower bar, not none
BENCHMARK_DAYS = 120      # how recently a bond must have been auctioned


def profile(conn, obs_date: str) -> dict:
    """{isin: liquidity facts} as of `obs_date`.

    Everything is measured over the window ENDING on obs_date, so a
    historical date is scored on what was known then, not on today.
    """
    start = (dt.date.fromisoformat(obs_date) - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    benchmark_start = (dt.date.fromisoformat(obs_date)
                       - dt.timedelta(days=BENCHMARK_DAYS)).isoformat()

    out: dict[str, dict] = {}
    for row in conn.execute(
            """SELECT isin, SUM(volume_lkr) AS turnover, SUM(n_trades) AS trades,
                      COUNT(*) AS days_traded
                 FROM trade_summary
                WHERE obs_date > ? AND obs_date <= ? AND security_type = 'TBond'
                GROUP BY isin""", (start, obs_date)):
        out[row["isin"]] = {
            "turnover_lkr": row["turnover"] or 0,
            "n_trades": row["trades"] or 0,
            "days_traded": row["days_traded"] or 0,
            "is_benchmark": False,
            "last_auction": None,
        }

    for row in conn.execute(
            """SELECT isin, MAX(auction_date) AS last_auction FROM auctions
                WHERE auction_date > ? AND auction_date <= ?
                GROUP BY isin""", (benchmark_start, obs_date)):
        entry = out.setdefault(row["isin"], {
            "turnover_lkr": 0, "n_trades": 0, "days_traded": 0,
            "is_benchmark": False, "last_auction": None})
        entry["is_benchmark"] = True
        entry["last_auction"] = row["last_auction"]

    return out


def is_tradeable(facts: dict | None) -> bool:
    """Enough recent trading to act on a dislocation in this bond.

    A current benchmark clears a LOWER bar rather than being waived through:
    freshly auctioned paper is dealable before its printed trade record
    catches up, so excluding it would hide what matters most — but a
    benchmark that has not traded at all in the window is not tradeable
    either, whatever its status, so it still has to show some activity.
    """
    if not facts:
        return False
    floor = BENCHMARK_MIN_DAYS if facts["is_benchmark"] else MIN_DAYS_TRADED
    return facts["days_traded"] >= floor


def describe(facts: dict | None) -> str:
    """Short human label for a bond's liquidity, for tables and tooltips."""
    if not facts:
        return "no recent trades"
    parts = [f"Rs {facts['turnover_lkr'] / 1e9:.1f}bn over {facts['days_traded']}d"]
    if facts["is_benchmark"]:
        parts.append(f"benchmark, last auctioned {facts['last_auction']}")
    return "; ".join(parts)
