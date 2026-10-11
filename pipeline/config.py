"""Central configuration: paths, URLs, and politeness settings.

Keeping every tunable in one small file means the other modules stay free of
magic numbers, and you only ever edit one place. (If you know R: think of this
as the block of constants you'd put at the top of a script — except shared.)
"""

import datetime as dt
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
# PDMO index pages, one per section per year.
#
# The years are COMPUTED, never typed in. The PDMO publishes each year on its
# own page (.../section/2026, .../section/2027, ...), and a hand-written list
# stops being complete on 1 January without anything failing: last year's
# page still loads and still lists files, so the "empty index" guard in
# ingest.build_worklist never fires, and every run reports success while
# ingesting nothing new. Deriving the list from today's date means the new
# year's page is fetched from its first day. Until the PDMO posts to it, that
# page answers with zero files and is skipped with a warning, which the guard
# survives because the earlier years' pages still list theirs.
#
# Every year from 2025 is kept, not just the current one: early in January the
# last reports of December are still being posted, and nothing confirms which
# year's page they land on.
# ---------------------------------------------------------------------------
PDMO_BASE = "https://www.treasury.gov.lk"
FIRST_INDEX_YEAR = 2025


def index_years(today: dt.date) -> range:
    """Every index-page year to fetch: FIRST_INDEX_YEAR through today's year."""
    return range(FIRST_INDEX_YEAR, today.year + 1)


def _by_year(section: str) -> dict[int, str]:
    return {year: f"{PDMO_BASE}/web/{section}/section/{year}"
            for year in index_years(dt.date.today())}


# Daily-report index pages. Each lists, per business day, a "Daily Summary
# Report" PDF and an "Outright Treasury Bond Transactions Volumes" PDF,
# served from /api/file/<uuid>.
PDMO_INDEX_URLS = _by_year("report-daily-report")

# Secondary-market trade summary index pages (one real PDF per business day,
# with per-ISIN executed trades). Ingested alongside the daily reports.
TRADE_SUMMARY_INDEX_URLS = _by_year("reports-secondary-market-trade-summary")

# Treasury bond auction press releases. Each auction is published in English,
# Sinhala and Tamil; only the English release is parsed. These carry the
# series-label -> ISIN mapping that the daily quote sheet lacks, plus the
# auction weighted-average yield.
BOND_AUCTION_INDEX_URLS = _by_year("result-treasury-bonds")

# Auction ANNOUNCEMENTS, published ahead of each auction. Richer than the
# result releases: they carry each bond's date of issue, coupon payment
# dates and accrued interest, and they name the bonds currently being
# auctioned — which is what "on the run" means in practice.
BOND_ISSUANCE_INDEX_URLS = _by_year("treasury-bonds-issuances")

# Further PDMO index pages (supplied by the user). Inspection showed both
# are hub pages with no files of their own, listing the sections above.
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
