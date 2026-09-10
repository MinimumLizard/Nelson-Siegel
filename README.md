# Nelson-Siegel — Sri Lanka government bond relative value

Ingests Sri Lanka PDMO secondary-market data — Treasury bond two-way quotes,
per-ISIN traded volumes, per-ISIN executed trades and primary auction
results — into a SQLite database (`pipeline/`), then fits a yield curve to
every day and measures how far each bond sits from it (`curves/`), and
ranks rich/cheap and switch candidates from those residuals (`signals/`).

**All three stages are complete** — data layer, curves, signals — and the
whole chain runs itself daily; see "The data updates itself" below.

Three report families are ingested (details and quirks in
`docs/DATA_NOTES.md`):

| report | format | what it carries |
|---|---|---|
| Daily Summary Report | legacy Excel `.xls` (despite the URL) | per-bond PD two-way quotes: avg bid/offer price + yield |
| Outright Transactions Volumes | legacy Excel `.xls` | per-ISIN outright traded volume, Rs. mn |
| Secondary Market Trade Summary | PDF | per-ISIN executed trades: OHLC + wavg yield, volume, trade count |
| Treasury Bond Auction press releases | PDF | auction ISIN, series label, weighted average yield, amounts offered/bid/accepted |
| Treasury Bond issuance announcements | PDF | ISIN, series label, **date of issue, coupon payment dates, accrued interest**, amount offered — and which bonds are currently being auctioned |

The archive reaches back to **1 Dec 2025** (the site publishes nothing older
under these sections). A full backfill on 2026-08-31 ingested **542 files**
with zero parse failures: 49 bonds, 8,069 daily observations and 2,727
executed-trade rows spanning 2025-12-01 to 2026-08-31.

### One known gap

The quote sheet identifies bonds only by a series label ("10.00%2030A"),
never by ISIN. The auction documents print that label beside its ISIN, so
quotes are resolved by label where one covers the bond and by maturity date
otherwise. The series letter is not printed consistently between sources —
an announcement said "11.20%2033" for the bond the quote sheet calls
"11.20%2033A" — so a letter-insensitive match is tried before falling back
to maturity, used only where it resolves to exactly one bond.

The rest cannot be identified from any published source. Their ISINs cannot
be synthesised either: the quote sheet's tenor column agrees with the tenor
encoded in real ISINs only 33 times out of 44, so a quarter of synthesised
ISINs would be wrong, and a wrong ISIN silently attributes one bond's
history to another. The pipeline refuses to guess; each file's `parse_note`
records the split so the gap is visible rather than hidden.

45 bonds spanning roughly 1 to 13 years is ample for curve fitting, so this
limits breadth, not curve quality.

## The data updates itself

A scheduled GitHub Action (`.github/workflows/daily-update.yml`) runs every
day at 20:00 UTC on GitHub's own servers: it fetches whatever the PDMO has
published since the last run, parses it, refreshes the watched bonds' charts
and commits the result back to this repository. Nothing needs to be running
on your machine.

Each run also leaves `data/reports/signals.txt` — the day's core book as
plain text — so the current state can be read straight from the repository
without running anything.

That is why `data/sgcp.sqlite` and `data/reports/*` are committed here
(unusual for a database, deliberate in this case): the Action's runners are
wiped after every job, so the repository itself is where the accumulated
data lives. Cloning gives you current data immediately — the setup below is
only needed to run the pipeline yourself or to extend it.

A day with nothing new (weekend, public holiday) simply makes no commit. To
run it on demand, open the repository's **Actions** tab, pick **Daily PDMO
update**, and press **Run workflow**.

## The dashboard page

`python -m dashboard.build` writes **`docs/index.html`**: a self-contained
page — inline SVG chart, no external requests — showing the day's fitted
curve with the core book drawn solid against the rest of the market, then
the core book in full with its auction cycle, the off-the-run bonds that
still trade, and the switch candidates with their costs. The daily Action
rebuilds and commits it, so it stays current on its own.

It is published as a web page at

**<https://minimumlizard.github.io/Nelson-Siegel/>**

and refreshes every day. (GitHub Pages paths are case-sensitive, so the
capitals matter.) It is served by GitHub Pages from **Settings → Pages →
Source: Deploy from a branch → Branch: `main`, folder: `/docs`**; the file
also opens fine straight from disk in any browser.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Commands

```bash
python -m pipeline.backfill                    # download + parse the whole archive (~25 min first run)
python -m pipeline.update                      # fetch only what's new (daily use)
python -m pipeline.report --isin LKB00934F154  # one bond's yield/volume history + chart

python -m curves.fit                           # fit any day that has no curve yet
python -m curves.fit --date 2026-08-28 --plot  # one day, with a chart
python -m curves.fit --calibrate               # re-choose lambda, then refit all

python -m signals.run                          # rebuild rich/cheap + switch signals
python -m signals.report                       # today's ranked candidates vs costs
python -m signals.validate                     # does the signal predict reversion?

pytest                                         # offline: runs against committed fixtures

# diagnostics used to design the parsers; still handy when the site changes
python -m pipeline.inspect_samples --list 2026
python -m pipeline.inspect_samples --list-extra bond_auction_2026
python -m pipeline.inspect_samples --download <url>...
python -m pipeline.inspect_samples --dump data/samples/*
```

## Data flow and layout

```
treasury.gov.lk index pages ──> data/raw/YYYY/<uuid>.{xls,pdf}   (immutable cache)
                                      │
                                      ▼  xlrd / pdfplumber parsing (repeatable)
                                data/sgcp.sqlite
                                      │
                                      ▼  curves/ (reads only the database)
                     one Nelson-Siegel fit per day + residuals
                                      │
                                      ▼  signals/ (reads only the database)
                       rich/cheap z-scores + switch candidates
```

* Every file is cached on disk before parsing; parsing is always repeatable
  from the cache and never mutates raw data.
* Downloads are sequential, ~1.5 s apart, with retries + exponential backoff
  and an honest User-Agent (`pipeline/fetch.py`).
* Re-running backfill is idempotent: cached files are not re-downloaded and
  parses overwrite themselves cleanly.
* A file that cannot be parsed is recorded as `parse_status='failed'` (with
  a one-line reason) in the `files` table and never stops a run.

## Database schema

Conventions: dates are ISO-8601 strings, yields are **percent per annum**
(9.32 means 9.32%), prices are per 100 face, LKR amounts are **integer
rupees**.

* **bonds** — one row per instrument ever seen: `isin` (PK), `coupon_pct`,
  `maturity_date`, `tenor_years` (decoded from the ISIN), `first_seen_date`,
  `notes` (holds the full step-coupon label, e.g. `12%9%2027A`, for the
  2023-restructuring bonds).
* **observations** — one row per bond/date/source: `obs_date`, `isin`,
  `source` ('pdmo_daily' | 'auction' | 'fct_quote'), `bid_yield`,
  `offer_yield`, `mid_yield`, `bid_price`, `offer_price`, `volume_lkr`,
  `executable` (0/1, default 0 — PD averages are indicative), `raw_ref`.
  For `pdmo_daily`, quotes and volume arrive in different files and merge
  into the same row via partial upserts.
* **trade_summary** — executed trades (bills and bonds): `obs_date`, `isin`,
  `security_type`, `open/high/low/close/wavg_yield`, `volume_lkr`,
  `n_trades`, `raw_ref`. Kept separate from `observations` because its
  OHLC shape fits no other source.
* **fills** — own executions, loaded manually later: `fill_date`, `isin`,
  `side`, `yield`, `clean_price`, `dirty_price`, `face_lkr`, `counterparty`,
  `deal_ref`.
* **auctions** — primary auction / issuance-window results: `auction_date`,
  `isin`, `kind`, `settlement_date`, `way_pct`, `offered_lkr`, `bids_lkr`
  (bids ÷ offered = bid-to-cover), `accepted_lkr`.
* **curve_fits** — one fitted curve per day: `beta0` (long-run level),
  `beta1` (slope; `beta0+beta1` is the short end), `beta2` (curvature),
  `lambda_years`, `n_quotes`, `rmse_bp`, and the daily honesty check
  against bonds that traded: `n_trades`, `trade_rmse_bp`, `trade_bias_bp`.
* **curve_residuals** — how far each bond sat from the curve:
  `obs_date`, `isin`, `source` ('quote' = in the fit, 'trade' = held out),
  `tau_years`, `observed_yield`, `fitted_yield`, `residual_bp`, `weight`.
  **Sign convention: positive means the bond yields more than the curve
  says it should — i.e. cheap.** This table is what the signals stage will
  consume.
* **files** — download/parse provenance: `url` (PK), `sha256`, `file_type`,
  `report_date`, `posted_date`, `downloaded_at`, `parse_status`,
  `parse_note`.

### Which date is which?

A report published Monday 31.08 carries quotes *for* 31.08 but transaction
data *for* Friday 28.08; the volumes file listed on the same index row is
also for 28.08. The pipeline therefore keys quotes by the report's own
`REPORTING DATE` and volumes by a date **derived from the data**
(`maturity − remaining_years × 365`, exact by construction), because the
files' free-text titles contain typos and mixed date orders. "Amended"
reports are complete replacements: files are ingested oldest-posted first,
so the amendment simply overwrites.

## The curve

One Nelson-Siegel curve is fitted per day, on the dealers' quote **mids**,
each weighted by **1/spread²** — the bid-offer spread is the dealers' own
statement of how sure they are, and the median spread is about 16bp while
the tail reaches several hundred. Step-coupon restructuring bonds are
excluded: their quotes look administered, and a stepped cash flow is not
comparable with a fixed-coupon bond on one yield axis.

**Executed trades are deliberately held out of the fit.** Quotes cover
44–46 bonds every business day spanning about 1 to 13 years; trades cover
around 11 a day (as few as 2) and typically reach neither the short end
nor past 10 years, so a trade-fitted curve would extrapolate at both ends
and lurch as the traded set changed — which a residual signal would read
as bonds turning rich and cheap overnight. Trades are instead compared
against the finished curve, giving an out-of-sample error (median 20.5bp)
and a measured quote-to-trade gap each day — see "Where trades actually
print" below, because that gap turns out to move far more than it looks.

**Lambda is calibrated once over the whole sample (2.82 years) and then
held fixed**, rather than refitted daily. Refitting it daily fits 0.2bp
better and costs far more than that: it pinned lambda at the top of the
search range on 29% of days, where the slope and curvature factors go
nearly collinear over the maturities actually observed, swinging the
"long-run level" beta0 between 2.4% and 14.6% and moving it by up to
6.9pp overnight. Held fixed, beta0 stays within 11.9–13.7% and moves at
most 0.5pp a day, so the three parameters mean the same thing on every
date — which is what a residual-based signal needs.

Typical fit quality: **10.8bp** weighted RMSE across 40–42 bonds a day.

## The signals

A bond's raw distance from the curve is not a signal. Most of that
variation is cross-sectional — some bonds simply sit persistently cheap and
would flag every single day. Across bonds the residual spread is 41.6bp,
while a typical bond's own residual moves with a standard deviation of just
7.5bp. So each bond is scored against **its own** trailing 60-day window,
which is the question a switch trade actually asks.

**The window excludes the day being scored**, so no z-score has seen the
value it scores and the stored history stays usable as a backtest.

Switch candidates apply the same idea to a pair of bonds: the spread
between their residuals, z-scored against its own history. Positive means
the first leg has become unusually cheap against the second. For ordinary
paper a pair has to mature within two years, otherwise the "switch" is
really a bet on the shape of the whole curve — but bonds that have been
auctioned pair with each other across the entire curve, because that is how
a benchmark book is actually traded.

### Three tiers: the core book, also trading, the wider market

Ranking on z-score alone **selects for the wrong bonds**, and this was a real
defect until it was measured. An illiquid bond is quoted from stale marks
that jump when they are finally refreshed, so its residual moves in steps
and its z-score is large; a liquid benchmark is quoted continuously and
barely moves, so it never reaches the top. Before the fix the cheap list was
headed by bonds that had traded on 1 to 5 of the previous 60 days, while the
single most-traded bond in the market — Rs 80bn over 60 days — did not appear
at all.

Bonds are therefore sorted into three tiers (`signals/liquidity.py`), and
the report and the dashboard both lead with the first:

| tier | test | what it is |
|---|---|---|
| **core** | auctioned in the last 120 days **and** traded on ≥ 8 of the last 60 | the current benchmarks — the paper the PDMO is issuing now and dealers make real prices in |
| **active** | traded on ≥ 10 of the last 60 days | off the run, but liquid enough to act on |
| **wider** | everything else quoted | fitted into the curve, not listed as a signal |

Note the asymmetry: the wider market stays **in the curve** — dropping it
would leave too few points to define the shape — while being demoted in the
**signals**. The broad universe is the measuring stick; the core book is
what you trade. Switch candidates require **two core legs**, since a pair is
only a trade if you can deal in both sides.

The core book is printed in full rather than trimmed to its cheap and rich
extremes: it is only ever a handful of bonds, and a benchmark sitting
mid-pack is information too. Every row carries turnover, days traded, days
since the bond was last auctioned and the bid-to-cover it met there.

Tradeable bonds that do not yet have 30 days of residual history — which is
exactly what a freshly auctioned benchmark looks like — are named separately
with the history they do have, rather than silently dropped.

### The auction cycle

Testing the 15 auctions in this data for the classic pre-auction concession
found the **opposite** pattern. Bonds do not cheapen going in (+0.5bp on
average over the ten days before, which is nothing); they cheapen **after**
and stay cheap — about **+5.9bp** versus their own norm over the following
fortnight, fading to +2.8bp by 15–30 days as the new supply is distributed.
`python -m signals.validate` prints this table, so it can be re-checked as
history grows.

So the reports mark `post_auction` for 14 days after a sale and show
`days_since_auction` on every core row, which lets a reading be judged
against where the bond sits in its cycle: a benchmark showing +5bp cheap a
week after its auction is closer to normal than the number alone suggests.

On 44 events across 15 auction dates this is **suggestive, not
established**, and 6bp sits below a typical 16bp bid-offer — it is
context for a decision, not a trade on its own.

### Where trades actually print

Nobody deals at a quote mid, and the gap between the screen and an executed
trade is not a constant to subtract. Over 2,077 bond-days that both quoted
and traded, the monthly median gap ran:

| | | | |
|---|---|---|---|
| 2026-02 | +0.8bp | 2026-06 | **+51.9bp** |
| 2026-03 | +15.2bp | 2026-08 | −3.6bp |
| 2026-05 | +20.6bp | 2026-09 | −6.6bp |

Early June printed +100 to +134bp market-wide, across 25 of the 31 bonds
that traded, decaying over a fortnight — dealer screens lagging a fast move.
Right now the gap is **negative**, so the whole-sample average of about
+16bp is not merely imprecise, it has the wrong sign for this regime.

The estimate is therefore taken over a **20-day trailing window**, chosen by
out-of-sample error on predicting each day's realised gap:

| window | mean abs error |
|---|---|
| all history | 18.1bp |
| 120 days | 18.0bp |
| 60 days | 16.5bp |
| **20 days** | **13.0bp** |

It is **not** bucketed by tenor, though the whole-sample split looks
convincing (+17.6bp at 0−2y against +8.3bp at 7−10y). Bucketing loses out of
sample: predicting a bucket's next-quarter gap from its own past gives
10.9bp of mean absolute error against 9.2bp for the blended past. The
market-wide level swings by 50bp while the tenor spread is worth about 9bp,
so slicing adds more estimation noise than the structure removes — and the
front-end-is-wider ordering itself reversed in one of the four quarters.
`python -m signals.validate` prints both tables.

The gap does **not** bias a z-score: each bond is measured against its own
trailing mean, so any offset that is stable for that bond divides out. What
it does change is the yield you actually buy at, which is where carry
starts.

## Carry

The relative-value signal asks whether a bond is mispriced against the
curve. For a book that buys bonds with borrowed money and holds them, the
first question is instead what the position earns just by existing:

* **carry** — the bond's yield less the cost of funding it
* **rolldown** — the price gain as the bond ages down a sloped curve, with
  the curve itself unchanged

Both are known at the outset, unlike a reversion that may not arrive. On the
core book they range over 147bp while the whole spread of relative-value
dislocations is 19bp, so on that comparison carry does about eight times the
work.

**But ranking on carry is a trap, and the number says so.** Across the same
book, carry plus rolldown correlates **+0.995 with duration** — "buy the
most carry" means "buy the longest bond", which is a view on the curve
rather than a choice between bonds. Divide by duration and the ordering
reverses (−0.98). The report therefore leads with **carry per year of
duration**, and on 2026-09-02 that column reads 46 to 50bp across all nine
core bonds: the curve is charging about the same for every year of risk on
it, and duration-adjusted carry does not discriminate between them today.

Funding is the **12-month Treasury bill plus 5bp**, taken from executed bill
trades in this same data (`security_type='Tbill'`, ISINs decoded by
`pipeline/isin.py`). Bills near the one-year point print on about half of
all days and usually one at a time, so prints are volume-weighted over a
short trailing window rather than taken from the latest day alone. Nothing
uses a rate from after the day being scored.

**Tax is not modelled.** Coupon income and price accretion are usually taxed
differently, and the split does vary here — the 2037 at 92.8 earns 97% of
its return as coupon, the 11.00%2030A at 101.5 earns 104% and gives some
back in price. But the whole core book sits within about 8 points of par, so
a 10-point wedge between the two rates moves the ranking by roughly 3bp a
year, which is inside the noise on everything else. It is left out rather
than guessed at.

### Does it work?

`python -m signals.validate` measures it. On this sample the residuals
mean-revert with an **AR(1) of 0.94, a half-life of about 11 days**, and
the relationship is monotone across every z bucket at 5, 10 and 20 days:

| signal | 10-day capture | right direction |
|---|---|---|
| single bond, \|z\| > 2 | 4.1bp | 65% |
| switch pair, \|z\| > 2 | 5.8bp | 66% |
| switch pair, \|z\| > 3 | 11.7bp | — |

**Read those against costs before trading.** The median bid-offer is 16bp,
so crossing both legs of a switch costs more than a \|z\|>2 signal has
historically returned. `signals.report` therefore prints the cost beside
every candidate and labels it "below costs" when it does not clear —
which, on a typical day, most do not. The realistic use is ranking and
timing trades you were going to do anyway, not a standalone strategy.

Quotes wider than 50bp are hidden from the report entirely: they are not
dealable prices, and they would otherwise top the cheap list every day.

Caveat worth repeating: this is one 9-month sample in one regime, measured
in-sample. It is evidence the mechanism works, not an estimate of what it
would pay.

## Out of scope

Any UI. `pipeline/indicators.py` remains a stub.
