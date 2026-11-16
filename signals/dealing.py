"""Where a bond is likely to actually deal tomorrow.

    python -m signals.validate      # reruns the test described below

The quote on the screen is not a dealing level. It is the midpoint of the
dealers' AVERAGE buying and selling yields, and measured against it the
trades are somewhere else: 61% of them print at a yield above the dealers'
own bid yield — outside the quoted spread altogether, on the cheap side.
Taking the screen as the forecast of where a bond trades the next day is
wrong by 26bp on average.

The model already corrected for that with one number for the whole market:
the median trade-less-quote gap of the last 20 days (`signals/execution.py`).
That window was chosen by test, but only against LONGER ones. Shorter turns
out to be much better, because the gap is a regime that moves in days, not
weeks: +25bp on 8 October, -25bp on 5 November.

This module forecasts each bond's dealing yield as its quote plus an
adjustment, and the adjustment is a blend of two things:

* **the market's gap over the last 2 trading days** that have trades; and
* **the bond's own last gap** — where IT last traded against its own quote
  that day — which counts fully if that was the latest trading day and
  half as much for every `HALF_LIFE_DAYS` (2) trading days of age.

So a bond that traded yesterday is expected to deal where it dealt
yesterday relative to its screen, and a bond that has not traded for a
fortnight is expected to deal where the market is dealing.

**How it was tested.** Walk-forward over every bond-trade from the 60th
day of history: forecast day T's executed yield using only quotes to T-1
and trades to T-1, then compare. Mean absolute error:

    the screen (quote mid)                 26.0bp
    quote + 20-day market gap (the old)    21.0bp
    quote + 2-day market gap               14.8bp
    this blend                             12.4bp

Half of all trades landed within 8bp of the forecast, against 19bp for the
screen, and it won in nine of the ten months tested (April went to the old
rule, 7.8bp against 8.2bp). When the trade file is a day behind (trades
known only to T-2) it scores 16.0bp against 21.6bp.

To check the rule was not fitted to its own test: on the second half of
the history alone it scores 10.1bp against 17.6bp for the 20-day rule, and
a rule chosen afresh using only the first half scored 9.8bp there. So the
particular settings barely matter and the improvement is not an accident
of the sample. A fitted regression on the same inputs did WORSE out of
sample (15.7bp) than this fixed rule.

`backtest` reruns all of that on the current database, and `signals.run`
stores its error bands so the page can print them beside each forecast.

**What it is not.** It is where the day's trades are expected to average,
not a price a dealer is obliged to show, and not a view on where yields go
next. A fifth of trades still land more than 18bp away.
"""

import numpy as np
import pandas as pd

from pipeline import db

MARKET_DAYS = 2            # trading days of trades behind the market gap
WIDER_WINDOWS = (5, 20)    # tried in turn when those days hold too few trades
MIN_TRADES = 5             # fewer than this is not a market reading
HALF_LIFE_DAYS = 2.0       # how fast a bond's own last gap stops counting
# A bond's own gap may sit this far from the market's and no further. One
# ticket booked at an agreed rather than a market level (2026-09-24's
# 10.70%2035A printed 68bp through its quote) should not become tomorrow's
# forecast for that bond.
MAX_OWN_OFFSET_BP = 40.0
BACKTEST_START = 60        # days of history before the first scored forecast


def load_gaps(conn) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(quote mids, traded yields, gaps in bp): one row per quote day, one
    column per bond. A gap exists only where a bond quoted AND traded."""
    frame = pd.read_sql_query(
        """SELECT obs_date, isin, source, observed_yield
             FROM curve_residuals""", conn)
    days = sorted(frame.loc[frame["source"] == "quote", "obs_date"].unique())
    wide = {source: frame[frame["source"] == source].pivot(
                index="obs_date", columns="isin", values="observed_yield")
            for source in ("quote", "trade")}
    mids = wide["quote"].reindex(days)
    traded = wide["trade"].reindex(index=days, columns=mids.columns)
    return mids, traded, (traded - mids) * 100.0


def _market_gap(gaps: pd.DataFrame, last: int) -> tuple[float | None, int]:
    """Median gap over the trading days ending at row `last`, and the
    window it took to find `MIN_TRADES` of them."""
    for window in (MARKET_DAYS, *WIDER_WINDOWS):
        values = gaps.iloc[max(last - window + 1, 0):last + 1].to_numpy()
        values = values[~np.isnan(values)]
        if len(values) >= MIN_TRADES:
            return float(np.median(values)), window
    return None, 0


def adjustments(gaps: pd.DataFrame, last: int) -> pd.DataFrame | None:
    """Per-bond adjustment to the quote, bp, knowing trades to row `last`.

    The one place the rule lives: `forecast` and `backtest` both call it,
    so the page can never show a forecast the test did not score.
    """
    market, window = _market_gap(gaps, last)
    if market is None:
        return None
    known = gaps.iloc[:last + 1].to_numpy()
    # Each bond's most recent gap, and how many trading days old it is:
    # the last row of its column that holds a number.
    present = ~np.isnan(known)
    row = len(known) - 1 - present[::-1].argmax(axis=0)
    ever = present.any(axis=0)
    own = pd.Series(np.where(ever, known[row, np.arange(known.shape[1])], np.nan),
                    index=gaps.columns)
    age = pd.Series(np.where(ever, last - row, np.nan), index=gaps.columns)
    own = market + (own - market).clip(-MAX_OWN_OFFSET_BP, MAX_OWN_OFFSET_BP)
    weight = (0.5 ** (age / HALF_LIFE_DAYS)).fillna(0.0)
    blended = weight * own.fillna(0.0) + (1.0 - weight) * market
    return pd.DataFrame({"adjustment_bp": blended, "own_gap_bp": own,
                         "own_age_days": age, "own_weight": weight,
                         "market_gap_bp": market, "market_window": window})


def forecast(conn, obs_date: str) -> dict | None:
    """Expected dealing yields for the day after `obs_date`.

    Uses quotes on `obs_date` and whatever trades exist on or before it, so
    a historical day is forecast on what was knowable then. Returns None
    before there are enough trades to read a market gap at all.
    """
    mids, traded, gaps = load_gaps(conn)
    mids, gaps = mids.loc[:obs_date], gaps.loc[:obs_date]
    if obs_date not in mids.index:
        return None
    with_trades = np.flatnonzero(gaps.notna().any(axis=1).to_numpy())
    if not len(with_trades):
        return None
    last = int(with_trades[-1])
    table = adjustments(gaps, last)
    if table is None:
        return None

    # The stored error bands depend on whether the trade file for this very
    # day is in. A day behind, everything it knows is a day staler.
    trades_through = gaps.index[last]
    fresh = trades_through == obs_date
    stats = db.signal_stats(conn)
    suffix = "fresh" if fresh else "lagged"
    band = {name: stats[f"dealing_{name}_{suffix}"]["value"]
            for name in ("band50_bp", "band80_bp", "screen_band50_bp")
            if f"dealing_{name}_{suffix}" in stats}

    bonds = {}
    for isin, mid in mids.loc[obs_date].dropna().items():
        row = table.loc[isin]
        bonds[isin] = {
            "quote_yield": float(mid),
            "expected_yield": float(mid + row["adjustment_bp"] / 100.0),
            "adjustment_bp": float(row["adjustment_bp"]),
            "own_gap_bp": None if np.isnan(row["own_gap_bp"]) else float(row["own_gap_bp"]),
            "own_age_days": None if np.isnan(row["own_age_days"]) else int(row["own_age_days"]),
        }
    first = table.iloc[0]
    return {"bonds": bonds,
            "market_gap_bp": float(first["market_gap_bp"]),
            "market_window": int(first["market_window"]),
            "trades_through": trades_through, "fresh": fresh,
            "band50_bp": band.get("band50_bp"), "band80_bp": band.get("band80_bp"),
            "screen_band50_bp": band.get("screen_band50_bp")}


def _old_rule(gaps: pd.DataFrame, last: int) -> float:
    """The 20-day market median this replaces, for the comparison only."""
    values = gaps.iloc[max(last - 19, 0):last + 1].to_numpy()
    values = values[~np.isnan(values)]
    return float(np.median(values)) if len(values) else 0.0


def backtest(conn) -> pd.DataFrame:
    """Every forecast the rule would have made, beside what happened.

    One row per bond-trade per information lag. `lag` 1 means the trade
    file for the quote day was in when the forecast was made; 2 means it
    was a day behind. Errors are actual less forecast, in bp of yield.
    """
    mids, traded, gaps = load_gaps(conn)
    rows = []
    for target in range(BACKTEST_START, len(mids)):
        actual = traded.iloc[target]
        quote = mids.iloc[target - 1]
        scored = actual.notna() & quote.notna()
        if not scored.any():
            continue
        move = (actual - quote)[scored] * 100.0      # what the screen missed
        for lag in (1, 2):
            table = adjustments(gaps, target - lag)
            if table is None:
                continue
            market_only = table["market_gap_bp"].iloc[0]
            old = _old_rule(gaps, target - lag)
            for isin, missed in move.items():
                rows.append({
                    "obs_date": mids.index[target], "isin": isin, "lag": lag,
                    "screen_error": missed,
                    "old_error": missed - old,
                    "market_error": missed - market_only,
                    "error": missed - table.at[isin, "adjustment_bp"],
                    "own_age_days": table.at[isin, "own_age_days"]})
    return pd.DataFrame(rows)


def summarise(scored: pd.DataFrame) -> dict:
    """{lag: accuracy figures} from a `backtest` frame."""
    out = {}
    for lag, group in scored.groupby("lag"):
        size = group["error"].abs()
        out[int(lag)] = {
            "n": len(group),
            "mae_bp": float(size.mean()),
            "band50_bp": float(size.quantile(0.5)),
            "band80_bp": float(size.quantile(0.8)),
            "bias_bp": float(group["error"].mean()),
            "screen_mae_bp": float(group["screen_error"].abs().mean()),
            "screen_band50_bp": float(group["screen_error"].abs().quantile(0.5)),
            "old_mae_bp": float(group["old_error"].abs().mean()),
            "market_mae_bp": float(group["market_error"].abs().mean())}
    return out


def measure(conn) -> dict:
    """Run the backtest and store the error bands the page quotes.

    Called on every signals rebuild, for the same reason the reversion
    figures are: a number pasted into the page is a number that goes stale.
    """
    scored = backtest(conn)
    if scored.empty:
        return {}
    summary = summarise(scored)
    for lag, name in ((1, "fresh"), (2, "lagged")):
        if lag not in summary or summary[lag]["n"] < 100:   # too thin to quote
            continue
        for key in ("mae_bp", "band50_bp", "band80_bp", "screen_mae_bp",
                    "screen_band50_bp", "old_mae_bp"):
            db.store_signal_stat(conn, f"dealing_{key}_{name}",
                                 summary[lag][key], summary[lag]["n"])
    return summary
