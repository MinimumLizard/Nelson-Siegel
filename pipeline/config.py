"""Central configuration: paths, URLs, and politeness settings.

Keeping every tunable in one small file means the other modules stay free of
magic numbers, and you only ever edit one place. (If you know R: think of this
as the block of constants you'd put at the top of a script — except shared.)
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths. Everything is relative to the repository root so the pipeline works
# no matter which directory you launch it from.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"          # cached PDFs live in data/raw/YYYY/
DB_PATH = DATA_DIR / "sgcp.sqlite"  # the one database file all stages share

# ---------------------------------------------------------------------------
# PDMO daily-report index pages, one per year. Each page lists, per business
# day, a "Daily Summary Report" PDF and an "Outright Treasury Bond
# Transactions Volumes" PDF, served from /api/file/<uuid>.
# ---------------------------------------------------------------------------
PDMO_BASE = "https://www.treasury.gov.lk"
PDMO_INDEX_URLS = {
    2025: f"{PDMO_BASE}/web/report-daily-report/section/2025",
    2026: f"{PDMO_BASE}/web/report-daily-report/section/2026",
}

# Secondary-market trade summary index pages (one real PDF per business day,
# with per-ISIN executed trades). Ingested alongside the daily reports.
TRADE_SUMMARY_INDEX_URLS = {
    2025: f"{PDMO_BASE}/web/reports-secondary-market-trade-summary/section/2025",
    2026: f"{PDMO_BASE}/web/reports-secondary-market-trade-summary/section/2026",
}

# Further PDMO index pages (supplied by the user). Inspection showed the
# first two are hub pages with no files; the bond-auction page lists press
# releases for the LATER auction stage, stubs only now.
PDMO_EXTRA_INDEX_URLS = {
    "market_reports": f"{PDMO_BASE}/web/government-securities/section/market%20reports",
    "auction_results": f"{PDMO_BASE}/web/government-securities/section/auction%20result",
    "bond_auction_2026": f"{PDMO_BASE}/web/result-treasury-bonds/section/2026",
}

# ---------------------------------------------------------------------------
# Politeness. This is a small government site: downloads are sequential, we
# pause between requests, retry with exponential backoff, and identify
# ourselves honestly in the User-Agent.
# ---------------------------------------------------------------------------
USER_AGENT = "sgcp-rv-pipeline/0.1 (personal research tool; polite sequential downloads)"
REQUEST_DELAY_SECONDS = 1.5   # pause between consecutive requests
REQUEST_TIMEOUT_SECONDS = 60  # give slow PDF responses time to finish
MAX_RETRIES = 4               # per request, with backoff: 2s, 4s, 8s, 16s
