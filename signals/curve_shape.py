"""How steep the curve is: the yield gap between the 2-year point and 5, 8,
10 and 12 years.

    python -m signals.validate      # prints the history and the tests below

These are the curve's SHAPE, where everything else in `signals/` is about
one bond against the curve. "2s10s" (said "twos tens") is the 10-year yield
less the 2-year: the usual one-number summary of how much more the market
asks to lend for longer. Wide means steep; near zero means flat; negative
means inverted. 2s5s, 2s8s and 2s12s ask the same question at other points,
and between them say WHERE along the curve a steepening or flattening is
happening.

**How each point is measured.** There is no bond at exactly 2 or 10 years,
so the yield at each point starts from the fitted Nelson-Siegel curve. On
its own that is not good enough. The curve has three parameters, so these
four spreads only have two degrees of freedom between them, and when the
market has a kink the curve cannot bend to follow it. In April and May 2026
the bonds near 2 years sat 16-18bp cheap to the curve and those near 5
years 15-16bp rich, so the curve's own 2s5s read about 30bp steeper than
any pair of actual bonds did; its level correlated -0.23 with a spread read
straight off the quotes.

So each point is the curve PLUS what the bonds near it are doing: the
weighted average residual of the quotes around that maturity, weighted by
a bell curve `KERNEL_YEARS` wide (and by each quote's own weight in the
fit). That tracks a spread interpolated from neighbouring bonds with level
correlations of 0.81 to 0.93 across the four pairs, against -0.23 to 0.84
for the curve alone, and day-to-day changes show no bounce-back (lag-1
autocorrelation +0.03 to +0.19), so it is not trading quote noise for
model error. A narrower bell tracked the quotes a little more closely but
rests the 12-year point on a single bond; this width spreads it over three.

**What it is for, and what it is not.** Tested as a trade — enter the day
after a spread sits 1.5 sd from its own 60-day norm — only 2s5s reverted
clearly more than a random walk with the same day-to-day moves would
(about 5-6bp over 5-10 days; chance did as well 2-10% of the time). 2s8s
was borderline at five days and no better than chance after that, and
2s10s and 2s12s could not be told from chance at all. And 5-6bp is well
short of what a curve trade costs: half the bid-offer on each leg, 16-18bp
on 2s5s and more further out. So this is context for decisions made on other grounds —
how steep is the curve, compared with its own recent history — not a
signal, and the report prints the measured reversion and the cost beside
each spread so that stays visible as history grows.
"""

import numpy as np
import pandas as pd

from curves import nelson_siegel as ns
from pipeline import db
from signals import zscore

PAIRS = ((2, 5), (2, 8), (2, 10), (2, 12))   # (short, long) years
KERNEL_YEARS = 0.75    # sd of the bell curve that pools nearby bonds' residuals
COST_BAND_YEARS = 1.0  # bonds this close to a point set its bid-offer
SIGNAL_Z = 1.5         # how far from its norm a spread must be to count
REVERSION_HORIZON_DAYS = 10


def pair_name(short: float, long: float) -> str:
    return f"{short:g}s{long:g}s"


def point_yield(fit: dict, quotes: pd.DataFrame, tau: float) -> float:
    """Yield at `tau` years, percent: the curve plus nearby bonds' residuals.

    `quotes` holds that day's quote residuals (tau_years, residual_bp,
    weight). Bonds more than three bell-widths away count for nothing,
    and a point with no bond inside that is the plain curve. Without the
    cut-off a lone bond twenty years off would set the whole correction,
    because its tiny weight is still the only weight there is.
    """
    curve = float(ns.predict([tau], (fit["beta0"], fit["beta1"], fit["beta2"]),
                             fit["lambda_years"])[0])
    distance = (quotes["tau_years"] - tau) / KERNEL_YEARS
    nearness = np.where(distance.abs() <= 3.0, np.exp(-0.5 * distance ** 2), 0.0)
    weights = quotes["weight"].fillna(1.0) * nearness
    if weights.sum() <= 0:
        return curve
    return curve + float(np.average(quotes["residual_bp"], weights=weights)) / 100.0


def _half_spread(quotes: pd.DataFrame, tau: float) -> float | None:
    """Half the median bid-offer of the bonds near `tau`, bp."""
    near = quotes[(quotes["tau_years"] - tau).abs() <= COST_BAND_YEARS]["bid_offer_bp"]
    near = near.dropna()
    return float(near.median()) / 2.0 if len(near) else None


def daily_spreads(conn) -> pd.DataFrame:
    """One row per pair per fitted day: the spread and what it costs to trade."""
    fits = {row["obs_date"]: dict(row) for row in conn.execute("SELECT * FROM curve_fits")}
    quotes = pd.read_sql_query(
        """SELECT r.obs_date, r.tau_years, r.residual_bp, r.weight,
                  (o.bid_yield - o.offer_yield) * 100.0 AS bid_offer_bp
             FROM curve_residuals r
             LEFT JOIN observations o
               ON o.obs_date = r.obs_date AND o.isin = r.isin
              AND o.source = 'pdmo_daily'
            WHERE r.source = 'quote'""", conn)
    rows = []
    for obs_date, day in quotes.groupby("obs_date"):
        fit = fits.get(obs_date)
        if fit is None:
            continue
        for short, long in PAIRS:
            curve_only = ns.predict([short, long],
                                    (fit["beta0"], fit["beta1"], fit["beta2"]),
                                    fit["lambda_years"])
            halves = [_half_spread(day, tau) for tau in (short, long)]
            rows.append({
                "obs_date": obs_date, "pair": pair_name(short, long),
                "short_years": short, "long_years": long,
                "spread_bp": (point_yield(fit, day, long)
                              - point_yield(fit, day, short)) * 100.0,
                "curve_only_bp": float(curve_only[1] - curve_only[0]) * 100.0,
                "cost_bp": sum(halves) if None not in halves else None,
            })
    return pd.DataFrame(rows)


def spread_signals(spreads: pd.DataFrame) -> pd.DataFrame:
    """Each pair against its own trailing window — the same z as every bond."""
    out = []
    for _, group in spreads.groupby("pair"):
        group = group.sort_values("obs_date").reset_index(drop=True)
        out.append(pd.concat([group, zscore._rolling_z(group["spread_bp"])], axis=1))
    # Unlike the bond tables, days without a z yet are kept: the first 30
    # days of a spread's history are still history.
    return pd.concat(out).reset_index(drop=True) if out else pd.DataFrame()


def measure_reversion(conn, signals: pd.DataFrame) -> dict:
    """How far a stretched spread came back over the next ten trading days.

    Measured from the day AFTER the signal. Measuring from the signal day
    itself would count a quote that is simply wrong for one day, and fixed
    the next, as the spread coming back.

    By row rather than by calendar: every pair has a value on every fitted
    day, so ten rows on is ten trading days on.
    """
    stored = {}
    for pair, group in signals.groupby("pair"):
        group = group.sort_values("obs_date")
        start = group["spread_bp"].shift(-1)
        forward = group["spread_bp"].shift(-(REVERSION_HORIZON_DAYS + 1)) - start
        stretched = (group["zscore"].abs() >= SIGNAL_Z) & forward.notna()
        if stretched.sum() < 20:            # too thin to quote at all
            continue
        capture = (-np.sign(group["zscore"][stretched]) * forward[stretched]).mean()
        key = f"curve_reversion_{REVERSION_HORIZON_DAYS}d_{pair}"
        db.store_signal_stat(conn, key, capture, int(stretched.sum()))
        stored[pair] = capture
    return stored


def rebuild(conn) -> int:
    """Recompute every spread from the stored fits; wholesale, like the rest."""
    conn.execute("DELETE FROM curve_spreads")
    spreads = daily_spreads(conn)
    if spreads.empty:
        return 0
    signals = spread_signals(spreads)
    conn.executemany(
        """INSERT INTO curve_spreads (obs_date, pair, short_years, long_years,
                                      spread_bp, curve_only_bp, mean_bp, sd_bp,
                                      zscore, n_window, cost_bp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(row.obs_date, row.pair, row.short_years, row.long_years, row.spread_bp,
          row.curve_only_bp, _none(row.mean_bp), _none(row.sd_bp),
          _none(row.zscore), None if _none(row.n_window) is None else int(row.n_window),
          _none(row.cost_bp)) for row in signals.itertuples()])
    measure_reversion(conn, signals)
    return len(signals)


def _none(value):
    """NaN to None, so SQLite stores NULL rather than a float NaN."""
    return None if value is None or value != value else float(value)


def on(conn, obs_date: str) -> list[dict]:
    """The four spreads on `obs_date`, with their history and verdict facts."""
    stats = db.signal_stats(conn)
    out = []
    for short, long in PAIRS:
        pair = pair_name(short, long)
        row = conn.execute(
            "SELECT * FROM curve_spreads WHERE obs_date = ? AND pair = ?",
            (obs_date, pair)).fetchone()
        if row is None:
            continue
        history = [r["spread_bp"] for r in conn.execute(
            """SELECT spread_bp FROM curve_spreads
                WHERE pair = ? AND obs_date <= ? ORDER BY obs_date""", (pair, obs_date))]
        measured = stats.get(f"curve_reversion_{REVERSION_HORIZON_DAYS}d_{pair}")
        out.append(dict(row) | {
            "low_bp": min(history), "high_bp": max(history),
            "change_20d_bp": (history[-1] - history[-21]) if len(history) > 20 else None,
            "reversion_bp": measured["value"] if measured else None,
        })
    return out


def verdict(row: dict) -> tuple[bool, str]:
    """(clears costs?, the word for it) for one row of `on`."""
    if row["reversion_bp"] is None:
        return False, "not measured yet"
    if row["cost_bp"] is None:
        return False, "cost unknown"
    clears = row["reversion_bp"] > row["cost_bp"]
    return clears, "clears costs" if clears else "below costs"


def history(conn) -> dict[str, list[tuple[str, float]]]:
    """{pair: [(date, spread_bp), ...]} for charting."""
    out: dict[str, list] = {}
    for row in conn.execute(
            "SELECT obs_date, pair, spread_bp FROM curve_spreads ORDER BY obs_date"):
        out.setdefault(row["pair"], []).append((row["obs_date"], row["spread_bp"]))
    return out
