"""Does the signal actually predict anything? Re-run this as history grows.

    python -m signals.validate

Every number below is measured on stored signals, using only information
available on the signal date: the z-score's window ends the day before the
observation it scores, and the outcome is the change in the following days.
It is still a single 9-month sample in one regime, so treat it as evidence
that the mechanism works, not as an estimate of what it will pay.
"""

import argparse

import numpy as np
import pandas as pd

from pipeline import db

HORIZONS = (5, 10, 20)
BUCKETS = [-np.inf, -3, -2, -1, 1, 2, 3, np.inf]
BUCKET_LABELS = ["z<-3", "-3..-2", "-2..-1", "-1..1", "1..2", "2..3", "z>3"]


def _forward_changes(frame, key, value_column):
    """Add the change in `value_column` over each horizon, per series."""
    out = []
    for _, group in frame.groupby(key):
        group = group.sort_values("obs_date").copy()
        for horizon in HORIZONS:
            group[f"fwd{horizon}"] = group[value_column].shift(-horizon) - group[value_column]
        out.append(group)
    return pd.concat(out)


def _report(frame, title):
    print(f"\n{title}")
    frame = frame.copy()
    frame["bucket"] = pd.cut(frame["zscore"], BUCKETS, labels=BUCKET_LABELS)
    header = "  " + f"{'z bucket':<10}" + "".join(f"{f'{h}d':>10}" for h in HORIZONS) + f"{'n':>8}"
    print(header)
    for label in BUCKET_LABELS:
        subset = frame[frame["bucket"] == label]
        if subset.empty:
            continue
        cells = "".join(f"{subset[f'fwd{h}'].mean():>+10.1f}" for h in HORIZONS)
        print(f"  {label:<10}{cells}{len(subset):>8}")
    print("  (reversion = sign opposite to the bucket: cheap should narrow)")

    for horizon in HORIZONS:
        valid = frame.dropna(subset=[f"fwd{horizon}"])
        if len(valid) > 10:
            correlation = np.corrcoef(valid["zscore"], valid[f"fwd{horizon}"])[0, 1]
            extreme = valid[abs(valid["zscore"]) > 2]
            capture = (-np.sign(extreme["zscore"]) * extreme[f"fwd{horizon}"]).mean()
            hit = ((-np.sign(extreme["zscore"]) * extreme[f"fwd{horizon}"]) > 0).mean()
            print(f"  {horizon:>2}d: corr(z, change) {correlation:+.3f} | "
                  f"|z|>2 captures {capture:+.1f}bp, right direction {hit:.0%} "
                  f"(n={len(extreme)})")


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    conn = db.connect()

    bonds = pd.read_sql_query(
        "SELECT obs_date, isin, residual_bp, zscore FROM bond_signals", conn)
    switches = pd.read_sql_query(
        "SELECT obs_date, isin_a, isin_b, spread_bp, zscore FROM switch_signals", conn)
    if bonds.empty:
        raise SystemExit("no signals stored — run: python -m signals.run")

    bonds["obs_date"] = pd.to_datetime(bonds["obs_date"])
    print(f"{len(bonds)} bond-days, {bonds['isin'].nunique()} bonds, "
          f"{bonds['obs_date'].dt.date.min()} .. {bonds['obs_date'].dt.date.max()}")
    _report(_forward_changes(bonds, "isin", "residual_bp"),
            "PER-BOND: mean change in residual (bp) after the signal")

    if not switches.empty:
        switches["obs_date"] = pd.to_datetime(switches["obs_date"])
        switches["pair"] = switches["isin_a"] + "/" + switches["isin_b"]
        print(f"\n{len(switches)} pair-days across "
              f"{switches['pair'].nunique()} candidate switch pairs")
        _report(_forward_changes(switches, "pair", "spread_bp"),
                "SWITCH PAIRS: mean change in the pair spread (bp) after the signal")

    print("\nOne 9-month sample, one regime, in-sample throughout: evidence that the "
          "residuals mean-revert,\nnot a forecast of what a strategy would earn. "
          "Compare any capture against the bid-offer\ncost shown by "
          "`python -m signals.report` before concluding a trade is worth doing.")


if __name__ == "__main__":
    main()
