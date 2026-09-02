"""Build a self-contained dashboard page from the database.

    python -m dashboard.build            # writes docs/index.html

The page is plain HTML with an inline SVG chart and no external requests, so
it works from a file:// path, from GitHub Pages, or anywhere else it is
dropped. The daily job regenerates it, which is why nothing here takes
arguments about what to show: it always renders the most recent day.

It leads with the CORE BOOK — the current auction benchmarks that are
genuinely trading — because that is the paper a decision can actually be
executed in. Bonds that trade but are off the run come second, and the rest
of the market is fitted into the curve but not listed. See
`signals/liquidity.py` for why that ordering is not cosmetic.
"""

import argparse
import datetime as dt
import html
from pathlib import Path

from curves import nelson_siegel as ns
from dashboard import palette
from pipeline import config, db
from signals import liquidity
from signals.report import MAX_TRADEABLE_SPREAD_BP, _expected_capture

OUTPUT = Path("docs/index.html")
CHART_WIDTH, CHART_HEIGHT = 880, 400
MARGIN = {"left": 58, "right": 18, "top": 18, "bottom": 44}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def gather(conn) -> dict | None:
    row = conn.execute("SELECT MAX(obs_date) AS d FROM curve_fits").fetchone()
    if not row or not row["d"]:
        return None
    obs_date = row["d"]

    fit = dict(conn.execute("SELECT * FROM curve_fits WHERE obs_date=?", (obs_date,)).fetchone())
    residuals = [dict(r) for r in conn.execute(
        "SELECT * FROM curve_residuals WHERE obs_date=? ORDER BY tau_years", (obs_date,))]
    spreads = {r["isin"]: (r["bid_yield"] - r["offer_yield"]) * 100.0
               for r in conn.execute(
                   """SELECT isin, bid_yield, offer_yield FROM observations
                       WHERE obs_date=? AND source='pdmo_daily'
                         AND bid_yield IS NOT NULL AND offer_yield IS NOT NULL""",
                   (obs_date,))}
    # Liquidity decides who appears at all: ranking on z-score alone selects
    # for illiquid bonds, whose stale quotes jump and so score highest.
    facts = liquidity.profile(conn, obs_date)
    tradeable = {isin for isin, spread in spreads.items()
                 if spread <= MAX_TRADEABLE_SPREAD_BP
                 and liquidity.is_tradeable(facts.get(isin))}

    core_isins = {isin for isin in tradeable if liquidity.is_core(facts.get(isin))}

    # Sorted cheap-first: a positive z means the bond yields more than its own
    # recent norm, which is what "cheap" means here.
    signals = [dict(r) for r in conn.execute(
        """SELECT s.*, b.series_label, b.maturity_date FROM bond_signals s
             JOIN bonds b USING(isin) WHERE s.obs_date=? ORDER BY s.zscore DESC""",
        (obs_date,)) if r["isin"] in tradeable]
    core = [row for row in signals if row["isin"] in core_isins]
    others = [row for row in signals if row["isin"] not in core_isins]
    # A pair is only a trade if you can deal in BOTH sides, so switches are
    # restricted to two core legs rather than anything merely quoted.
    switches = [dict(r) for r in conn.execute(
        """SELECT s.*, ba.series_label AS label_a, bb.series_label AS label_b
             FROM switch_signals s
             JOIN bonds ba ON ba.isin=s.isin_a JOIN bonds bb ON bb.isin=s.isin_b
            WHERE s.obs_date=? ORDER BY ABS(s.zscore) DESC""", (obs_date,))
        if {r["isin_a"], r["isin_b"]} <= core_isins][:8]
    waiting = _waiting(conn, obs_date, facts)
    labels = {r["isin"]: r["series_label"] or r["isin"]
              for r in conn.execute("SELECT isin, series_label FROM bonds")}

    coverage = conn.execute(
        """SELECT COUNT(*) AS days, MIN(obs_date) AS first, MAX(obs_date) AS last
             FROM curve_fits""").fetchone()
    # The trade summary for a day is published after its daily report, so the
    # newest curve often has no trades yet. Fall back to the most recent day
    # that does, labelled with its date, rather than showing a blank tile.
    last_checked = conn.execute(
        """SELECT obs_date, trade_bias_bp, n_trades FROM curve_fits
            WHERE trade_bias_bp IS NOT NULL ORDER BY obs_date DESC LIMIT 1""").fetchone()

    return {
        "obs_date": obs_date, "fit": fit, "residuals": residuals,
        "spreads": spreads, "signals": signals, "switches": switches,
        "core": core, "others": others, "core_isins": core_isins,
        "waiting": waiting, "liquidity": facts, "labels": labels,
        "coverage": dict(coverage),
        "last_checked": dict(last_checked) if last_checked else None,
        "hidden": len(spreads) - len(tradeable),
    }


def _waiting(conn, obs_date, facts) -> list[dict]:
    """Tradeable bonds quoted today that have no z-score yet.

    A bond auctioned last month is the most tradeable paper on the screen and
    the least likely to have the ~30 days of residual history a z-score needs.
    Dropping it silently would hide exactly what matters most, so it is named
    with the history it has so far.
    """
    scored = {r["isin"] for r in conn.execute(
        "SELECT isin FROM bond_signals WHERE obs_date=?", (obs_date,))}
    quoted = {r["isin"] for r in conn.execute(
        """SELECT isin FROM observations WHERE obs_date=? AND source='pdmo_daily'
             AND bid_yield IS NOT NULL""", (obs_date,))}
    labels = {r["isin"]: r["series_label"]
              for r in conn.execute("SELECT isin, series_label FROM bonds")}
    out = []
    for isin, fact in sorted(facts.items(), key=lambda kv: -kv[1]["turnover_lkr"]):
        if isin not in quoted or isin in scored or not liquidity.is_tradeable(fact):
            continue
        days = conn.execute(
            """SELECT COUNT(*) c FROM curve_residuals
                WHERE isin=? AND source='quote' AND obs_date<=?""",
            (isin, obs_date)).fetchone()["c"]
        out.append({"isin": isin, "label": labels.get(isin) or isin,
                    "days": days, "facts": fact})
    return out


# ---------------------------------------------------------------------------
# The curve chart, as inline SVG
# ---------------------------------------------------------------------------

def _scales(residuals):
    taus = [r["tau_years"] for r in residuals]
    ys = [r["observed_yield"] for r in residuals]
    x_max = max(taus) * 1.06
    y_min, y_max = min(ys), max(ys)
    pad = max((y_max - y_min) * 0.12, 0.15)
    return 0.0, x_max, y_min - pad, y_max + pad


def _nice_ticks(low, high, count=5):
    span = high - low
    if span <= 0:
        return [low]
    raw = span / count
    magnitude = 10 ** int(f"{raw:e}".split("e")[1])
    step = min((m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw),
               default=magnitude)
    start = (int(low / step) + (1 if low > 0 else 0)) * step
    ticks, value = [], start
    while value <= high:
        ticks.append(round(value, 6))
        value += step
    return ticks


def curve_svg(data) -> str:
    residuals = data["residuals"]
    if not residuals:
        return "<p class='empty'>No residuals for this day.</p>"
    fit = data["fit"]
    x0, x1, y0, y1 = _scales(residuals)
    left, right = MARGIN["left"], CHART_WIDTH - MARGIN["right"]
    top, bottom = MARGIN["top"], CHART_HEIGHT - MARGIN["bottom"]

    def sx(tau):
        return left + (tau - x0) / (x1 - x0) * (right - left)

    def sy(value):
        return bottom - (value - y0) / (y1 - y0) * (bottom - top)

    parts = [f'<svg viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" role="img" '
             f'aria-label="Fitted yield curve for {data["obs_date"]}" class="curve">']

    # Gridlines and axes, recessive.
    for value in _nice_ticks(y0, y1):
        y = sy(value)
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 10}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{value:.1f}</text>')
    for tau in _nice_ticks(x0, x1, 7):
        if tau < x0:
            continue
        x = sx(tau)
        parts.append(f'<text class="tick" x="{x:.1f}" y="{bottom + 22}" '
                     f'text-anchor="middle">{tau:.0f}y</text>')
    parts.append(f'<line class="axis" x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}"/>')

    # The fitted curve itself.
    betas = (fit["beta0"], fit["beta1"], fit["beta2"])
    steps = 160
    points = []
    for index in range(steps + 1):
        tau = max(x0 + (x1 - x0) * index / steps, 0.02)
        points.append(f"{sx(tau):.1f},{sy(float(ns.predict([tau], betas, fit['lambda_years'])[0])):.1f}")
    parts.append(f'<polyline class="fitline" points="{" ".join(points)}"/>')

    # Quote marks, sized by the weight each carried in the fit. Core bonds are
    # drawn solid and the rest faded — an intensity difference, deliberately
    # not a fourth colour, since three categorical hues are already on screen.
    quotes = [r for r in residuals if r["source"] == "quote"]
    core_isins = data.get("core_isins") or set()
    labels = data.get("labels") or {}
    max_weight = max((r["weight"] or 1.0) for r in quotes) if quotes else 1.0
    for row in quotes:
        weight = row["weight"] or 1.0
        radius = 4.0 + 3.5 * (weight / max_weight) ** 0.5
        is_core = row["isin"] in core_isins
        name = labels.get(row["isin"]) or row["isin"]
        tip = (f'{name} · {row["tau_years"]:.1f}y · {row["observed_yield"]:.2f}% · '
               f'{row["residual_bp"]:+.0f}bp vs curve'
               + (" · core book" if is_core else ""))
        parts.append(f'<circle class="quote{" core" if is_core else ""}" '
                     f'cx="{sx(row["tau_years"]):.1f}" '
                     f'cy="{sy(row["observed_yield"]):.1f}" r="{radius:.1f}" '
                     f'data-tip="{html.escape(tip)}"><title>{html.escape(tip)}</title></circle>')

    # Executed trades, held out of the fit.
    for row in (r for r in residuals if r["source"] == "trade"):
        x, y = sx(row["tau_years"]), sy(row["observed_yield"])
        tip = (f'{row["isin"]} traded · {row["tau_years"]:.1f}y · '
               f'{row["observed_yield"]:.2f}% · {row["residual_bp"]:+.0f}bp vs curve')
        parts.append(f'<path class="trade" d="M {x:.1f} {y - 6:.1f} L {x + 6:.1f} {y:.1f} '
                     f'L {x:.1f} {y + 6:.1f} L {x - 6:.1f} {y:.1f} Z" '
                     f'data-tip="{html.escape(tip)}"><title>{html.escape(tip)}</title></path>')

    parts.append(f'<text class="axis-label" x="{(left + right) / 2:.0f}" '
                 f'y="{CHART_HEIGHT - 6}" text-anchor="middle">years to maturity</text>')
    parts.append(f'<text class="axis-label" transform="translate(14,{(top + bottom) / 2:.0f}) '
                 f'rotate(-90)" text-anchor="middle">yield, % p.a.</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

STYLE = palette.CSS + """
* { box-sizing: border-box; }
body { margin: 0; background: var(--plane); color: var(--ink);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 980px; margin: 0 auto; padding: 28px 20px 56px; }
h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 15px; margin: 32px 0 10px; }
.sub { color: var(--ink-2); margin: 0 0 22px; }
.card { background: var(--surface); border: 1px solid var(--border);
        border-radius: 10px; padding: 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
         gap: 10px; margin-bottom: 22px; }
.tile .label { color: var(--muted); font-size: 12px; }
.tile .value { font-size: 25px; margin-top: 2px; letter-spacing: -0.02em; }
.tile .note { color: var(--ink-2); font-size: 12px; }
.curve { width: 100%; height: auto; display: block; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.tick, .axis-label { fill: var(--muted); font-size: 11px; }
.fitline { fill: none; stroke: var(--series-1); stroke-width: 2;
           stroke-linecap: round; }
.quote { fill: var(--muted); fill-opacity: .38; stroke: var(--surface);
         stroke-width: 2; }
.quote.core { fill: var(--ink-2); fill-opacity: .95; }
.trade { fill: var(--series-2); stroke: var(--surface); stroke-width: 2; }
.legend { display: flex; gap: 18px; flex-wrap: wrap; color: var(--ink-2);
          font-size: 12px; margin-top: 10px; }
.legend i { display: inline-block; width: 11px; height: 11px; margin-right: 6px;
            vertical-align: -1px; border-radius: 50%; }
.legend .line-key { width: 16px; height: 3px; border-radius: 2px; vertical-align: 3px; }
.legend .muted-key { color: var(--muted); font-style: italic; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 720px) { .cols { grid-template-columns: 1fr; } }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th { text-align: right; font-weight: 500; color: var(--muted); font-size: 11px;
     padding: 0 0 6px 14px; border-bottom: 1px solid var(--border);
     white-space: nowrap; }
th:first-child { padding-left: 0; }
th:first-child, td:first-child { text-align: left; }
td { padding: 7px 0 7px 14px; border-bottom: 1px solid var(--border); text-align: right; }
td:first-child { padding-left: 0; }
tr:last-child td { border-bottom: none; }
.bar { position: relative; height: 8px; border-radius: 4px; background: var(--neutral);
       overflow: hidden; min-width: 60px; }
.bar span { position: absolute; top: 0; bottom: 0; border-radius: 4px; }
.cheap span { background: var(--cheap); left: 50%; }
.rich span { background: var(--rich); right: 50%; }
.bench { font-size: 10px; letter-spacing: .04em; text-transform: uppercase;
         color: var(--series-1); border: 1px solid var(--series-1);
         border-radius: 4px; padding: 0 4px; margin-left: 6px; }
.badge { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
         color: var(--ink-2); white-space: nowrap; }
.badge i { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.hot { color: var(--series-2); font-weight: 600; }
.tier { color: var(--muted); font-size: 12.5px; margin: -4px 0 10px; }
.foot { color: var(--ink-2); font-size: 12.5px; margin-top: 10px; }
.foot code { background: var(--neutral); padding: 1px 5px; border-radius: 4px; }
.empty { color: var(--muted); }
#tip { position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
       background: var(--ink); color: var(--surface); font-size: 12px;
       padding: 5px 9px; border-radius: 6px; z-index: 9; white-space: nowrap; }
"""

SCRIPT = """
const tip = document.getElementById('tip');
for (const mark of document.querySelectorAll('[data-tip]')) {
  mark.addEventListener('pointerenter', event => {
    tip.textContent = mark.dataset.tip;
    tip.style.opacity = 1;
    const box = mark.getBoundingClientRect();
    tip.style.left = Math.min(box.left, window.innerWidth - tip.offsetWidth - 12) + 'px';
    tip.style.top = (box.top - tip.offsetHeight - 8) + 'px';
  });
  mark.addEventListener('pointerleave', () => { tip.style.opacity = 0; });
}
"""


def _signal_rows(signals, spreads, cheap: bool, limit=6, facts=None) -> str:
    """One side of a tier: the cheapest, or the richest.

    Split on the SIGN of the z-score rather than by slicing `limit` rows off
    each end of the sorted list. A z-score is the gap divided by a positive
    standard deviation, so its sign is the direction — which means every row
    lands under the right heading, and a list shorter than 2*limit can no
    longer put the same bond in both tables, as end-slicing did.
    """
    side = [row for row in signals if (row["zscore"] >= 0) == cheap]
    rows = side[:limit] if cheap else list(reversed(side[-limit:]))
    widest = max((abs(r["dislocation_bp"]) for r in signals), default=1.0) or 1.0
    cells = []
    for row in rows:
        gap = row["dislocation_bp"]
        share = min(abs(gap) / widest, 1.0) * 50.0
        side = "cheap" if gap >= 0 else "rich"
        spread = spreads.get(row["isin"])
        spread_cell = f"{spread:.0f}" if spread is not None else "–"
        fact = (facts or {}).get(row["isin"], {})
        name = html.escape(row["series_label"] or row["isin"])
        if fact.get("is_benchmark"):
            name += ('<span class="bench" title="named in a recent auction '
                     'announcement">bmk</span>')
        turnover = f'{fact.get("turnover_lkr", 0) / 1e9:.0f}' if fact else "–"
        cells.append(
            f'<tr><td>{name}</td>'
            f'<td>{gap:+.1f}</td><td>{row["zscore"]:.1f}</td>'
            f'<td>{spread_cell}</td><td>{turnover}</td>'
            f'<td style="width:30%"><div class="bar {side}">'
            f'<span style="width:{share:.1f}%"></span></div></td></tr>')
    return "".join(cells)


def _core_rows(core, spreads, facts) -> str:
    """The core book in full, cheapest at the top.

    Unlike the other tables this one is never trimmed. It is only ever a
    handful of bonds, it is the book a decision is executed in, and a
    benchmark sitting in the MIDDLE of the pack is itself worth seeing.
    """
    widest = max((abs(row["dislocation_bp"]) for row in core), default=1.0) or 1.0
    out = []
    for row in core:
        gap = row["dislocation_bp"]
        share = min(abs(gap) / widest, 1.0) * 50.0
        side = "cheap" if gap >= 0 else "rich"
        fact = facts.get(row["isin"], {})
        spread = spreads.get(row["isin"])
        spread_cell = f"{spread:.0f}" if spread is not None else "–"
        since = fact.get("days_since_auction")
        if since is None:
            auction = "–"
        elif fact.get("post_auction"):
            auction = (f'<span class="hot" title="still inside the '
                       f'{liquidity.POST_AUCTION_DAYS}-day window in which freshly '
                       f'auctioned paper has sat cheap">{since}d</span>')
        else:
            auction = f'{since}d'
        cover = f'{fact["bid_to_cover"]:.1f}×' if fact.get("bid_to_cover") else "–"
        out.append(
            f'<tr><td>{html.escape(row["series_label"] or row["isin"])}</td>'
            f'<td>{gap:+.1f}</td><td>{row["zscore"]:.1f}</td>'
            f'<td>{spread_cell}</td>'
            f'<td>{fact.get("turnover_lkr", 0) / 1e9:.0f}</td>'
            f'<td>{fact.get("days_traded", 0)}</td>'
            f'<td>{auction}</td><td>{cover}</td>'
            f'<td style="width:22%"><div class="bar {side}">'
            f'<span style="width:{share:.1f}%"></span></div></td></tr>')
    return "".join(out)


def _waiting_note(waiting) -> str:
    """One line naming tradeable bonds that cannot be scored yet."""
    if not waiting:
        return ""
    items = ", ".join(
        f'<b>{html.escape(row["label"])}</b> ({row["days"]} quote days, '
        f'Rs {row["facts"]["turnover_lkr"] / 1e9:.0f}bn traded)' for row in waiting)
    return (f'<p class="foot">Trading, but not scored yet — a z-score needs about '
            f'30 days of history and these have less: {items}. They are on the '
            f'chart and in the curve; they simply have no norm to be measured '
            f'against so far.</p>')


def _switch_rows(switches, spreads) -> str:
    out = []
    for row in switches:
        if row["zscore"] >= 0:
            buy, sell = row["label_a"] or row["isin_a"], row["label_b"] or row["isin_b"]
            buy_isin, sell_isin = row["isin_a"], row["isin_b"]
        else:
            buy, sell = row["label_b"] or row["isin_b"], row["label_a"] or row["isin_a"]
            buy_isin, sell_isin = row["isin_b"], row["isin_a"]
        cost = sum(spreads.get(i, 0.0) for i in (buy_isin, sell_isin)) / 2.0
        capture = _expected_capture(row["zscore"])
        clears = capture > cost
        colour = "var(--good)" if clears else "var(--warning)"
        verdict = "clears costs" if clears else ("below costs" if capture else "weak signal")
        out.append(
            f'<tr><td>{html.escape(buy)}</td><td>{html.escape(sell)}</td>'
            f'<td>{abs(row["dislocation_bp"]):.1f}</td><td>{row["zscore"]:+.1f}</td>'
            f'<td>{cost:.0f}</td><td>{capture:.1f}</td>'
            f'<td><span class="badge"><i style="background:{colour}"></i>{verdict}</span></td></tr>')
    return "".join(out)


def render(data, fragment: bool = False) -> str:
    """The page. `fragment` omits the document wrapper for hosts that supply
    their own <html>/<head>/<body> (an Artifact publish does)."""
    fit, coverage = data["fit"], data["coverage"]
    checked = data["last_checked"]
    if fit["trade_bias_bp"] is not None:
        bias_value = f'{fit["trade_bias_bp"]:+.0f}<span style="font-size:15px">bp</span>'
        bias_note = f'{fit["n_trades"]} trades held out'
    elif checked:
        bias_value = f'{checked["trade_bias_bp"]:+.0f}<span style="font-size:15px">bp</span>'
        bias_note = f'{checked["n_trades"]} trades on {checked["obs_date"]}'
    else:
        bias_value, bias_note = "–", "no trades recorded yet"
    core = data.get("core", [])
    core_turnover = sum(data["liquidity"].get(row["isin"], {}).get("turnover_lkr", 0)
                        for row in core) / 1e9
    tiles = [
        ("core book", f'{len(core)}',
         f'benchmarks trading · Rs {core_turnover:.0f}bn in {liquidity.WINDOW_DAYS} days'),
        ("bonds on the curve", f'{fit["n_quotes"]}', "quotes fitted"),
        ("fit error", f'{fit["rmse_bp"]:.1f}<span style="font-size:15px">bp</span>', "weighted RMSE"),
        ("vs executed trades", bias_value, bias_note),
        ("history", f'{coverage["days"]}', f'days from {coverage["first"]}'),
    ]
    tile_html = "".join(
        f'<div class="card tile"><div class="label">{label}</div>'
        f'<div class="value">{value}</div><div class="note">{note}</div></div>'
        for label, value, note in tiles)

    hidden = (f'<p class="foot"><b>The wider market.</b> {data["hidden"]} further '
              f'quoted bond(s) are fitted into the curve but not listed: quoted '
              f'wider than {MAX_TRADEABLE_SPREAD_BP:.0f}bp, or traded on fewer than '
              f'{liquidity.MIN_DAYS_TRADED} of the last {liquidity.WINDOW_DAYS} days. '
              f'They stay in the fit because the curve needs the whole cross-section '
              f'to have a shape, but ranking on z-score alone surfaces exactly these '
              f'— an illiquid bond is quoted from stale marks that jump when '
              f'refreshed, so its score is largest precisely where it is least '
              f'tradeable.</p>' if data["hidden"] else "")

    head = ('<th>series</th><th>gap bp</th><th>z</th><th>b/o</th>'
            '<th title="turnover over the last 60 days">Rs bn</th><th></th>')
    has_trades = any(r["source"] == "trade" for r in data["residuals"])
    core_table = (
        f'<table><thead><tr><th>series</th><th>gap bp</th><th>z</th><th>b/o</th>'
        f'<th title="turnover over the last {liquidity.WINDOW_DAYS} days">Rs bn</th>'
        f'<th title="days traded in the last {liquidity.WINDOW_DAYS}">days</th>'
        f'<th>auction</th><th title="bids over amount offered">cover</th><th></th>'
        f'</tr></thead><tbody>'
        f'{_core_rows(core, data["spreads"], data["liquidity"])}</tbody></table>'
        if core else
        f'<p class="empty">No current benchmark cleared the trading floor of '
        f'{liquidity.BENCHMARK_MIN_DAYS} days in the last {liquidity.WINDOW_DAYS} '
        f'today.</p>')
    trade_key = ('<span><i style="background:var(--series-2)"></i>executed trades, '
                 'held out of the fit</span>' if has_trades else
                 '<span class="muted-key">no executed trades published for this day yet</span>')
    body = f"""<div id="tip"></div>
<div class="wrap">
  <h1>LKR government bond relative value</h1>
  <p class="sub">{data["obs_date"]} · rebuilt automatically each day from
     Sri Lanka PDMO published data</p>

  <div class="tiles">{tile_html}</div>

  <div class="card">
    {curve_svg(data)}
    <div class="legend">
      <span><i class="line-key" style="background:var(--series-1)"></i>fitted curve</span>
      <span><i style="background:var(--ink-2)"></i>core book (size = weight in the fit)</span>
      <span><i style="background:var(--muted);opacity:.38"></i>the rest of the market, also fitted</span>
      {trade_key}
    </div>
  </div>

  <h2>Core book — auction benchmarks that are actually trading</h2>
  <p class="tier">Cheapest at the top, richest at the bottom. This is the paper
     the PDMO is issuing now and dealers make real prices in, so it is where a
     decision can be executed in size. Shown in full rather than trimmed to the
     extremes: a benchmark sitting mid-pack is information too.</p>
  <div class="card">{core_table}</div>
  <p class="foot"><b>Rs bn</b> and <b>days</b> are turnover and days traded over
     the last {liquidity.WINDOW_DAYS} · <b>auction</b> is days since this bond
     was last sold, <span class="hot">highlighted</span> inside the
     {liquidity.POST_AUCTION_DAYS}-day window in which freshly auctioned paper
     has sat about 6bp cheap to its own norm · <b>cover</b> is bids over the
     amount offered at that auction, so how much demand the last supply met.</p>
  {_waiting_note(data.get("waiting", []))}

  <h2>Also trading — liquid, but not on the run</h2>
  <div class="cols">
    <div>
      <h2>Cheap — yields above its own norm</h2>
      <div class="card"><table><thead><tr>{head}</tr></thead>
        <tbody>{_signal_rows(data.get("others", data["signals"]), data["spreads"],
                             cheap=True, facts=data["liquidity"])}</tbody></table></div>
    </div>
    <div>
      <h2>Rich — yields below its own norm</h2>
      <div class="card"><table><thead><tr>{head}</tr></thead>
        <tbody>{_signal_rows(data.get("others", data["signals"]), data["spreads"],
                             cheap=False, facts=data["liquidity"])}</tbody></table></div>
    </div>
  </div>
  {hidden}

  <h2>Switch candidates — both legs in the core book</h2>
  <div class="card"><table><thead><tr>
    <th>buy</th><th>sell</th><th>gap bp</th><th>z</th><th>cost bp</th>
    <th>expected bp</th><th>verdict</th></tr></thead>
    <tbody>{_switch_rows(data["switches"], data["spreads"])}</tbody></table></div>
  <p class="foot"><b>gap</b> is distance from the pair's own recent norm ·
     <b>z</b> is that in its own standard deviations · <b>cost</b> is half the
     bid-offer on each leg · <b>expected</b> is what a signal this size has
     historically recovered within ten days. Most candidates do not clear their
     own costs as a standalone round trip — the honest use is choosing between
     trades you were doing anyway.</p>

  <p class="foot">A bond's raw distance from the curve is not a signal: some
     bonds sit permanently cheap. Each is scored against <b>its own</b> trailing
     60-day window, which excludes the day being scored. Measured over this
     sample the residuals mean-revert with a half-life near 11 days; run
     <code>python -m signals.validate</code> for the current numbers. One
     9-month sample in one regime — evidence the mechanism works, not a
     forecast of what it pays.</p>
</div>"""

    title = "LKR Bond Relative Value"
    if fragment:
        return (f"<title>{title}</title>\n<style>{STYLE}</style>\n"
                f"{body}\n<script>{SCRIPT}</script>")
    return (f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{title} — {data['obs_date']}</title>\n"
            f"<style>{STYLE}</style></head><body>\n{body}\n"
            f"<script>{SCRIPT}</script>\n</body></html>")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUTPUT), help="where to write the page")
    parser.add_argument("--fragment", action="store_true",
                        help="omit the <html>/<head>/<body> wrapper, for hosts "
                             "that supply their own document shell")
    args = parser.parse_args()

    data = gather(db.connect())
    if data is None:
        raise SystemExit("no fitted curves yet — run: python -m curves.fit")
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data, fragment=args.fragment), encoding="utf-8")
    print(f"dashboard written to {path} ({path.stat().st_size // 1024}kB, "
          f"{data['obs_date']})")


if __name__ == "__main__":
    main()
