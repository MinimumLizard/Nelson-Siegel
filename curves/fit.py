"""Fit one Nelson-Siegel curve per day and store the residuals.

    python -m curves.fit                     # every day not yet fitted
    python -m curves.fit --all               # refit everything
    python -m curves.fit --date 2026-08-28 --plot
    python -m curves.fit --calibrate        # re-choose lambda, refit everything

**What goes into the fit, and why.** The curve is fitted on the dealers'
two-way quote MIDS, not on executed trades, because coverage decides curve
quality: quotes cover 44-46 bonds every single business day spanning
roughly 1 to 13 years, while trades cover about 11 bonds a day (as few as
2) and on a typical day reach neither the short end nor past 10 years.
A curve fitted only on trades would be extrapolating at both ends and
would lurch around as the traded set changed, which reads as fake
richness and cheapness.

**What trades are used for instead.** Executed yields run about 11bp above
quote mids on average — real business clears cheaper than dealers show —
so mixing the two would move the curve by ~10bp depending on which bonds
happened to trade. Instead trades are held OUT of the fit and compared
against it afterwards: `trade_bias_bp` measures that quote-to-trade gap
each day, and `trade_rmse_bp` is an honest out-of-sample error. Both are
stored per day so the curve can be checked rather than trusted.

**Weighting.** Each quote is weighted by 1/spread^2: the bid-offer spread
is the dealers' own statement of how sure they are. The median spread is
about 16bp but the tail reaches several hundred, and those wide quotes
carry almost no information — this single choice does more for fit
quality than anything else here.

**Exclusions.** Step-coupon restructuring bonds are left out: their quotes
look administered, and a stepped cash flow is not comparable with a
fixed-coupon bond on a single yield axis anyway.
"""

import argparse
import datetime as dt
import logging

import numpy as np

from pipeline import db, series
from curves import nelson_siegel as ns

log = logging.getLogger(__name__)

# lambda is calibrated ONCE across the whole sample and then held fixed for
# every day — see calibrate_lambda(). Used if calibration has not been run.
DEFAULT_LAMBDA_YEARS = 2.5

MIN_TAU_YEARS = 0.08     # under a month: price is noise, not curve
MIN_BONDS = 6            # fewer than this and the shape is not identified
MIN_SPREAD_BP = 1.0      # floor, so a zero spread cannot dominate the fit
MAX_SPREAD_BP = 300.0    # beyond this the quote carries no information


def _years(obs_date: dt.date, maturity: dt.date) -> float:
    return (maturity - obs_date).days / 365.25


def load_day(conn, obs_date: str):
    """Quotes to fit, and the day's trades to check the fit against."""
    quotes, trades = [], []
    day = dt.date.fromisoformat(obs_date)

    for row in conn.execute(
            """SELECT o.isin, o.mid_yield, o.bid_yield, o.offer_yield,
                      b.maturity_date, b.series_label, b.notes
                 FROM observations o JOIN bonds b USING(isin)
                WHERE o.obs_date = ? AND o.source = 'pdmo_daily'
                  AND o.mid_yield IS NOT NULL AND b.maturity_date IS NOT NULL""",
            (obs_date,)):
        # Step-coupon bonds chain several rates in their label.
        if len(series.coupon_steps(row["series_label"])) > 1 or row["notes"]:
            continue
        tau = _years(day, dt.date.fromisoformat(row["maturity_date"]))
        if tau < MIN_TAU_YEARS:
            continue
        spread = None
        if row["bid_yield"] is not None and row["offer_yield"] is not None:
            spread = abs(row["bid_yield"] - row["offer_yield"]) * 100.0
        quotes.append({"isin": row["isin"], "tau": tau,
                       "yield": row["mid_yield"], "spread_bp": spread})

    for row in conn.execute(
            """SELECT t.isin, t.wavg_yield, b.maturity_date, b.series_label, b.notes
                 FROM trade_summary t JOIN bonds b USING(isin)
                WHERE t.obs_date = ? AND t.security_type = 'TBond'
                  AND t.wavg_yield IS NOT NULL AND b.maturity_date IS NOT NULL""",
            (obs_date,)):
        if len(series.coupon_steps(row["series_label"])) > 1 or row["notes"]:
            continue
        tau = _years(day, dt.date.fromisoformat(row["maturity_date"]))
        if tau < MIN_TAU_YEARS:
            continue
        trades.append({"isin": row["isin"], "tau": tau, "yield": row["wavg_yield"]})

    return quotes, trades


def weights_from_spreads(quotes) -> np.ndarray:
    """1/spread^2, with the median standing in for a missing spread.

    Normalised to average 1 so the stored weights are readable as
    "this quote counted N times as much as an average one".
    """
    known = [q["spread_bp"] for q in quotes if q["spread_bp"] is not None]
    fallback = float(np.median(known)) if known else 20.0
    spreads = np.array([
        min(max(q["spread_bp"] if q["spread_bp"] is not None else fallback,
                MIN_SPREAD_BP), MAX_SPREAD_BP)
        for q in quotes])
    weights = 1.0 / spreads**2
    return weights / weights.mean()


def get_lambda(conn) -> float:
    """The calibrated lambda, or the default if calibration has not run."""
    row = conn.execute("SELECT value FROM curve_settings WHERE key='lambda_years'").fetchone()
    return float(row["value"]) if row else DEFAULT_LAMBDA_YEARS


def calibrate_lambda(conn, dates) -> float:
    """Choose ONE lambda for the whole sample, by pooled weighted error.

    Fitting lambda separately each day tracks each day marginally better
    but destroys comparability: the three betas only mean "level, slope,
    curvature" relative to a fixed lambda, and letting it drift turns a
    change in the curve's shape into a change in its parameterisation.
    Worse on this data, a free lambda ran to the top of the search range
    on 29% of days, where the slope and curvature factors are nearly
    collinear and the betas stop being separately identified.

    So lambda is picked once, here, as the value minimising total weighted
    squared error across every day, and then held fixed.
    """
    days = []
    for obs_date in dates:
        quotes, _ = load_day(conn, obs_date)
        if len(quotes) >= MIN_BONDS:
            days.append((np.array([q["tau"] for q in quotes]),
                         np.array([q["yield"] for q in quotes]),
                         weights_from_spreads(quotes)))
    if not days:
        raise SystemExit("no days with enough quotes to calibrate lambda")

    best = None
    for lam in ns.LAMBDA_GRID:
        total = sum(ns.fit_fixed(tau, y, lam, w)[2] for tau, y, w in days)
        if best is None or total < best[0]:
            best = (total, float(lam))
    _, lam = best
    conn.execute("""INSERT INTO curve_settings (key, value) VALUES ('lambda_years', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (str(lam),))
    conn.commit()
    log.info("calibrated lambda = %.3f years over %d days", lam, len(days))
    return lam


def fit_day(conn, obs_date: str, lam: float | None = None):
    """Fit one day; returns a summary dict, or None if not enough bonds."""
    quotes, trades = load_day(conn, obs_date)
    if len(quotes) < MIN_BONDS:
        log.warning("%s: only %d usable quotes — skipped", obs_date, len(quotes))
        return None

    lam = get_lambda(conn) if lam is None else lam
    tau = np.array([q["tau"] for q in quotes])
    observed = np.array([q["yield"] for q in quotes])
    weights = weights_from_spreads(quotes)
    betas, fitted, _ = ns.fit_fixed(tau, observed, lam, weights)

    summary = {
        "obs_date": obs_date,
        "beta0": float(betas[0]), "beta1": float(betas[1]), "beta2": float(betas[2]),
        "lambda_years": lam,
        "n_quotes": len(quotes),
        "rmse_bp": ns.weighted_rmse_bp(observed, fitted, weights),
        "n_trades": 0, "trade_rmse_bp": None, "trade_bias_bp": None,
    }

    rows = [("quote", q, obs, fit_value, weight)
            for q, obs, fit_value, weight in zip(quotes, observed, fitted, weights)]

    # Trades are NOT in the fit: comparing them to it is the daily check.
    if trades:
        trade_tau = np.array([t["tau"] for t in trades])
        trade_obs = np.array([t["yield"] for t in trades])
        trade_fit = ns.predict(trade_tau, betas, lam)
        errors_bp = (trade_obs - trade_fit) * 100.0
        summary["n_trades"] = len(trades)
        summary["trade_rmse_bp"] = float(np.sqrt(np.mean(errors_bp**2)))
        summary["trade_bias_bp"] = float(np.mean(errors_bp))
        rows += [("trade", t, obs, fit_value, None)
                 for t, obs, fit_value in zip(trades, trade_obs, trade_fit)]

    _store(conn, summary, rows)
    return summary


def _store(conn, summary, rows) -> None:
    conn.execute("DELETE FROM curve_residuals WHERE obs_date = ?", (summary["obs_date"],))
    conn.execute(
        """INSERT INTO curve_fits (obs_date, beta0, beta1, beta2, lambda_years,
                                   n_quotes, rmse_bp, n_trades, trade_rmse_bp,
                                   trade_bias_bp, fitted_at)
           VALUES (:obs_date, :beta0, :beta1, :beta2, :lambda_years, :n_quotes,
                   :rmse_bp, :n_trades, :trade_rmse_bp, :trade_bias_bp, :fitted_at)
           ON CONFLICT(obs_date) DO UPDATE SET
               beta0=excluded.beta0, beta1=excluded.beta1, beta2=excluded.beta2,
               lambda_years=excluded.lambda_years, n_quotes=excluded.n_quotes,
               rmse_bp=excluded.rmse_bp, n_trades=excluded.n_trades,
               trade_rmse_bp=excluded.trade_rmse_bp,
               trade_bias_bp=excluded.trade_bias_bp, fitted_at=excluded.fitted_at""",
        {**summary, "fitted_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")})
    conn.executemany(
        """INSERT INTO curve_residuals (obs_date, isin, source, tau_years,
                                        observed_yield, fitted_yield, residual_bp, weight)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(summary["obs_date"], point["isin"], source, point["tau"],
          float(observed), float(fitted), float((observed - fitted) * 100.0),
          None if weight is None else float(weight))
         for source, point, observed, fitted, weight in rows])


def available_dates(conn, only_new: bool):
    dates = [row["obs_date"] for row in conn.execute(
        """SELECT DISTINCT obs_date FROM observations
            WHERE source='pdmo_daily' AND mid_yield IS NOT NULL ORDER BY obs_date""")]
    if only_new:
        done = {row["obs_date"] for row in conn.execute("SELECT obs_date FROM curve_fits")}
        dates = [d for d in dates if d not in done]
    return dates


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="fit only this date (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true",
                        help="refit days already fitted (default: only new days)")
    parser.add_argument("--calibrate", action="store_true",
                        help="re-choose the sample-wide lambda, then refit every day")
    parser.add_argument("--plot", action="store_true",
                        help="save a chart of the fitted curve vs the day's trades")
    parser.add_argument("--plot-latest", action="store_true",
                        help="(re)draw data/reports/curve_latest.png for the newest "
                             "fitted day — one stable filename, so the daily job "
                             "does not accumulate a chart per date in the repo")
    args = parser.parse_args()

    conn = db.connect()
    if args.calibrate:
        lam = calibrate_lambda(conn, available_dates(conn, only_new=False))
        print(f"calibrated lambda = {lam:.3f} years; refitting every day")
        args.all = True
    dates = [args.date] if args.date else available_dates(conn, only_new=not args.all)
    if not dates:
        # Not an error, and not a reason to skip the chart: on most days the
        # daily job finds nothing new and still wants curve_latest.png drawn.
        print("nothing to fit — every day already has a curve (use --all to refit)")
        _draw(conn, args, fallback_date=None)
        return

    lam = get_lambda(conn)
    fitted = 0
    for obs_date in dates:
        summary = fit_day(conn, obs_date, lam)
        if summary:
            fitted += 1
            if args.date:
                print(f"{obs_date}: {summary['n_quotes']} bonds, "
                      f"fit RMSE {summary['rmse_bp']:.1f}bp, "
                      f"lambda {summary['lambda_years']:.2f}y")
                if summary["n_trades"]:
                    print(f"  checked against {summary['n_trades']} traded bonds: "
                          f"bias {summary['trade_bias_bp']:+.1f}bp, "
                          f"RMSE {summary['trade_rmse_bp']:.1f}bp")
    conn.commit()
    print(f"fitted {fitted} day(s)")
    _draw(conn, args, fallback_date=dates[-1])


def _draw(conn, args, fallback_date) -> None:
    """Charts requested on the command line, if any."""
    if not (args.plot or args.plot_latest):
        return
    from curves import plot
    if args.plot and fallback_date:
        plot.plot_day(conn, fallback_date)
    if args.plot_latest:
        row = conn.execute("SELECT MAX(obs_date) AS d FROM curve_fits").fetchone()
        if row and row["d"]:
            plot.plot_day(conn, row["d"], plot.REPORTS_DIR / "curve_latest.png")


if __name__ == "__main__":
    main()
