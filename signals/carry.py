"""What a bond earns while you hold it, funded.

The relative-value signal answers "is this bond mispriced against the
curve". For a book that buys bonds with borrowed money and holds them, that
is the second question. The first is what the position earns per day just by
existing, which is:

    carry     = the bond's yield minus the cost of funding it
    rolldown  = the price gain as the bond ages down a sloped curve, with
                the curve itself unchanged

Both are known at the outset, unlike a mean reversion that may or may not
arrive. On the core book on 2026-09-02, carry plus rolldown ranged over
147bp across the nine bonds while the whole spread of relative-value
dislocations was 19bp. On that comparison carry is doing roughly eight
times the work.

**But ranking on carry alone is a trap, and the number says so.** Across
that same book, carry plus rolldown correlates +0.995 with duration. It is
almost entirely a duration bet: "buy the most carry" means "buy the longest
bond", which is a view on the curve, not a selection between bonds. Divide
by duration and the ordering REVERSES (correlation -0.98) — the 2030s earn
about 79bp per year of duration against 61bp for the 2037. Per unit of the
risk taken, the front of the core book is the better carry, and the long
end's extra 147bp is payment for duration rather than value.

Duration-adjusted, the spread across the book is 19bp — the same size as
the relative-value spread. So the honest summary is not "carry dominates":
it is that carry dominates only if you are willing to own the duration, and
once you adjust for that the two are comparable and belong side by side.

**Funding** is the 12-month Treasury bill, taken from executed bill trades
in this same data, plus a spread for the dealer's own margin. The bill
prints on about half of all days, so the most recent print on or before the
day being scored is used and its staleness is reported. Nothing here uses a
rate from after the day being scored.

**Tax is not modelled.** Coupon income and price accretion are taxed
differently in most regimes, and the split varies across this book — the
2037 at 92.8 earns 97% of its return as coupon, the 11.00%2030A at 101.5
earns 104% as coupon and gives some back in price. But the whole core book
sits within about 8 points of par, so a 10-point wedge between the two tax
rates moves the ranking by roughly 3bp a year. That is inside the noise on
everything else here, so it is left out rather than guessed at.
"""

import datetime as dt

from pipeline import isin as isin_module

FUNDING_SPREAD_BP = 5.0    # dealer margin over the bill, per the desk
BILL_MIN_DAYS = 240        # a "12-month" bill: anything with 8 to 12.5 months left
BILL_MAX_DAYS = 380
BILL_WINDOW_DAYS = 5       # trailing days of bill prints to average over
BILL_LOOKBACK_DAYS = 30    # how far back to accept a stale bill print at all
HORIZON_YEARS = 0.25       # rolldown horizon: one quarter, annualised
COUPON_FREQUENCY = 2       # Sri Lankan government bonds pay semiannually


def funding_rate(conn, obs_date: str) -> dict | None:
    """The 12-month bill plus the desk's spread, as of `obs_date`.

    Bills near the one-year point trade on about half of all days, and on
    most of those exactly ONE such bill prints — so taking the latest day
    alone would let a single trade set the funding rate for the whole book.
    Instead every qualifying print in a short trailing window is
    volume-weighted together. Nothing after `obs_date` is used.
    """
    today = dt.date.fromisoformat(obs_date)
    earliest = (today - dt.timedelta(days=BILL_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT obs_date, isin, wavg_yield, volume_lkr FROM trade_summary
            WHERE security_type = 'Tbill' AND obs_date <= ? AND obs_date > ?
              AND wavg_yield IS NOT NULL AND volume_lkr > 0
            ORDER BY obs_date DESC""", (obs_date, earliest)).fetchall()

    by_day: dict[str, list] = {}
    for row in rows:
        decoded = isin_module.decode_bill(row["isin"])
        if not decoded:
            continue
        remaining = (decoded[1] - dt.date.fromisoformat(row["obs_date"])).days
        if BILL_MIN_DAYS <= remaining <= BILL_MAX_DAYS:
            by_day.setdefault(row["obs_date"], []).append(row)
    if not by_day:
        return None

    # Everything within BILL_WINDOW_DAYS of the most recent qualifying print.
    latest = max(by_day)
    cutoff = (dt.date.fromisoformat(latest)
              - dt.timedelta(days=BILL_WINDOW_DAYS)).isoformat()
    sample = [row for day, rows in by_day.items() if day > cutoff for row in rows]
    weight = sum(row["volume_lkr"] for row in sample)
    bill = sum(row["wavg_yield"] * row["volume_lkr"] for row in sample) / weight
    return {"bill_pct": bill, "rate_pct": bill + FUNDING_SPREAD_BP / 100.0,
            "spread_bp": FUNDING_SPREAD_BP, "as_of": latest,
            "stale_days": (today - dt.date.fromisoformat(latest)).days,
            "n_bills": len(sample), "window_days": BILL_WINDOW_DAYS}


def price_and_duration(yield_pct: float, coupon_pct: float,
                       tau_years: float) -> tuple[float, float]:
    """(clean price per 100, modified duration) from a level yield.

    A flat-yield discounting of the bond's own cash flows. Good to a few
    hundredths of a year of duration, which is far finer than anything it
    is compared against here.
    """
    periods = max(int(round(tau_years * COUPON_FREQUENCY)), 1)
    rate = yield_pct / 100.0 / COUPON_FREQUENCY
    price = weighted = 0.0
    for index in range(1, periods + 1):
        flow = coupon_pct / COUPON_FREQUENCY + (100.0 if index == periods else 0.0)
        present = flow / (1.0 + rate) ** index
        price += present
        weighted += present * index / COUPON_FREQUENCY
    return price, weighted / price / (1.0 + rate)


def profile(conn, obs_date: str, entry_gap_bp: float = 0.0) -> dict:
    """{isin: carry, rolldown and duration facts} as of `obs_date`.

    `entry_gap_bp` shifts every bond's assumed entry yield by the same
    amount — the measured quote-to-trade gap, if the caller supplies it.
    Being uniform it moves the LEVEL of carry and not the ranking, which is
    exactly why it is a single number rather than a per-bond adjustment.
    """
    from curves import nelson_siegel

    fit = conn.execute("SELECT * FROM curve_fits WHERE obs_date = ?", (obs_date,)).fetchone()
    funding = funding_rate(conn, obs_date)
    if not fit or not funding:
        return {}
    betas = (fit["beta0"], fit["beta1"], fit["beta2"])
    lam = fit["lambda_years"]

    out = {}
    for row in conn.execute(
            """SELECT r.isin, r.tau_years, r.observed_yield, b.coupon_pct
                 FROM curve_residuals r JOIN bonds b USING(isin)
                WHERE r.obs_date = ? AND r.source = 'quote'
                  AND b.coupon_pct IS NOT NULL""", (obs_date,)):
        tau = row["tau_years"]
        entry = row["observed_yield"] + entry_gap_bp / 100.0
        price, duration = price_and_duration(entry, row["coupon_pct"], tau)

        # Rolldown: where this bond's yield sits a quarter from now if the
        # curve does not move, times duration, annualised.
        aged = max(tau - HORIZON_YEARS, 0.02)
        drop = float(nelson_siegel.predict([tau], betas, lam)[0]) - \
            float(nelson_siegel.predict([aged], betas, lam)[0])
        roll_bp = duration * drop * 100.0 / HORIZON_YEARS

        carry_bp = (entry - funding["rate_pct"]) * 100.0
        total = carry_bp + roll_bp
        out[row["isin"]] = {
            "entry_yield": entry, "price": price, "duration": duration,
            "carry_bp": carry_bp, "roll_bp": roll_bp, "total_bp": total,
            "per_duration_bp": total / duration if duration > 0 else None,
            "funding": funding}
    return out
