"""`python -m pipeline.report --isin LKB00934F154` — one bond's history.

Prints the bond's identity, a merged daily table (PD two-way quotes from
the daily summaries + executed trades from the trade summaries), and saves
a two-panel yield/volume chart to data/reports/<ISIN>.png.

Think of the SQL below as two data frames merged on obs_date — the same
full_join(quotes, trades) you would write in dplyr.

**Two sources report traded volume, and they are dated differently.** The
Outright Transactions Volumes file and the Secondary Market Trade Summary
carry identical amounts two business days apart: on 938 matched amounts in
this archive the volumes file sits +2 business days after the trade
summary, against 59 matches at no offset. The cause is settlement. The
volumes file's date is derived from a remaining-years column quoted to
SETTLEMENT, and PDMO settlement is T+2 on 9 of the 16 auctions here.

So the chart and the traded column use the TRADE SUMMARY, which is dated to
the trade. The volumes figure is still printed, under the name `settled_mn`
rather than a bare `volume_mn`, because it is real published data and
because a column silently two days out is exactly what makes someone
distrust a model that is otherwise right.

Nothing else reads the volumes-file column: the curve, the signals,
liquidity tiering and carry all take their turnover from the trade summary.
"""

import argparse
import sys

import matplotlib

matplotlib.use("Agg")  # write PNGs without needing a display
import matplotlib.pyplot as plt
import pandas as pd

from pipeline import config, db

REPORTS_DIR = config.DATA_DIR / "reports"


def load_history(conn, isin: str) -> pd.DataFrame:
    quotes = pd.read_sql_query(
        """SELECT obs_date, bid_yield, offer_yield, mid_yield, volume_lkr
           FROM observations WHERE isin = ? AND source = 'pdmo_daily'
           ORDER BY obs_date""",
        conn, params=(isin,))
    trades = pd.read_sql_query(
        """SELECT obs_date, wavg_yield AS traded_yield,
                  volume_lkr AS traded_lkr, n_trades
           FROM trade_summary WHERE isin = ? ORDER BY obs_date""",
        conn, params=(isin,))
    merged = quotes.merge(trades, on="obs_date", how="outer").sort_values("obs_date")
    merged["obs_date"] = pd.to_datetime(merged["obs_date"])
    return merged.reset_index(drop=True)


def plot_history(history: pd.DataFrame, isin: str, title: str):
    figure, (yield_ax, volume_ax) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]})

    yield_ax.plot(history["obs_date"], history["bid_yield"],
                  label="bid yield (PD quotes)", lw=1)
    yield_ax.plot(history["obs_date"], history["offer_yield"],
                  label="offer yield (PD quotes)", lw=1)
    yield_ax.plot(history["obs_date"], history["traded_yield"],
                  "o", ms=3, label="traded wavg yield")
    yield_ax.set_ylabel("yield, % p.a.")
    yield_ax.set_title(title)
    yield_ax.legend(loc="best", fontsize=8)
    yield_ax.grid(alpha=0.3)

    # Executed volume from the trade summary, which is dated to the TRADE.
    # The volumes file reports the same amounts two business days later,
    # because its date derives from a remaining-years column quoted to
    # settlement; plotting that would put every bar two days late.
    volume_ax.bar(history["obs_date"], history["traded_lkr"] / 1e9, width=1.0)
    volume_ax.set_ylabel("traded, Rs. bn")
    volume_ax.grid(alpha=0.3)
    figure.autofmt_xdate()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{isin}.png"
    figure.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(figure)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isin", required=True, help="bond ISIN, e.g. LKB00934F154")
    parser.add_argument("--tail", type=int, default=20,
                        help="how many most-recent rows to print (default 20)")
    args = parser.parse_args()
    isin = args.isin.strip().upper()

    conn = db.connect()
    bond = conn.execute("SELECT * FROM bonds WHERE isin = ?", (isin,)).fetchone()
    history = load_history(conn, isin)
    if history.empty:
        sys.exit(f"no observations for {isin} — has the backfill run?")

    if bond:
        coupon = f"{bond['coupon_pct']}%" if bond["coupon_pct"] is not None else "?"
        label = bond["notes"] or coupon
        title = f"{isin}  ({label}, matures {bond['maturity_date']})"
    else:
        title = isin
    print(title)
    print(f"{len(history)} daily rows, "
          f"{history['obs_date'].min():%Y-%m-%d} .. {history['obs_date'].max():%Y-%m-%d}")

    printable = history.tail(args.tail).copy()
    printable["obs_date"] = printable["obs_date"].dt.strftime("%Y-%m-%d")
    for column in ("volume_lkr", "traded_lkr"):
        printable[column] = (printable[column] / 1e6).round(1)  # show Rs. mn
    printable = printable.rename(columns={"volume_lkr": "settled_mn",
                                          "traded_lkr": "traded_mn"})
    print(printable.to_string(index=False, na_rep="."))

    out_path = plot_history(history, isin, title)
    print(f"\nchart saved to {out_path}")


if __name__ == "__main__":
    main()
