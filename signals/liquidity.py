"""How tradeable each bond is, and where it sits in the auction cycle.

Ranking purely by z-score selects for the WRONG bonds. An illiquid bond is
quoted from stale marks that jump when they are finally refreshed, so its
residual moves in big steps and its z-score is large; a liquid benchmark is
quoted tightly and continuously, so its residual barely moves and it never
reaches the top of the list. Measured on this data the effect was stark:
the bonds heading the cheap list had traded on 1 to 5 of the previous 60
days, while the single most-traded bond in the market (Rs 80bn over 60
days) never appeared at all.

Bonds are therefore sorted into three tiers, and the reports lead with the
first:

* **core** — a current auction benchmark that is genuinely trading. This is
  the paper the PDMO is issuing now and the dealers make real prices in;
  it is where a decision can actually be executed in size.
* **active** — not on the run, but trading often enough that a dislocation
  can be acted on.
* **wider** — quoted, rarely traded. Still fitted into the curve, because
  the curve needs the whole cross-section to have a shape, but not
  something to act on.

Note the asymmetry: the wider market stays in the CURVE (dropping it would
leave too few points to define the shape) while being demoted in the
SIGNALS. The broad universe is the measuring stick; the core is what you
trade.

**The auction cycle.** Testing the 15 auctions in this data for the classic
pre-auction concession found the opposite pattern: bonds do not cheapen
going in (+0.5bp over the ten days before, which is nothing), they cheapen
AFTER and stay cheap — about +5.9bp versus their own norm over the following
fortnight, fading to +2.8bp by 15-30 days as the new supply is distributed.
So `post_auction` marks that window, and `days_since_auction` lets a reading
be judged against it. On 44 bond-auctions across 15 auction dates this is
suggestive, not established, and 6bp sits below a typical 16bp bid-offer —
it is context for a decision, not a trade on its own. `python -m
signals.validate` reprints the table as history grows.
"""

import datetime as dt

WINDOW_DAYS = 60           # trailing window for turnover and days traded
MIN_DAYS_TRADED = 10       # under this in the window, treat as untradeable
BENCHMARK_MIN_DAYS = 8     # a current benchmark clears a lower bar, not none
BENCHMARK_DAYS = 120       # how recently a bond must have been auctioned
POST_AUCTION_DAYS = 14     # the window in which auctioned paper sits cheap

EMPTY = {"turnover_lkr": 0, "n_trades": 0, "days_traded": 0,
         "is_benchmark": False, "last_auction": None, "days_since_auction": None,
         "post_auction": False, "bid_to_cover": None, "tier": "wider"}


def profile(conn, obs_date: str) -> dict:
    """{isin: liquidity and auction facts} as of `obs_date`.

    Everything is measured over the window ENDING on obs_date, so a
    historical date is scored on what was known then, not on today.
    """
    today = dt.date.fromisoformat(obs_date)
    start = (today - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    benchmark_start = (today - dt.timedelta(days=BENCHMARK_DAYS)).isoformat()

    out: dict[str, dict] = {}
    for row in conn.execute(
            """SELECT isin, SUM(volume_lkr) AS turnover, SUM(n_trades) AS trades,
                      COUNT(*) AS days_traded
                 FROM trade_summary
                WHERE obs_date > ? AND obs_date <= ? AND security_type = 'TBond'
                GROUP BY isin""", (start, obs_date)):
        out[row["isin"]] = dict(EMPTY, turnover_lkr=row["turnover"] or 0,
                                n_trades=row["trades"] or 0,
                                days_traded=row["days_traded"] or 0)

    # Benchmark status and where each bond sits in its auction cycle.
    for row in conn.execute(
            """SELECT isin, MAX(auction_date) AS last_auction FROM auctions
                WHERE auction_date > ? AND auction_date <= ?
                GROUP BY isin""", (benchmark_start, obs_date)):
        entry = out.setdefault(row["isin"], dict(EMPTY))
        since = (today - dt.date.fromisoformat(row["last_auction"])).days
        entry.update(is_benchmark=True, last_auction=row["last_auction"],
                     days_since_auction=since,
                     post_auction=0 <= since <= POST_AUCTION_DAYS)

    # Bid-to-cover from the most recent competitive auction of each bond:
    # how much demand the last supply met.
    for row in conn.execute(
            """SELECT isin, bids_lkr, offered_lkr FROM auctions
                WHERE kind = 'auction' AND bids_lkr IS NOT NULL
                  AND offered_lkr > 0 AND auction_date <= ?
                ORDER BY auction_date""", (obs_date,)):
        if row["isin"] in out:
            out[row["isin"]]["bid_to_cover"] = row["bids_lkr"] / row["offered_lkr"]

    for facts in out.values():
        facts["tier"] = _tier(facts)
    return out


def _tier(facts: dict) -> str:
    if facts["is_benchmark"] and facts["days_traded"] >= BENCHMARK_MIN_DAYS:
        return "core"
    if facts["days_traded"] >= MIN_DAYS_TRADED:
        return "active"
    return "wider"


def is_tradeable(facts: dict | None) -> bool:
    """Enough recent trading to act on a dislocation in this bond."""
    return bool(facts) and facts["tier"] in ("core", "active")


def is_core(facts: dict | None) -> bool:
    """A current benchmark that is genuinely trading."""
    return bool(facts) and facts["tier"] == "core"


def describe(facts: dict | None) -> str:
    """Short human label for a bond's liquidity, for tables and tooltips."""
    if not facts:
        return "no recent trades"
    parts = [f"Rs {facts['turnover_lkr'] / 1e9:.1f}bn over {facts['days_traded']}d"]
    if facts["is_benchmark"]:
        auctioned = f"auctioned {facts['last_auction']}"
        if facts["post_auction"]:
            auctioned += f", {facts['days_since_auction']}d ago — still in the cheap window"
        parts.append(auctioned)
    if facts["bid_to_cover"]:
        parts.append(f"last cover {facts['bid_to_cover']:.1f}x")
    return "; ".join(parts)
