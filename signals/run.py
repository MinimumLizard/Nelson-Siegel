"""Compute and store rich/cheap and switch signals.

    python -m signals.run              # rebuild every signal from the residuals

Signals are cheap to recompute from `curve_residuals` and depend on a
trailing window, so the whole table is rebuilt rather than appended to:
that way a change to the window length or the pair rule can never leave
half the history computed under the old rules.
"""

import argparse
import logging

import numpy as np
import pandas as pd

from pipeline import db
from signals import zscore

# Horizon and thresholds the report quotes a signal's worth at. Measured on
# every rebuild rather than pasted in: the previous figures were taken on a
# 45-bond curve and were still being printed after it grew to 53, which
# understated the 10-day capture enough to flip a verdict.
REVERSION_HORIZON_DAYS = 10
REVERSION_THRESHOLDS = (2.0, 3.0)

log = logging.getLogger(__name__)


def rebuild(conn) -> dict:
    residuals = zscore.load_residuals(conn)
    if residuals.empty:
        raise SystemExit("no curve residuals — run: python -m curves.fit")

    bonds = zscore.bond_signals(residuals)
    bonds["dislocation_bp"] = bonds["residual_bp"] - bonds["mean_bp"]
    # Bonds ever auctioned pair with each other across the whole curve, which
    # is how a benchmark book actually trades. "Ever" rather than "currently"
    # keeps the pair universe fixed under the historical z-scores.
    auctioned = {row["isin"] for row in conn.execute("SELECT DISTINCT isin FROM auctions")}
    switches = zscore.switch_signals(residuals, auctioned)
    if not switches.empty:
        switches["dislocation_bp"] = switches["spread_bp"] - switches["mean_bp"]

    conn.execute("DELETE FROM bond_signals")
    conn.executemany(
        """INSERT INTO bond_signals (obs_date, isin, residual_bp, mean_bp, sd_bp,
                                     dislocation_bp, zscore, n_window)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(row.obs_date.date().isoformat(), row.isin, row.residual_bp, row.mean_bp,
          row.sd_bp, row.dislocation_bp, row.zscore, int(row.n_window))
         for row in bonds.itertuples()])

    conn.execute("DELETE FROM switch_signals")
    if not switches.empty:
        conn.executemany(
            """INSERT INTO switch_signals (obs_date, isin_a, isin_b, tau_a, tau_b,
                                           spread_bp, mean_bp, sd_bp,
                                           dislocation_bp, zscore, n_window)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(row.obs_date.date().isoformat(), row.isin_a, row.isin_b, row.tau_a,
              row.tau_b, row.spread_bp, row.mean_bp, row.sd_bp, row.dislocation_bp,
              row.zscore, int(row.n_window)) for row in switches.itertuples()])
    measure_reversion(conn, switches)
    conn.commit()
    return {"bond_signals": len(bonds), "switch_signals": len(switches)}


# A pair may be missing an observation, so the row N places later is not the
# row N business days later. Allow a short search forward from the target date
# before giving up on a reading.
REVERSION_TOLERANCE_DAYS = 4


def _forward_change(group) -> list:
    """Change in the pair spread REVERSION_HORIZON_DAYS business days later.

    Indexed by date rather than by row. `shift(-10)` looks ten OBSERVATIONS
    ahead, and a pair that missed a day is then measured over eleven or
    twelve — on this data a ten-row shift spanned exactly ten business days
    only 45% of the time, a median of eleven, and up to fourteen. Reversion
    grows with horizon, so a row-indexed reading quietly overstates what a
    ten-day capture is worth and the label stops describing the number.
    """
    import datetime as dt

    by_date = dict(zip(group.obs_date, group.spread_bp))
    offset = pd.tseries.offsets.BDay(REVERSION_HORIZON_DAYS)
    out = []
    for when, spread in zip(group.obs_date, group.spread_bp):
        target = when + offset
        found = None
        for slip in range(REVERSION_TOLERANCE_DAYS + 1):
            candidate = target + dt.timedelta(days=slip)
            if candidate in by_date:
                found = by_date[candidate]
                break
        out.append(float("nan") if found is None else found - spread)
    return out


def measure_reversion(conn, switches) -> dict:
    """How far a pair spread actually came back, per z threshold.

    This is what `signals.report` prints as `exp` and uses to decide whether
    a candidate clears its own costs, so it has to describe the signals that
    are actually stored — not a curve from three model versions ago.

    In-sample throughout, and on one regime, so it measures that the
    mechanism reverts rather than forecasting a strategy's return.
    """
    stored = {}
    if switches.empty:
        return stored
    frame = switches.copy()
    frame["pair"] = frame.isin_a + "/" + frame.isin_b
    forward = []
    for _, group in frame.groupby("pair"):
        group = group.sort_values("obs_date").copy()
        group["forward"] = _forward_change(group)
        forward.append(group)
    measured = pd.concat(forward).dropna(subset=["forward"])
    if measured.empty:
        return stored
    # Reversion is movement OPPOSITE the signal, so a cheap pair narrowing
    # counts as a positive capture.
    capture = -np.sign(measured.zscore) * measured.forward
    for threshold in REVERSION_THRESHOLDS:
        extreme = abs(measured.zscore) >= threshold
        if extreme.sum() < 30:            # too thin to quote at all
            continue
        key = f"switch_reversion_{REVERSION_HORIZON_DAYS}d_z{threshold:g}"
        db.store_signal_stat(conn, key, capture[extreme].mean(), int(extreme.sum()))
        stored[key] = capture[extreme].mean()
        log.info("%s = %+.1fbp over %d pair-days", key,
                 capture[extreme].mean(), int(extreme.sum()))
    return stored


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    argparse.ArgumentParser(description=__doc__).parse_args()
    counts = rebuild(db.connect())
    print(f"stored {counts['bond_signals']} bond signals "
          f"and {counts['switch_signals']} switch signals")


if __name__ == "__main__":
    main()
