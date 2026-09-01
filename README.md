# SGCP RV Pipeline — Stage 0: Data Layer

Ingests Sri Lanka PDMO secondary-market data — Treasury bond two-way quotes,
per-ISIN traded volumes, and per-ISIN executed trades — into a SQLite
database. Future stages — a `curves/` module (daily Nelson-Siegel fit) and a
`signals/` module (rich/cheap residuals, switch signals) — will read from
that database and never touch the raw files directly.

Three report families are ingested (details and quirks in
`docs/DATA_NOTES.md`):

| report | format | what it carries |
|---|---|---|
| Daily Summary Report | legacy Excel `.xls` (despite the URL) | per-bond PD two-way quotes: avg bid/offer price + yield |
| Outright Transactions Volumes | legacy Excel `.xls` | per-ISIN outright traded volume, Rs. mn |
| Secondary Market Trade Summary | PDF | per-ISIN executed trades: OHLC + wavg yield, volume, trade count |

The archive reaches back to **1 Dec 2025** (the site publishes nothing older
under these sections). A full backfill on 2026-08-31 ingested **542 files**
with zero parse failures: 49 bonds, 8,069 daily observations and 2,727
executed-trade rows spanning 2025-12-01 to 2026-08-31.

### One known gap

The quote sheet identifies bonds by coupon + maturity only, and its tenor
column disagrees with the tenor encoded in real ISINs — so ISINs cannot be
synthesised from it. Real ISINs are learned from the volumes and
trade-summary files, and quotes are joined to them by maturity date. About
45 of the ~92 daily quote rows resolve this way; the rest are step-coupon
restructuring bonds and never-traded long-dated bonds, whose quotes are
counted in each file's `parse_note` but not stored. Inventing an ISIN would
corrupt every downstream join, so the pipeline refuses to guess. See
`docs/DATA_NOTES.md` for how to close the gap without re-downloading.

## The data updates itself

A scheduled GitHub Action (`.github/workflows/daily-update.yml`) runs every
day at 20:00 UTC on GitHub's own servers: it fetches whatever the PDMO has
published since the last run, parses it, refreshes the watched bonds' charts
and commits the result back to this repository. Nothing needs to be running
on your machine.

That is why `data/sgcp.sqlite` and `data/reports/*.png` are committed here
(unusual for a database, deliberate in this case): the Action's runners are
wiped after every job, so the repository itself is where the accumulated
data lives. Cloning gives you current data immediately — the setup below is
only needed to run the pipeline yourself or to extend it.

A day with nothing new (weekend, public holiday) simply makes no commit. To
run it on demand, open the repository's **Actions** tab, pick **Daily PDMO
update**, and press **Run workflow**.

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
                                      ▼
                       future: curves/ and signals/ modules
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

## Out of scope for Stage 0

Curve fitting, signals, auction parsing (stub module only), any UI.
