# PDMO data-format notes (from sample inspection, 2026-08-31)

Everything below was established by actually fetching the index pages and a
dozen sample files (Dec 2025, Jan/Apr/Aug 2026, including the one amended
report). The parser and schema are designed against these facts, so if the
site changes format, start by re-running `pipeline.inspect_samples` and
updating this file.

## The headline surprise: the "PDFs" are Excel files

Every "Daily Summary Report" and "Outright Treasury Bond Transactions
Volumes" link serves a **legacy Excel .xls workbook** (OLE2 compound
document), not a PDF:

* the server sends `content-type: application/vnd.ms-excel`;
* `content-disposition` carries a filename like
  `Daily Summary Report  31  August 2026.xls` — note the report date in it;
* the first bytes are the OLE2 magic `D0 CF 11 E0`, which is how the
  pipeline sniffs the type (never trust the URL or extension).

Only the *Secondary Market Trade Summary* files are real PDFs
(`%PDF` magic, one page, produced from Excel via print-to-PDF).

Consequence: the daily pair is parsed with **xlrd** (much more reliable than
PDF table extraction — cells arrive typed), and pdfplumber is only needed if
we ingest the trade summaries.

## Archive coverage

* Daily reports: `web/report-daily-report/section/2025` only reaches back to
  **28.11.2025** (the section navigation offers just 2025 and 2026), so the
  full archive is ~Dec 2025 → today: roughly 180 business days, two files
  per day.
* Trade summaries: `web/reports-secondary-market-trade-summary/section/2026`
  has ~180 links; a 2025 section exists as well.

## Daily-report index pages (per year)

Each business day is one `<p>` row inside `.page-template--body__content`:

    > 31.08.2026   [a: Outright Treasury Bond Transactions Volumes as at 28.08.2026]
                   [a: Daily Summary Report]

* The leading `> DD.MM.YYYY` is the **posting date** (plain text, not a link).
* The volumes link text names its own date ("as at DD.MM.YYYY") — always
  D.M.Y on the index, and it lags the posting date by one business day.
* The daily-summary link text carries **no date**; its date comes from
  inside the file.

Observed messiness the scraper must survive (all real examples):

* anchor text split mid-word across two anchors with the same href:
  `"D"` + `"aily Summary Report"`; also `"Daily Summary Repor"` with the
  trailing `t` outside the anchor;
* empty-text anchors duplicating a labelled link's href;
* **empty-text anchors pointing at a different row's file** (copy-paste
  slips) — e.g. the 19.02.2026 row contains a stray link to the 17.02
  volumes file. Rule: only labelled anchors identify a file; global
  dedup by URL; never attribute an unlabelled href to a row;
* stray spaces inside dates: `> 16 .04.2026`, `as at 26.01.202 6` — collapse
  whitespace before matching dates;
* one row (16.04.2026) says **"Amended Daily Summary Report"** — it is a
  full replacement report (see below), not a diff.

## Daily Summary Report (.xls) layout — stable Dec 2025 → Aug 2026

Five sheets, identical structure in every sample:

* **Main Menu** — three labelled cells (Excel serial dates):
  `DATE OF TRADING` (the business day the transactions cover),
  `REPORTING DATE` (publication date, = the index row's posting date),
  `UPDATE` (save timestamp). These are the authoritative dates.
* **QuotesTBond** — the table we want. Header at row 6, data rows 8–113
  (~94 bonds, blank separator rows in between), footer text after.
  Columns: series label (`11.25%2026A` — coupon% + maturity year + series
  letter), original tenor in years, maturity date (Excel serial), days to
  maturity, average buying price, buying yield, average selling price,
  selling yield, spread. **Yields are fractions** (0.0932 = 9.32%),
  prices per 100 face. No ISIN column (see ISIN notes below).
  Matured bonds linger with zeros in every numeric column — skip rows
  whose prices/yields are all zero.
* **Quotes TBills** — bill curve by tenor bucket (1-7 days … 12 months),
  same price/yield/spread shape. No ISINs, buckets only.
* **NewFormat** / **Summary Statistics** — aggregates: last-auction rates,
  bucketed bid/offer averages, repo rates, and market totals
  ("Secondary Market Transactions (Rs. Million)": outright purchases/sales
  and repo volumes for bills vs bonds).

The one **amended** sample (posted 16.04.2026, `UPDATE` 17.04) is simply the
next day's full report: trading date 15.04, reporting date 16.04. Dedup rule:
key on the dates read from inside the file; when two files claim the same
dates, the later `UPDATE` wins.

## Volumes report (.xls) layout

One sheet, `Outright MO EVENING REPORT`:

    r1: Outright Treasury bond transaction volumes on <DATE>
    r2: ISIN | Maturity Date | Remaining years | Sum of Amount (Mn)
    ...one row per ISIN traded...
    last: Total | <sum>

Per-ISIN traded volume in **Rs. millions** (fractional — 232.825 means
Rs 232,825,000). Quirks observed:

* header drift: `Sum of Amount (Mn)` vs `Sum Of Amount (Mn)`; a leading
  blank column appears in some files — locate the header row by the `ISIN`
  cell, don't hardcode positions;
* the **title date is unreliable**: formats seen include `28 August 2026`,
  `16 .01.2026` (D.M.Y with stray space), and `04.10.2026` / `12.18.2025`
  (**M.D.Y**!). Worse, one file's title is a plain typo: the file labelled
  "as at 19.12.2025" on the index says "12.18.2025" inside while the
  previous day's file *also* says 12.18 — content proves the index right;
* one corrupted maturity cell (serial 14472 ≈ year 1939) while the
  Remaining-years cell was fine — validate serials to 2024–2060 and fall
  back to the ISIN-encoded maturity.

The saving grace: **`maturity_date − remaining_years × 365` reproduces the
observation date exactly** (the sheet computes remaining years as
days/365). Median across rows = a mathematically derived observation date.
Date policy: index "as at" label first, validated against the derived date;
derived date wins on conflict; title only as a last resort.

## ISIN structure (verified on 8 samples, check digits included)

`LKB00934F154` decodes as:

    LK  B  009  34  F  15  4
    │   │   │   │  │   │  └─ standard ISIN (Luhn) check digit — verified
    │   │   │   │  │   └─── maturity day
    │   │   │   │  └─────── maturity month, A=Jan … L=Dec
    │   │   │   └────────── maturity year (20YY)
    │   │   └────────────── original tenor, years, zero-padded
    │   └────────────────── B = Treasury bond (bills use LKA…)
    └────────────────────── country

**But beware**: the QuotesTBond "Maturity Period (Years)" column does NOT
match the tenor digits inside real ISINs (LKB00934F154 encodes tenor 9, the
column says 8; LKB01136H151 encodes 11, the column says 12), so an ISIN
cannot be synthesised from the quote sheet. Real ISINs are learned from the
volumes and trade-summary files; quote rows are joined to them by maturity
date, with the coupon as tie-break when two bonds share a maturity.

The quote sheet also carries the 2023-restructuring **step-coupon bonds**
("12%9%2027A", "12.4%7.5%5%2029A" — several rates chained before the year).
Their quotes look administered (bid/offer pinned at 13%/12% almost every
row) but they are real bonds; coupon_pct records the first step and the
full label goes into bonds.notes.

## Secondary Market Trade Summary (real PDF, one page)

Per-ISIN **executed trades** — bills and bonds together: ISIN, tenure,
security type (Tbill/TBond), opening/closing/highest/lowest/weighted-average
yield (already in %), volume (Rs. mn), number of trades; plus a small
indicators table (total turnover, trade/participant counts) for the report
date and the previous session. pdfplumber's `extract_tables` handles it
well, but **three cell-boundary layouts** appear across the archive and the
parser handles all three by reading rows by content rather than position:

1. clean (Aug 2026): one cell per column;
2. fused (Dec 2025): the row number and ISIN share one cell
   (`"1LKA36426K135"`) and the ISIN column is empty;
3. collapsed (Feb 2026): the *entire* table extracts as a single row whose
   cells are whole newline-joined columns — re-exploded by splitting on
   newlines and zipping back into per-security rows.

Number cells contain stray spaces from digit grouping (`2 ,800`) — strip
`[ ,]` before parsing. Every parse reconciles against the PDF's own
"Total Turnover" indicator, which is how all three layouts were verified. Index link text usually carries the date
("… - 28 August 2026" or "… 26.05.2026"); ~20 entries are labelled just
"Download" with the date in an ancestor element's text.

## Other pages inspected

* `government-securities/section/market reports` and `…/auction result` are
  hub pages (no files) pointing at the sections above plus bill/bond/dollar
  auction results.
* `result-treasury-bonds/section/2026` lists auction press releases in
  three languages (duplicated links) — left to the later auction stage.

## Known coverage limitation: quotes without a discoverable ISIN

The quote sheet identifies bonds only by coupon + maturity, and its tenor
column cannot be used to synthesise an ISIN (above). The pipeline therefore
learns real ISINs from the volumes and trade-summary files and joins quotes
to them by maturity date (coupon as tie-break). A bond that never traded
anywhere in the archive window has no discoverable ISIN, so its quotes are
counted and reported but not stored.

Measured on the Dec 2025 - Aug 2026 backfill: 49 distinct bonds discovered,
~45 of the ~92 daily quote rows stored per day. The remainder are ~19
step-coupon restructuring bonds plus ~24 never-traded ordinary bonds
(typically long-dated, maturing on the 1st of a month).

This is deliberate: inventing an ISIN would silently corrupt every join
downstream, and a wrong identifier is far worse than a missing row. Each
file's `parse_note` records the split, e.g. "49 quotes (43 without a known
ISIN)", so the gap is visible in the database rather than hidden.

To close it, supply a coupon+maturity -> ISIN reference table (the later
auction-results stage publishes ISINs directly, which will fill most of it
automatically), and re-run `python -m pipeline.backfill`: the cached files
re-parse offline and the newly resolvable quotes land without a single
download.

## Repeatability rule for future parser fixes

Ingestion clears a file's previous output before writing the new rows
(`db.clear_quotes` / `clear_volumes` / `clear_trade_summary`). Without this,
re-parsing was additive: a row emitted by an older, buggier parser survived
forever because nothing produced it again to overwrite. One such row was
found in practice (an empty `security_type` left by the pre-fix trade
summary parser).

The practical consequence: after fixing a parser, just re-run
`python -m pipeline.backfill`. It re-parses everything from the cache with
no downloads, and the database ends up exactly as if that file had never
been parsed the old way.

## Auction press releases (PDF)

Two document shapes are published under identical index titles, so the
parser decides from content, not link text:

* **auction result** — a table laid out one COLUMN PER BOND (Series, Date
  of Maturity, ISINs, Coupon, Amount Offered, Bids Received, Amount
  Accepted, Weighted Average Yield Rate);
* **issuance window** — a prose follow-up naming ISINs and their yields in
  sentences.

Every auction is published in English, Sinhala and Tamil; only the English
release is parsed. Layout hazards, all real:

* field names wrap across up to three table rows, and the values can sit on
  a row of their own BETWEEN two halves of the label ("Coupon Rate" /
  values / "(p.a.) (%)"), so a row's label is read from its neighbours —
  but a row's OWN label wins first, or one field claims the next field's
  values (this shifted every figure by one row until fixed);
* the table grid drifts between rows: in January 2026 the series labels sit
  in columns 4/7/11/14 while the ISINs sit in 4/8/11/14, so values are read
  as "non-empty cells from the first bond column rightwards";
* in prose releases a line break falls inside the searched phrase
  ("Weighted Average\nYield Rates of"), so prose is matched against
  whitespace-collapsed text, and the yields themselves contain full stops
  so the text after the phrase is taken by length, not up to the next
  period.

Cross-check available: an auction result and its issuance follow-up state
the same weighted average yields in completely different formats. The
25 August 2026 pair agrees at 10.54% / 11.70%.

## What the auctions did and did not fix

They were added expecting to roughly double the bonds on the curve. **They
did not.** All 30 English releases across 2025-2026 cover only 17 distinct
ISINs, every one of which the volumes and trade-summary files had already
revealed. Bonds per day is unchanged at 44-46.

What they did give:

* **all 49 bonds now carry a series label**, so quotes resolve by exact
  label (43 a day) instead of by maturity-date inference — a materially
  more robust join, and the reason the previous coupon tie-break is now
  only a fallback;
* auction weighted-average yields as observations with `executable=1`;
* offered / bids / accepted amounts, i.e. bid-to-cover, for later signals.

**Synthesising the missing ISINs was tested and rejected.** Comparing the
quote sheet's tenor column against the tenor encoded in the ISIN, for the
44 bonds where both are known: 33 agree, 8 are off by +1, 2 by -1, 1 by +2.
A quarter of synthesised ISINs would therefore be wrong, and a wrong ISIN
silently merges two bonds' histories — far worse than a missing row.

## Auction announcements (PDF) — the best reference source

Published ahead of each auction at `/web/treasury-bonds-issuances`, in three
languages; only the English one is parsed. The table is the tidy one of the
three auction-document shapes: field names in the left column, one column
per bond, nothing wrapped.

It is the only source for three things:

* **date of issue** and **coupon payment dates** ("01 February & 01 August",
  normalised to `02-01,08-01`);
* **accrued interest at settlement**, which cross-checks cleanly — the
  10.00%2030A announcement gives Rs 0.8424 per 100, exactly
  `5.00 × 31/184`, i.e. actual/actual on a semi-annual coupon, 31 days from
  the 01 August coupon to the 01 September settlement;
* **which bonds are currently being auctioned**, which is what "on the run"
  means in practice and is how the signals stage knows where the depth is.

## Two ways the quote-to-ISIN join was wrong

Both were found by asking why the most-traded bonds were missing from the
signal list, and both are now covered by tests.

**The series letter is not printed consistently.** An announcement calls a
bond "11.20%2033"; the daily quote sheet calls the same bond "11.20%2033A"
(same maturity, same coupon). An exact label match misses. A
letter-insensitive match is therefore tried next, and only accepted where
it resolves to exactly one bond.

**Maturity alone was enough to attribute a quote — and it should never have
been.** Four maturities in the current quote sheet carry two different
series each:

    2029-07-15   20.00%2029A          and 1.00%2029A
    2031-05-15   18.00%2031A          and 12.40%7.50%5.00%2031A
    2033-01-15   11.20%2033A          and 12.40%7.50%5.00%2033A
    2035-03-15   11.50%2035A          and 12.40%7.50%5.00%2035A

Where only ONE of the pair was known to the database, the old rule returned
it for BOTH quotes, so a step-coupon bond's prices were written to an
ordinary bond's ISIN — two bonds' histories silently merged into one. It
also stamped that bond's `notes` with the other's step-coupon label, and
because the curve stage treated any `notes` value as "this is a step-coupon
bond, exclude it", two liquid benchmarks (11.20%2033A and 11.50%2035A) were
dropped from **every** curve despite having 183 days of quotes each.

Three changes: the coupon must agree wherever both are known, even when only
one bond matches the maturity; the curve decides step-coupon status from the
series label rather than from free-text `notes`; and a repair on connect
clears a step-coupon note from a bond whose own label carries a single
coupon, which COALESCE could never do on its own.

## Which bonds are actually traded

The quote sheet prices 44-46 bonds every business day, but that is not the
same as 44-46 bonds being dealable. Two published series say which are:

* **Outright Transactions Volumes** and the **Trade Summary** give turnover,
  trade count and — most usefully — how MANY DAYS a bond traded at all. Over
  a 60-day window the market splits sharply: a handful of bonds trade on 20
  to 30 days, and the long tail trades on 1 to 5.
* **Auction announcements and results** say which bonds the PDMO is issuing
  now. A bond auctioned in the last few months is the on-the-run paper the
  dealers make real prices in.

Neither alone is enough. Turnover alone promotes a bond that traded once in
size; benchmark status alone promotes paper that was announced and then
barely traded. The tiering in `signals/liquidity.py` requires both for the
core book (auctioned within 120 days AND traded on >= 8 of the last 60), and
turnover alone for the "active" tier (>= 10 of the last 60).

On 2026-09-02 that gives 9 core, 11 active and 22 wider, and the core book
is exactly the paper the auction announcements name — 11.70%2034A (Rs 80.6bn
over 60 days), 11.00%2030B, 10.00%2030A, 10.75%2037A, 10.85%2036A,
11.20%2033, 11.50%2032A, 11.00%2030A and 10.75%2034A, with 11.60%2031A
trading but too new to score.

**The benchmark floor cannot be waived.** It was first set at 3 days, on the
reasoning that a freshly auctioned bond's printed trade record lags its real
dealability. That let 11.50%2035A into the core book on 6 trading days, and
it is not a bond anyone is dealing in size. Eight days is the floor now: low
enough that genuinely new paper still clears it within a fortnight of its
auction, high enough that an announced-but-untraded bond does not.

## The auction cycle

`python -m signals.validate` measures where in its issuance cycle a bond
sits cheap to its OWN norm (`dislocation_bp`, the same quantity the reports
show as `gap`). Over the 44 bond-auctions on 15 auction dates in this data:

    10-6 days before      +0.3bp     n=99
    5-1 days before       +0.7bp     n=75
    auction day           +0.7bp     n=25
    1-7 days after        +6.0bp    n=122
    8-14 days after       +5.7bp    n=142
    15-30 days after      +2.8bp    n=335

This is the OPPOSITE of the textbook pre-auction concession, in which the
market is supposed to cheapen a bond going in to make room for the supply.
Here nothing happens before and the cheapening arrives after, persisting for
about a fortnight while the new paper is distributed, then decaying.

A plausible reading is that the PDMO's auction sizes are known and modest
relative to the book, so there is nothing to concede in advance, while
dealers who took down the new supply then carry it and quote it cheap until
it clears. That is a story, not a finding; what the data supports is the
shape of the curve above.

Treat it as context rather than a signal: 44 events is few, the sample is
one 9-month regime, and 6bp is well inside a typical 16bp bid-offer. Its
practical use is judging a reading — a benchmark showing +5bp cheap a week
after its auction is closer to normal than the number alone suggests.

## Treasury bills: the funding leg

`trade_summary` carries executed T-bill trades alongside the bonds
(`security_type='Tbill'`, 686 rows over the sample). Bill ISINs use the same
12-character layout and the same Luhn check digit as bonds, with two
differences: the prefix is `LKA` rather than `LKB`, and the three-digit
field is the original tenor in **days** (091, 182, 364) rather than in
years. `pipeline/isin.decode_bill` handles them; the check digit rejects a
mangled cell exactly as it does for bonds.

That gives a funding leg from published data rather than an assumption. A
"12-month" rate is taken as any bill with 240 to 380 days left to run,
volume-weighted. Two facts about its availability shape the code:

* bills near the one-year point print on **52% of curve days** — so the most
  recent print on or before the day being scored is used, and its staleness
  is reported (median 1 day, max 6 over the last 60);
* on most of those days exactly **one** such bill trades — so a single trade
  would otherwise set the funding rate for the entire book. Prints are
  volume-weighted over a 5-day trailing window instead, which lifts the
  median sample to 2 and cuts the day-to-day jitter to 1.5bp.

The rate moved from 8.04% to 9.75% over the sample, so carry is not a
constant either.

## The quote-to-trade gap moves, and that is the finding

On days a bond both quoted and traded, the executed weighted-average yield
can be compared with the quoted mid. Over 2,077 such bond-days the median
gap is +10.4bp and the mean +16.5bp — but a single number is the wrong
summary, because the monthly medians run:

    2025-12   +8.3      2026-03  +15.2      2026-06  +51.9      2026-09   -6.6
    2026-01   +9.8      2026-04  +16.7      2026-07  +22.5
    2026-02   +0.8      2026-05  +20.6      2026-08   -3.6

Early June ran +100 to +134bp on the daily medians, across 25 of the 31
bonds that traded, decaying smoothly over a fortnight. That is not a parse
artifact — it is market-wide and it decays — it looks like dealer screens
lagging a fast move while trades printed far cheaper. The gap is negative
now, so the sample average has the wrong sign for the current regime.

### Why it is not bucketed by tenor

The whole-sample tenor split is tempting:

    0-2y   +17.9      4-7y    +10.1      10y+   +8.6
    2-4y    +9.4      7-10y    +8.3

and the front-end effect is broad, not one name: 16 of 16 front-end bonds
sit above +12bp. It still fails out of sample. Predicting a bucket's
next-quarter median from that bucket's own past gives a mean absolute error
of **10.9bp**, against **9.2bp** for using the blended past — the bucketed
estimate is worse. The reason is visible in the quarterly table: the
market-wide level swings by 50bp while the tenor spread is worth about 9bp,
and the front-wider-than-long ordering held in 2025Q4, 2026Q1 and 2026Q2 but
**reversed** in 2026Q3 (front +4.5 against +5.9 at 7-10y).

Window length was chosen the same way, on out-of-sample error predicting
each day's realised gap: all history 18.1bp, 120 days 18.0bp, 60 days
16.5bp, 20 days 13.0bp. Hence a 20-day blended window. Even that is a 13bp
standard error on a quantity that has ranged over 140bp, so it describes a
regime rather than a level.

### What it does and does not affect

It does **not** bias any z-score. A bond is scored against its own trailing
mean, so any offset that is stable for that bond divides out completely; a
tenor-dependent but stable gap cannot move a z-score at all.

It does change the yield you actually buy at, which is where carry starts.
Because the estimate is blended across the market, it shifts every bond's
carry by the same amount — moving the level and not the ranking. That is why
`signals/carry.py` takes it as a single `entry_gap_bp` argument rather than
a per-bond adjustment.

## The trade file moved, and the curve stopped checking itself

On 2026-09-18 the model looked healthy — the page said "updated", carried the
current date, and every workflow run was green — while its only out-of-sample
check had been dead for a week. Worth writing down, because nothing about it
looked broken.

**What happened.** The trade summary covering day D is published after that
day's quote sheet. Until 2026-09-10 it arrived the same evening, so the
nightly job ingested both and fitted day D's curve with its trades already
present. From 2026-09-11 the file began arriving the following morning
instead. The curve for day D was then fitted with no trades at all, and
`available_dates(only_new=True)` only ever returned days with NO curve — so
a day fitted early was never revisited, and its trades were dropped for good
rather than late.

**What it cost.** Four consecutive days (09-11, 09-14, 09-15, 09-16) carried
55 executed trades the curve never saw. Priced against the curve stored for
those days they read:

    2026-09-11   8 trades   mean +20.0bp vs curve
    2026-09-14  19 trades   mean +29.5bp
    2026-09-15  16 trades   mean +40.0bp
    2026-09-16  12 trades   mean +35.7bp

That is the largest quote-to-trade divergence anywhere in the sample, and
the check that exists to catch exactly this had switched itself off. The
dashboard went on showing a trade-bias tile the whole time, quietly falling
back to the last day that had a reading without saying how old it was.

**The fix.** `dates_with_late_trades` refits any day whose stored `n_trades`
differs from what `load_trades` returns now. It compares against that
function rather than a raw row count, so a day whose trades are all
legitimately unusable (step-coupon bonds, say) settles at its stored count
instead of being refitted on every run. `trade_check_age` then reports how
old the last real check is, and both surfaces shout past three days rather
than printing a stale number as though it were today's.

**Was the market really 35bp away?** Yes, and the quotes were live: 45 of 45
bonds repriced every day through the period, and the 11.70%2034A mid moved
11.53% to 11.84%. The screen was moving, just not fast enough — trades were
printing 30-50bp cheap to quotes that were drifting up behind them.

## One index page timing out killed the whole run

The 2026-09-13 run failed outright on a 60-second connect timeout to
treasury.gov.lk. Nothing was wrong with the data; the site was briefly
unreachable. But the failure took down the curve, signals and dashboard
steps too, none of which need the network and all of which had a perfectly
good database to work from.

`build_worklist` now logs and skips an index page it cannot reach, and only
raises when EVERY page fails — which is an outage rather than a blip, and
where continuing would report success having fetched nothing. Ingest is
idempotent, so anything missed is picked up on the next run.

## The curve was missing its entire long end

The daily quote sheet carries 92 rows, of which the ISIN join resolved 46.
The parse note reported the other 46 as "unresolved", which read like a
join failure and was left alone for months. Looked at properly they are two
completely different things:

* **38 rows that belong nowhere near a curve.** The 2023 restructuring
  block: step-coupon bonds (`12.00%9.00%2033A`, `12.40%7.50%5.00%2035A`) and
  sub-1% bonds (`1.00%2032A`, `0.50%2040A`), posted at a flat 13.00 bid /
  12.00 offer every single day. A 100bp administered quote is not a price
  anyone deals on. One row, `12.00%9.00%2033A`, was even inverted at
  10.835/14.00.
* **8 rows that are ordinary bonds with ordinary prices**, quoted 18-30bp
  wide and moving daily. They resolve to nothing only because no auction
  release in the archive happens to name them.

Five of that second group sit past 14 years:

    7.80%2027A     0.9y      13.50%2044A   17.3y
    13.25%2033A    6.8y      13.50%2044B   17.7y
    13.25%2034A    7.3y      12.50%2045A   18.5y
    12.00%2041A   14.3y
    9.00%2043A    16.7y

The curve ended at 12.9 years while quotes ran to 18.5. Everything past the
2039 was extrapolation, including beta0, the parameter that is supposed to
BE the long-run level.

### Keying a bond with no ISIN

A synthesised ISIN was rejected before and stays rejected: the sheet's tenor
column disagrees with the tenor encoded in real ISINs 11 times out of 44, so
a quarter of guesses would be wrong, and a wrong ISIN merges one bond's
history into another's.

`isin.synthetic_key` sidesteps that by keying on what actually identifies
the cash flows — `SYN:2045-03-01:12.500` — behind a prefix no real ISIN can
have. The failure mode is strictly better: a bad key SPLITS one bond across
two keys rather than MERGING two bonds into one, and a split loses history
where a merge corrupts it. `_bond_lookup` reads only `LKB%` rows, so a real
ISIN always wins once an auction release names the bond, and the synthetic
key simply stops receiving quotes.

Admission is deliberately narrow (`_admissible_without_isin`): a single
coupon in the label, a two-way quote, and a spread above zero and at most
50bp. On 2026-09-17 that admitted exactly the 8 real bonds and rejected all
38 administered ones.

**Effect.** 45 bonds ending at 12.9y becomes 53 ending at 18.4y, with median
weighted RMSE essentially unchanged — which is itself evidence the
Nelson-Siegel shape genuinely extends out there rather than being bent to
fit.

## Lambda: the grid minimum is the wrong answer

Extending the curve to 18.4 years moved the error-minimising lambda from
2.82y to 4.72y. It was not adopted, and the reasoning is worth recording
because it is the same argument that fixed lambda in the first place:

    lambda    median RMSE    beta0 range      max 1-day move    >1pp moves
    2.822        9.74bp      11.81-14.37          0.87pp             0
    3.500        9.75bp      11.65-14.74          1.19pp             2
    4.000        9.71bp      11.47-14.99          1.44pp             2
    4.716        9.62bp      11.15-15.30          1.83pp             3
    5.500        9.55bp      10.69-15.60          2.29pp             4

The grid minimum buys 0.12bp of median fit and costs 1.6pp of beta0 range
and a doubling of the largest daily move. For a model whose output is a
residual measured against its own trailing history, parameters that mean the
same thing on every date are worth far more than a tenth of a basis point.

`calibrate_lambda` now encodes that: among lambdas within 2% of the best
pooled error, take the smallest. On this sample it returns 2.822y — the
value calibrated when the curve stopped at 13 years, which is a reassuring
thing for a longer cross-section to agree on independently.

It also no longer WRITES what it measures. That side effect bit during this
very investigation: a diagnostic asking "what would lambda be now" silently
repointed the stored model. Persisting is `--calibrate`'s own step
(`store_lambda`), so measuring can never move the model underneath a stored
history of residuals.

## Two sources report traded volume, two business days apart

The Outright Transactions Volumes file and the Secondary Market Trade
Summary both report how much of a bond changed hands. They carry identical
amounts on different dates:

    10.75%2034A   trade summary:  3,400mn on 2026-09-14,  1,750mn on 09-15
                  volumes file:   3,400mn on 2026-09-16,  1,750mn on 09-17

Matching on (ISIN, exact amount) across the archive, the offset is
systematic:

    trade summary date + 0 business days ->   59 matches
                        + 1               ->  102
                        + 2               ->  938   <-- the alignment
                        + 3               ->  139

The cause is settlement. The volumes file has no date column; its date is
derived from a remaining-years figure, and that figure is quoted to
SETTLEMENT rather than to the trade. PDMO settlement is T+2 on 9 of the 16
auctions in this data, which is exactly the offset observed.

**Which date is right depends on the question.** For "when did this bond
trade", the trade summary. For "when does this money move", the volumes
file. The pipeline stores both, unaltered, and `pipeline/report.py` now
plots the trade-dated one and labels the other `settled_mn` rather than a
bare `volume_mn`.

Nothing else was affected: the curve, the signals, liquidity tiering and
carry all take turnover from `trade_summary`. The defect was confined to one
diagnostic chart — which happened to be the exact chart someone would open
to ask "did the 2034 trade", so it was worth correcting rather than
documenting away.

A tempting fix was rejected: shifting the volumes date back two business
days on ingest. Settlement is T+2 only modally (T+3 on 4 auctions, T+4 on 2,
T+5 on 1), and only 938 of 2,250 trade rows matched an amount at all, so the
arithmetic would be a guess dressed as a correction. Storing what each
source says, and choosing between them at the point of use, is honest.

## The site can answer with a firewall instead of a report

On 2026-09-19 treasury.gov.lk began serving this project's container
**HTTP 307 and a Sucuri JavaScript challenge page** in place of every index.
GitHub's runners were unaffected on the same day — the 22:08 UTC run logged
348 daily files and 173 trade summaries as usual — so it is an IP
reputation judgement, most likely provoked by a 631-file backfill a few
hours earlier.

What made it worth fixing is how it would have failed rather than that it
happened. Nothing about the challenge looks like an error:

* `raise_for_status()` ignores 3xx, so a 307 passed straight through;
* the challenge is valid HTML, so the index parser simply found no rows;
* zero rows meant `build_worklist` contributed nothing;
* the run then reported success having fetched precisely nothing.

Green, quiet and wrong — the same shape as the trade-check failure a week
earlier, which is twice now that a silent no-op has outlasted a loud one.
Two guards:

* `fetch.polite_get` refuses to return a response that is not the content
  asked for: any status at or above 300, or a small HTML body carrying a
  known challenge marker. A challenge now raises, retries, and finally fails
  the fetch.
* `build_worklist` counts an index that parses to ZERO entries as a failure,
  not as "no news". One empty index is survivable, because a year that has
  not started yet legitimately lists nothing; every index empty raises.


## How late is the trade file, really?

This was asserted before it was measured, and the assertion was wrong.
Restricting to files the daily runs caught fresh (so excluding the initial
2026-08-31 backfill, which makes every historical file look 120+ days late):

    covers        weekday   first seen by the pipeline    lag
    2026-09-02    Wed       2026-09-02 22:14              0d
    2026-09-04    Fri       2026-09-04 22:00              0d
    2026-09-09    Wed       2026-09-09 22:09              0d
    2026-09-10    Thu       2026-09-10 22:10              0d
    2026-09-11    Fri       2026-09-14 22:54              3d
    2026-09-14    Mon       2026-09-15 22:37              1d
    2026-09-15    Tue       2026-09-16 22:33              1d

Two things, only one of which was noticed at the time.

**The lag changed.** Through 2026-09-10 a day's trades were published the
same evening, in time for that night's run. From 2026-09-14 they arrive the
next day. That shift is what silently killed the out-of-sample check, and
`dates_with_late_trades` now absorbs it.

**Friday is not established as a three-day wait.** The claim came from one
observation, 2026-09-11, and it is confounded: the Saturday run at 21:52 UTC
did not have the file and the Sunday run FAILED on a connection timeout, so
the file could have appeared on either Saturday or Sunday and gone unseen
until Monday. The only other Friday in the sample, 2026-09-04, published
same-day under the old regime. No Friday has yet been observed under the
new one.

The dashboard therefore no longer prints a predicted publication date. It
said "published on Mon 21 Sep", derived from a next-business-day rule — a
rule invented from a single confounded data point, about a schedule the
PDMO has already changed once. It now says only that the trades are pending
and the next run will collect them, which is true whatever the lag turns
out to be.
