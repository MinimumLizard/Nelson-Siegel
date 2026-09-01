"""Compute and store rich/cheap and switch signals.

    python -m signals.run              # rebuild every signal from the residuals

Signals are cheap to recompute from `curve_residuals` and depend on a
trailing window, so the whole table is rebuilt rather than appended to:
that way a change to the window length or the pair rule can never leave
half the history computed under the old rules.
"""

import argparse
import logging

from pipeline import db
from signals import zscore

log = logging.getLogger(__name__)


def rebuild(conn) -> dict:
    residuals = zscore.load_residuals(conn)
    if residuals.empty:
        raise SystemExit("no curve residuals — run: python -m curves.fit")

    bonds = zscore.bond_signals(residuals)
    bonds["dislocation_bp"] = bonds["residual_bp"] - bonds["mean_bp"]
    switches = zscore.switch_signals(residuals)
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
    conn.commit()
    return {"bond_signals": len(bonds), "switch_signals": len(switches)}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    argparse.ArgumentParser(description=__doc__).parse_args()
    counts = rebuild(db.connect())
    print(f"stored {counts['bond_signals']} bond signals "
          f"and {counts['switch_signals']} switch signals")


if __name__ == "__main__":
    main()
