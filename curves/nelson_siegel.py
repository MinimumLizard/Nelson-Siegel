"""The Nelson-Siegel yield curve: pure maths, no database, no I/O.

The model gives the yield of a bond with `tau` years to maturity as

    y(tau) = b0 + b1 * f1(tau) + b2 * f2(tau)

    f1 = (1 - exp(-tau/lam)) / (tau/lam)          "slope" factor
    f2 = f1 - exp(-tau/lam)                        "curvature" factor

with the usual reading: b0 is the long-run level (f1 and f2 both vanish as
tau grows), b1 the short-vs-long slope (b0 + b1 is the yield at the very
short end), b2 the hump in the middle, and lam where that hump sits.

**How it is fitted, and why that way.** For a FIXED lam the model is linear
in b0, b1, b2 — the two factors are just constructed regressors — so the
best fit is ordinary weighted least squares, which has a closed form and
cannot fail to converge. Only lam is genuinely non-linear, and it is a
single well-behaved parameter, so it is chosen by scanning a grid and
keeping the lam whose WLS fit has the lowest weighted error.

That is deliberately not a joint non-linear optimisation of all four
parameters at once: with ~45 bonds a day and a curve this shape, joint
fitting is prone to running off to silly lam values or landing in a local
minimum, and the day-to-day parameter jumps that follow would show up as
fake richness/cheapness. Scanning lam is slower in principle and instant in
practice, and it gives the same answer every time it sees the same data.

(For an R user: think `lm(y ~ f1 + f2, weights = w)` inside a loop over lam.)
"""

import numpy as np

# Where the curvature hump may sit, in years. The grid is geometric because
# lam matters far more at the short end than the long: the difference
# between 0.5 and 1.0 years reshapes the front of the curve, while 9 vs 10
# barely moves it.
LAMBDA_GRID = np.geomspace(0.3, 12.0, 80)


def factors(tau, lam):
    """The two Nelson-Siegel factor loadings at maturities `tau`."""
    tau = np.asarray(tau, dtype=float)
    scaled = tau / lam
    # exp(-x)/x is 0/0 at tau=0; the limit of f1 there is 1.
    with np.errstate(divide="ignore", invalid="ignore"):
        decay = np.exp(-scaled)
        slope = np.where(scaled > 1e-8, (1.0 - decay) / scaled, 1.0)
    return slope, slope - decay


def design_matrix(tau, lam):
    """Columns [1, f1, f2] — the regressors of the linear-in-beta form."""
    slope, curve = factors(tau, lam)
    return np.column_stack([np.ones_like(slope), slope, curve])


def _weighted_least_squares(matrix, y, weights):
    """Solve min sum(w*(y - X b)^2), returning (beta, weighted RSS)."""
    root = np.sqrt(weights)
    solution, *_ = np.linalg.lstsq(matrix * root[:, None], y * root, rcond=None)
    residuals = y - matrix @ solution
    return solution, float(np.sum(weights * residuals**2))


def fit_fixed(tau, y, lam, weights=None):
    """Fit b0,b1,b2 for a GIVEN lam. Returns (betas, fitted, weighted RSS).

    This is the workhorse: with lam fixed the model is plain weighted least
    squares, so it is exact, instant, and returns the same answer every
    time — no optimiser, nothing to converge.
    """
    tau = np.asarray(tau, dtype=float)
    y = np.asarray(y, dtype=float)
    weights = (np.ones_like(y) if weights is None
               else np.asarray(weights, dtype=float))
    if len(y) < 4:
        raise ValueError(f"need at least 4 bonds to fit a curve, got {len(y)}")
    matrix = design_matrix(tau, lam)
    betas, rss = _weighted_least_squares(matrix, y, weights)
    return betas, matrix @ betas, rss


def fit(tau, y, weights=None, lambda_grid=LAMBDA_GRID):
    """Fit the curve, choosing lam per day. Returns (betas, lam, fitted).

    NOTE: prefer fit_fixed() with a lam calibrated once over the whole
    sample. Choosing lam afresh each day fits marginally better but makes
    the three betas incomparable from one day to the next — on this data
    it pinned lam at the search ceiling on 29% of days and swung the
    "long-run level" beta0 between 2.4% and 14.6%, because at large lam
    the two factors become nearly collinear over the maturities we
    observe. The curve still passes through the points, but its shape
    BETWEEN them then jumps around, which a residual-based signal would
    read as bonds turning rich and cheap overnight. Kept for calibration
    and for diagnostics.
    """
    best = None
    for lam in lambda_grid:
        betas, fitted, rss = fit_fixed(tau, y, lam, weights)
        if best is None or rss < best[0]:
            best = (rss, betas, lam, fitted)
    _, betas, lam, fitted = best
    return betas, float(lam), fitted


def predict(tau, betas, lam):
    """Curve value at arbitrary maturities — for plotting and for pricing
    a bond that was not itself in the fit."""
    return design_matrix(tau, lam) @ np.asarray(betas, dtype=float)


def weighted_rmse_bp(y, fitted, weights=None):
    """Weighted RMS error in basis points (yields are given in percent)."""
    y, fitted = np.asarray(y, dtype=float), np.asarray(fitted, dtype=float)
    weights = (np.ones_like(y) if weights is None
               else np.asarray(weights, dtype=float))
    error = (y - fitted) * 100.0
    return float(np.sqrt(np.sum(weights * error**2) / np.sum(weights)))
