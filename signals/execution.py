"""Where trades actually print, relative to the dealers' quote screen.

The curve is fitted to quote MIDS, but nobody deals at a quote mid. On days
when a bond both quoted and traded, the executed weighted-average yield can
be compared with the quoted mid, and the difference is the gap. Positive
means trades print at a HIGHER yield — a lower price — than the screen.

`curve_fits.trade_bias_bp` records that per day, and the README quoted its
whole-sample average of about +16bp. Both of those hide the thing that
actually matters, which is that the gap MOVES, and by far more than it
differs across the curve:

    monthly median      2026-02   +0.8bp     2026-06  +51.9bp
                        2026-03  +15.2bp     2026-08   -3.6bp
                        2026-05  +20.6bp     2026-09   -6.6bp

Early June ran +100 to +134bp market-wide, across 25 of the 31 bonds that
traded, decaying over a fortnight — dealer screens lagging a fast move,
with trades printing far cheaper than the quotes. A constant on the page
cannot describe that. Right now the gap is NEGATIVE, so the whole-sample
+16bp is not merely imprecise, it has the wrong sign for this regime.

**Why not bucket it by tenor?** It was tried, because the whole-sample
medians look convincing (+17.6bp at 0-2y against +8.3bp at 7-10y, on 2,077
bond-days). It does not survive an out-of-sample test: predicting the next
quarter's gap for a bucket from that bucket's own past gives a mean absolute
error of 10.9bp, against 9.2bp for simply using the blended past. Slicing
into buckets adds more estimation noise than the tenor structure removes,
because the market-wide level swings by 50bp while the tenor spread is worth
about 9bp. The front-end-is-wider ordering also held in three quarters and
reversed in the fourth. So the estimate stays blended across tenors, and the
window is what got shortened.

Window length was chosen the same way — by out-of-sample error on predicting
each day's realised gap:

    expanding (all history)   18.1bp        60 days   16.5bp
    120 days                  18.0bp        20 days   13.0bp

Twenty days it is. That is still a 13bp standard error on a number that has
ranged over 140bp, so it is an estimate of a regime, not a precise level.
"""

WINDOW_DAYS = 20           # trailing window; chosen by the test above
MIN_OBSERVATIONS = 8       # under this the estimate is not reported at all

# Kept only for the diagnostic in `signals.validate`. NOT used to adjust any
# bond's level — see the module docstring for why tenor buckets lost.
BUCKETS = [(0.0, 2.0, "0-2y"), (2.0, 4.0, "2-4y"), (4.0, 7.0, "4-7y"),
           (7.0, 10.0, "7-10y"), (10.0, 99.0, "10y+")]


def _median(values) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _pairs(conn, obs_date: str, window: int) -> list:
    """(tau, gap_bp) for every bond-day in the window that both quoted and traded."""
    return conn.execute(
        """SELECT q.tau_years AS tau,
                  (t.observed_yield - q.observed_yield) * 100.0 AS gap_bp
             FROM curve_residuals q
             JOIN curve_residuals t
               ON t.obs_date = q.obs_date AND t.isin = q.isin AND t.source = 'trade'
            WHERE q.source = 'quote'
              AND q.obs_date > date(?, ?) AND q.obs_date <= ?""",
        (obs_date, f"-{window} days", obs_date)).fetchall()


def gap(conn, obs_date: str) -> dict | None:
    """The current quote-to-trade gap, or None when too little has traded.

    Measured over a trailing window ending on `obs_date`, so a historical
    day is judged on what was knowable then.
    """
    rows = _pairs(conn, obs_date, WINDOW_DAYS)
    if len(rows) < MIN_OBSERVATIONS:
        return None
    values = [row["gap_bp"] for row in rows]
    return {"gap_bp": _median(values), "n": len(values), "window_days": WINDOW_DAYS}


def by_tenor(conn, obs_date: str, window: int = 365) -> dict:
    """Diagnostic only: the same gap split by tenor bucket.

    Reported by `python -m signals.validate` so the tenor structure can be
    inspected, and deliberately not fed into any bond's numbers.
    """
    rows = _pairs(conn, obs_date, window)
    out = {}
    for low, high, label in BUCKETS:
        sample = [row["gap_bp"] for row in rows if low <= row["tau"] < high]
        if sample:
            out[label] = {"gap_bp": _median(sample), "n": len(sample)}
    return out


def describe(measured: dict | None) -> str:
    """One line for a report header."""
    if not measured:
        return "quote-to-trade gap: too few trades in the last "\
               f"{WINDOW_DAYS} days to measure"
    direction = "cheaper than" if measured["gap_bp"] > 0 else "richer than"
    return (f"trades printing {abs(measured['gap_bp']):.0f}bp {direction} the quote "
            f"mid over the last {measured['window_days']} days "
            f"(n={measured['n']})")
