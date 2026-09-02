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
