# SGCP RV Pipeline — Stage 0: Data Layer

Ingests Sri Lanka PDMO daily secondary-market reports (Treasury bond quotes,
yields, and traded volumes) into a SQLite database. Future stages — a
`curves/` module (daily Nelson-Siegel fit) and a `signals/` module (rich/cheap
residuals, switch signals) — will read from that database and never touch the
PDFs directly.

## Status

Stage 0 scaffolding. The parser and final schema are designed around real
sample PDFs (see `pipeline/inspect_samples.py`); building the full parser
waits until those samples have been inspected and the structure confirmed.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Commands

```bash
# one-off diagnostics: inspect real PDFs before the parser exists
python -m pipeline.inspect_samples --list 2026          # list index-page PDF links
python -m pipeline.inspect_samples --download <url>...  # fetch chosen samples
python -m pipeline.inspect_samples --dump data/samples/*.pdf  # show table structure

# the pipeline proper (stubs until the parser is built)
python -m pipeline.backfill                    # download + parse the whole archive
python -m pipeline.update                      # fetch only what's new
python -m pipeline.report --isin LKB00934F154  # one bond's yield/volume history

# tests
pytest
```

## Data flow and layout

```
treasury.gov.lk index pages ──> data/raw/YYYY/*.pdf  (immutable cache)
                                      │
                                      ▼  pdfplumber parsing (repeatable)
                                data/sgcp.sqlite
                                      │
                                      ▼
                       future: curves/ and signals/ modules
```

* Every PDF is cached on disk before parsing; parsing is always repeatable
  from the cache and never mutates raw data.
* Downloads are sequential, ~1.5 s apart, with retries + exponential backoff
  and an honest User-Agent (`pipeline/fetch.py`).
* Re-running backfill is idempotent: already-cached files are skipped.

## Database schema (provisional — to be confirmed against real PDFs)

* **bonds** — one row per instrument: `isin` (PK), `coupon_pct`,
  `maturity_date`, `first_seen_date`, `notes`.
* **observations** — one row per bond per date per source: `obs_date`, `isin`,
  `source` ('pdmo_daily' | 'auction' | 'fct_quote'), `bid_yield`,
  `offer_yield`, `mid_yield`, `price`, `volume_lkr`, `executable` (0/1),
  `raw_ref`.
* **fills** — own executions, loaded manually later: `fill_date`, `isin`,
  `side`, `yield`, `clean_price`, `dirty_price`, `face_lkr`, `counterparty`,
  `deal_ref`.
* **files** — download/parse provenance: `url`, `sha256`, `report_date`,
  `file_type`, `downloaded_at`, `parse_status`.

Dates are ISO-8601 strings; LKR amounts are integers.

## Out of scope for Stage 0

Curve fitting, signals, auction parsing (stub module only), any UI.
