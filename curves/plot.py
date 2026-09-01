"""Draw one day's fitted curve against the bonds behind it.

The chart is the honesty check made visual: the fitted line, the quotes it
was fitted on (sized by the weight each carried), and the bonds that
actually traded that day — which were held OUT of the fit. If the curve is
any good, the traded points sit close to the line, a little above it (real
business clears cheaper than dealers quote).
"""

import datetime as dt

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from pipeline import config
from curves import nelson_siegel as ns

REPORTS_DIR = config.DATA_DIR / "reports"


def plot_day(conn, obs_date: str, out_path=None):
    fit = conn.execute("SELECT * FROM curve_fits WHERE obs_date = ?", (obs_date,)).fetchone()
    if fit is None:
        raise SystemExit(f"no fitted curve for {obs_date} — run: python -m curves.fit --date {obs_date}")
    residuals = conn.execute(
        "SELECT * FROM curve_residuals WHERE obs_date = ? ORDER BY tau_years", (obs_date,)).fetchall()

    quotes = [r for r in residuals if r["source"] == "quote"]
    trades = [r for r in residuals if r["source"] == "trade"]
    betas = (fit["beta0"], fit["beta1"], fit["beta2"])

    figure, (curve_ax, residual_ax) = plt.subplots(
        2, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]})

    grid = np.linspace(0.05, max(r["tau_years"] for r in residuals) * 1.05, 300)
    curve_ax.plot(grid, ns.predict(grid, betas, fit["lambda_years"]),
                  lw=2, color="tab:blue", label="fitted Nelson-Siegel curve", zorder=1)

    # Marker area tracks the weight each quote carried: big dots are tight
    # two-way prices, small ones are wide, uninformative quotes.
    weights = np.array([r["weight"] or 1.0 for r in quotes])
    curve_ax.scatter([r["tau_years"] for r in quotes], [r["observed_yield"] for r in quotes],
                     s=18 + 55 * np.sqrt(weights / weights.max()),
                     alpha=0.65, color="tab:grey", edgecolor="none",
                     label=f"dealer quote mids, fitted ({len(quotes)})", zorder=2)
    if trades:
        curve_ax.scatter([r["tau_years"] for r in trades], [r["observed_yield"] for r in trades],
                         s=52, marker="D", color="tab:red", alpha=0.85,
                         label=f"executed trades, held out ({len(trades)})", zorder=3)

    curve_ax.set_ylabel("yield, % p.a.")
    title = (f"LKR government bond curve — {obs_date}\n"
             f"{fit['n_quotes']} bonds, fit RMSE {fit['rmse_bp']:.1f}bp, "
             f"lambda {fit['lambda_years']:.2f}y")
    if fit["n_trades"]:
        title += (f"  |  vs {fit['n_trades']} trades: bias {fit['trade_bias_bp']:+.1f}bp, "
                  f"RMSE {fit['trade_rmse_bp']:.1f}bp")
    curve_ax.set_title(title, fontsize=10)
    curve_ax.legend(loc="lower right", fontsize=8)
    curve_ax.grid(alpha=0.3)

    residual_ax.axhline(0, color="tab:blue", lw=1)
    residual_ax.scatter([r["tau_years"] for r in quotes], [r["residual_bp"] for r in quotes],
                        s=14, alpha=0.6, color="tab:grey")
    if trades:
        residual_ax.scatter([r["tau_years"] for r in trades], [r["residual_bp"] for r in trades],
                            s=40, marker="D", color="tab:red", alpha=0.85)
        residual_ax.axhline(fit["trade_bias_bp"], color="tab:red", ls="--", lw=1,
                            label=f"trade bias {fit['trade_bias_bp']:+.1f}bp")
        residual_ax.legend(loc="best", fontsize=8)
    residual_ax.set_ylabel("cheap (+) / rich (−), bp")
    residual_ax.set_xlabel("years to maturity")
    residual_ax.grid(alpha=0.3)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out_path or REPORTS_DIR / f"curve_{obs_date}.png"
    figure.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(figure)
    print(f"chart saved to {out_path}")
    return out_path
