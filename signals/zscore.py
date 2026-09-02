"""Turning curve residuals into rich/cheap signals.

The curve stage leaves one number per bond per day: `residual_bp`, how far
the bond's yield sits from the fitted curve, positive meaning it yields
more than the curve says — cheap.

That number is NOT a signal on its own. Most of its variation is
cross-sectional: some bonds simply sit persistently cheap (off-the-run,
less liquid, held by someone who never sells), and would show a large
positive residual every single day without ever being an opportunity.
Measured on this data the spread of residuals ACROSS bonds is 41.6bp while
a typical bond's own residual moves with a standard deviation of only
7.5bp.

So the signal is each bond against ITS OWN recent history:

    z = (today's residual - mean of the trailing window) / sd of that window

A z of +2 means "this bond is unusually cheap for this bond", which is the
question a switch trade actually asks.

**The window excludes today.** Statistics are computed over the N days
BEFORE the observation being scored, so nothing in a z-score has seen the
value it is scoring. That keeps the stored history honest as a backtest
rather than something that quietly used tomorrow's information.

Switch signals apply the same idea to a PAIR of bonds: the spread between
two residuals, z-scored against its own trailing window.

Which pairs are worth tracking depends on the bonds. For ordinary paper a
switch means selling one bond and buying its neighbour, so pairs are
limited to maturities within a couple of years — otherwise the "switch" is
really a bet on the shape of the whole curve. But the auction benchmarks
are the exception: they are spread from 2030 to 2037 and a book trades
between them across that whole range, so any two of them count as a pair
regardless of the gap. Membership is decided by whether a bond has EVER
been auctioned, not by whether it is on the run today, so the pair universe
does not shift under the historical z-scores.
"""

import itertools

import pandas as pd

WINDOW_DAYS = 60      # trailing window; ~3 months of business days
MIN_OBSERVATIONS = 30  # below this a sd is too noisy to divide by
MAX_TAU_GAP_YEARS = 2.0  # how far apart two bonds may be to count as a switch


def load_residuals(conn) -> pd.DataFrame:
    """Quote residuals, long format: one row per bond per day."""
    frame = pd.read_sql_query(
        """SELECT obs_date, isin, residual_bp, tau_years
             FROM curve_residuals WHERE source = 'quote'
            ORDER BY isin, obs_date""", conn)
    frame["obs_date"] = pd.to_datetime(frame["obs_date"])
    return frame


def _rolling_z(values: pd.Series) -> pd.DataFrame:
    """z-score of each value against the window of values BEFORE it."""
    previous = values.shift(1)  # excluding today: no lookahead
    mean = previous.rolling(WINDOW_DAYS, min_periods=MIN_OBSERVATIONS).mean()
    sd = previous.rolling(WINDOW_DAYS, min_periods=MIN_OBSERVATIONS).std()
    count = previous.rolling(WINDOW_DAYS, min_periods=MIN_OBSERVATIONS).count()
    return pd.DataFrame({
        "mean_bp": mean,
        "sd_bp": sd,
        # A near-zero sd would manufacture enormous z-scores from noise.
        "zscore": (values - mean) / sd.where(sd > 0.5),
        "n_window": count,
    })


def bond_signals(residuals: pd.DataFrame) -> pd.DataFrame:
    """Per-bond rich/cheap z-scores."""
    out = []
    for isin, group in residuals.groupby("isin"):
        group = group.sort_values("obs_date").reset_index(drop=True)
        stats = _rolling_z(group["residual_bp"])
        out.append(pd.concat([group[["obs_date", "isin", "residual_bp"]], stats], axis=1))
    if not out:
        return pd.DataFrame()
    return pd.concat(out).dropna(subset=["zscore"]).reset_index(drop=True)


def candidate_pairs(residuals: pd.DataFrame, auctioned=None) -> list[tuple[str, str]]:
    """Pairs worth tracking: near neighbours, plus any two auction bonds."""
    auctioned = set(auctioned or ())
    tau = residuals.groupby("isin")["tau_years"].median()
    return [(a, b) for a, b in itertools.combinations(sorted(tau.index), 2)
            if abs(tau[a] - tau[b]) <= MAX_TAU_GAP_YEARS
            or (a in auctioned and b in auctioned)]


def switch_signals(residuals: pd.DataFrame, auctioned=None) -> pd.DataFrame:
    """Z-scores of the residual SPREAD within each candidate pair.

    A positive z means bond A has become unusually cheap relative to B —
    the switch is sell B, buy A.
    """
    wide = residuals.pivot_table(index="obs_date", columns="isin", values="residual_bp")
    tau = residuals.groupby("isin")["tau_years"].median()

    out = []
    for first, second in candidate_pairs(residuals, auctioned):
        spread = (wide[first] - wide[second]).dropna()
        if len(spread) < MIN_OBSERVATIONS + 1:
            continue
        stats = _rolling_z(spread)
        frame = pd.DataFrame({
            "obs_date": spread.index,
            "isin_a": first,
            "isin_b": second,
            "spread_bp": spread.values,
            "tau_a": tau[first],
            "tau_b": tau[second],
        }).join(stats.reset_index(drop=True))
        out.append(frame)
    if not out:
        return pd.DataFrame()
    return pd.concat(out).dropna(subset=["zscore"]).reset_index(drop=True)
