"""The SQLite schema and every read/write the pipeline does against it.

Design decisions (agreed during sample inspection):

* Yields are stored in PERCENT (9.32 means 9.32% p.a.) even though the
  daily-summary workbook stores fractions (0.0932) — percent is what every
  human-facing source (trade summary PDF, FCT quotes) uses.
* LKR amounts are INTEGER rupees. The source files report millions with
  three decimals (232.825), so `round(mn * 1_000_000)` is exact.
* Dates are ISO-8601 TEXT ("2026-08-31"). SQLite has no date type; ISO
  strings sort correctly, which is all we need.
* Quotes and traded volumes for the same (day, ISIN) arrive in two
  different files, so `observations` rows are built up by two upserts that
  each touch only their own columns (think of it as a left-join done
  incrementally instead of in one merge() call).
* Executed trades (trade summary PDFs) get their own `trade_summary`
  table rather than extra OHLC columns on `observations`: two tidy tables
  beat one wide table where half the columns are NULL for half the rows.
"""

import logging
import sqlite3
from pathlib import Path

from pipeline import config

log = logging.getLogger(__name__)

SCHEMA = """
-- One row per bond we have ever seen. ISINs are synthesised/verified from
-- tenor + maturity (see pipeline/isin.py), so coupon and maturity are also
-- recorded here as the human-readable identity of the bond.
CREATE TABLE IF NOT EXISTS bonds (
    isin            TEXT PRIMARY KEY,
    coupon_pct      REAL,      -- e.g. 11.25 (percent per annum)
    maturity_date   TEXT,      -- ISO date
    tenor_years     INTEGER,   -- original tenor encoded in the ISIN
    series_label    TEXT,      -- canonical "10.00%2030A"; joins quotes to ISINs
    first_seen_date TEXT,      -- first obs_date this bond appeared on
    notes           TEXT
);

-- Primary auction / issuance-window results, one row per bond per auction.
CREATE TABLE IF NOT EXISTS auctions (
    auction_date    TEXT NOT NULL,
    isin            TEXT NOT NULL,
    kind            TEXT,      -- 'auction' (competitive) | 'issuance' (window)
    settlement_date TEXT,
    way_pct         REAL,      -- weighted average yield accepted, percent
    offered_lkr     INTEGER,
    bids_lkr        INTEGER,   -- bids_lkr / offered_lkr = bid-to-cover
    accepted_lkr    INTEGER,
    raw_ref         TEXT,
    PRIMARY KEY (auction_date, isin, kind)
);

-- One row per (day, bond, source). For source='pdmo_daily' the yield/price
-- columns come from the daily summary published ON obs_date and volume_lkr
-- from the volumes file covering obs_date.
CREATE TABLE IF NOT EXISTS observations (
    obs_date    TEXT NOT NULL,
    isin        TEXT NOT NULL,
    source      TEXT NOT NULL,   -- 'pdmo_daily' | 'auction' | 'fct_quote'
    bid_yield   REAL,            -- percent p.a.; bid = dealer buys from you
    offer_yield REAL,
    mid_yield   REAL,            -- simple average of bid and offer
    bid_price   REAL,            -- per 100 face value
    offer_price REAL,
    volume_lkr  INTEGER,         -- rupees traded outright that day
    executable  INTEGER NOT NULL DEFAULT 0,  -- 1 only for firm/executable quotes
    raw_ref     TEXT,            -- uuid of the source file (see `files`)
    PRIMARY KEY (obs_date, isin, source)
);

-- Executed trades from the secondary-market trade summary PDFs. Bills and
-- bonds both appear (bills have LKA... ISINs); yields already in percent.
CREATE TABLE IF NOT EXISTS trade_summary (
    obs_date      TEXT NOT NULL,
    isin          TEXT NOT NULL,
    security_type TEXT,          -- 'Tbill' | 'TBond' as printed in the PDF
    open_yield    REAL,
    high_yield    REAL,
    low_yield     REAL,
    close_yield   REAL,
    wavg_yield    REAL,          -- weighted-average yield: the headline number
    volume_lkr    INTEGER,
    n_trades      INTEGER,
    raw_ref       TEXT,
    PRIMARY KEY (obs_date, isin)
);

-- My own executed fills, loaded by hand later. The pipeline only creates
-- the table; nothing in Stage 0 writes to it.
CREATE TABLE IF NOT EXISTS fills (
    fill_date    TEXT NOT NULL,
    isin         TEXT NOT NULL,
    side         TEXT,           -- 'buy' | 'sell'
    yield        REAL,
    clean_price  REAL,
    dirty_price  REAL,
    face_lkr     INTEGER,
    counterparty TEXT,
    deal_ref     TEXT
);

-- Settings the curve stage calibrates once and then reuses (lambda_years).
CREATE TABLE IF NOT EXISTS curve_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One fitted Nelson-Siegel curve per day (written by the curves/ package).
-- The trade_* columns are the daily honesty check: how the curve, fitted on
-- dealer quotes, compares with the bonds that actually traded that day.
CREATE TABLE IF NOT EXISTS curve_fits (
    obs_date      TEXT PRIMARY KEY,
    beta0         REAL,     -- long-run level, percent
    beta1         REAL,     -- slope: beta0+beta1 is the very short end
    beta2         REAL,     -- curvature (the hump)
    lambda_years  REAL,     -- where the hump sits
    n_quotes      INTEGER,  -- bonds the curve was fitted on
    rmse_bp       REAL,     -- weighted fit error, basis points
    n_trades      INTEGER,  -- traded bonds available to check against
    trade_rmse_bp REAL,
    trade_bias_bp REAL,     -- mean(traded - fitted); the quote/trade gap
    fitted_at     TEXT
);

-- Per-bond distance from the fitted curve. This is what the signals stage
-- consumes. SIGN CONVENTION: residual_bp = observed - fitted, so POSITIVE
-- means the bond yields more than the curve says it should — i.e. CHEAP.
CREATE TABLE IF NOT EXISTS curve_residuals (
    obs_date       TEXT NOT NULL,
    isin           TEXT NOT NULL,
    source         TEXT NOT NULL,  -- 'quote' (in the fit) | 'trade' (check)
    tau_years      REAL,
    observed_yield REAL,
    fitted_yield   REAL,
    residual_bp    REAL,
    weight         REAL,           -- relative weight this point carried
    PRIMARY KEY (obs_date, isin, source)
);

-- Bookkeeping for every remote file: what we downloaded, its hash, which
-- report date it turned out to cover, and whether parsing succeeded.
-- parse_status: 'pending' | 'ok' | 'failed'
CREATE TABLE IF NOT EXISTS files (
    url           TEXT PRIMARY KEY,
    sha256        TEXT,
    file_type     TEXT,   -- 'daily_summary' | 'volumes' | 'trade_summary'
    report_date   TEXT,   -- the observation date the file covers
    posted_date   TEXT,   -- the date on the index row it was listed under
    downloaded_at TEXT,
    parse_status  TEXT NOT NULL DEFAULT 'pending',
    parse_note    TEXT    -- one-line summary: row counts, or the error
);
"""

# Indexes are created after the column migration below, since one of them
# is on a column that older databases gain only at migration time.
INDEXES = """
CREATE INDEX IF NOT EXISTS bonds_series ON bonds(series_label);
CREATE INDEX IF NOT EXISTS observations_isin ON observations(isin, source);
CREATE INDEX IF NOT EXISTS trade_summary_date ON trade_summary(obs_date);
CREATE INDEX IF NOT EXISTS curve_residuals_isin ON curve_residuals(isin, source);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the pipeline database and ensure the schema.

    Every caller goes through here, so the schema is always up before any
    query runs — the same idea as source()-ing a setup script at the top of
    an R analysis.
    """
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # rows behave like named lists, not tuples
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
    return conn


# Columns added after the first databases were built. CREATE TABLE IF NOT
# EXISTS leaves an existing table untouched, so new columns are added here
# instead; the pipeline is expected to run against a database that has been
# accumulating data since before they existed.
ADDED_COLUMNS = {
    "bonds": {"series_label": "TEXT"},
}


def _migrate(conn) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, column_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                log.info("migrated: added %s.%s", table, column)
    conn.commit()


def upsert_bond(conn, isin, coupon_pct, maturity_date, tenor_years, first_seen_date,
                notes=None, series_label=None):
    """Insert a bond, or fill in blanks on an existing row.

    COALESCE(old, new) keeps an already-known coupon if a later file
    doesn't provide one; first_seen_date only ever moves earlier. `notes`
    records e.g. the full step-coupon label ("12%9%2027A") of restructured
    bonds, whose single coupon_pct only holds the first step.
    """
    conn.execute(
        """INSERT INTO bonds (isin, coupon_pct, maturity_date, tenor_years,
                              first_seen_date, notes, series_label)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(isin) DO UPDATE SET
               coupon_pct      = COALESCE(bonds.coupon_pct, excluded.coupon_pct),
               maturity_date   = COALESCE(bonds.maturity_date, excluded.maturity_date),
               tenor_years     = COALESCE(bonds.tenor_years, excluded.tenor_years),
               notes           = COALESCE(bonds.notes, excluded.notes),
               series_label    = COALESCE(bonds.series_label, excluded.series_label),
               first_seen_date = MIN(COALESCE(bonds.first_seen_date, excluded.first_seen_date),
                                     COALESCE(excluded.first_seen_date, bonds.first_seen_date))""",
        (isin, coupon_pct, maturity_date, tenor_years, first_seen_date, notes, series_label),
    )


def upsert_quote(conn, obs_date, isin, bid_yield, offer_yield, bid_price, offer_price, raw_ref):
    """Write the quote half of a pdmo_daily observation (yields/prices only)."""
    mid = (bid_yield + offer_yield) / 2 if bid_yield is not None and offer_yield is not None else None
    conn.execute(
        """INSERT INTO observations
               (obs_date, isin, source, bid_yield, offer_yield, mid_yield,
                bid_price, offer_price, raw_ref)
           VALUES (?, ?, 'pdmo_daily', ?, ?, ?, ?, ?, ?)
           ON CONFLICT(obs_date, isin, source) DO UPDATE SET
               bid_yield   = excluded.bid_yield,
               offer_yield = excluded.offer_yield,
               mid_yield   = excluded.mid_yield,
               bid_price   = excluded.bid_price,
               offer_price = excluded.offer_price,
               raw_ref     = excluded.raw_ref""",
        (obs_date, isin, bid_yield, offer_yield, mid, bid_price, offer_price, raw_ref),
    )


def upsert_volume(conn, obs_date, isin, volume_lkr):
    """Write the traded-volume half of a pdmo_daily observation.

    Deliberately leaves the yield/price columns alone: the volumes file for
    day D and the quotes for day D arrive in different workbooks, and either
    may be parsed first (or be missing entirely).
    """
    conn.execute(
        """INSERT INTO observations (obs_date, isin, source, volume_lkr)
           VALUES (?, ?, 'pdmo_daily', ?)
           ON CONFLICT(obs_date, isin, source) DO UPDATE SET
               volume_lkr = excluded.volume_lkr""",
        (obs_date, isin, volume_lkr),
    )


def upsert_trade_summary(conn, obs_date, isin, security_type, open_yield, high_yield,
                         low_yield, close_yield, wavg_yield, volume_lkr, n_trades, raw_ref):
    """One executed-trades row from a trade summary PDF (replace on re-parse)."""
    conn.execute(
        """INSERT INTO trade_summary
               (obs_date, isin, security_type, open_yield, high_yield, low_yield,
                close_yield, wavg_yield, volume_lkr, n_trades, raw_ref)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(obs_date, isin) DO UPDATE SET
               security_type = excluded.security_type,
               open_yield    = excluded.open_yield,
               high_yield    = excluded.high_yield,
               low_yield     = excluded.low_yield,
               close_yield   = excluded.close_yield,
               wavg_yield    = excluded.wavg_yield,
               volume_lkr    = excluded.volume_lkr,
               n_trades      = excluded.n_trades,
               raw_ref       = excluded.raw_ref""",
        (obs_date, isin, security_type, open_yield, high_yield, low_yield,
         close_yield, wavg_yield, volume_lkr, n_trades, raw_ref),
    )


def _drop_empty_observations(conn):
    """Delete observation rows left with no data at all after a clear."""
    conn.execute("""DELETE FROM observations
                    WHERE bid_yield IS NULL AND offer_yield IS NULL
                      AND bid_price IS NULL AND offer_price IS NULL
                      AND volume_lkr IS NULL""")


def clear_quotes(conn, raw_ref):
    """Forget the quotes a previous parse of this file wrote.

    Called before re-ingesting a daily summary so the database always
    reflects what the CURRENT parser produces: if a fixed parser no longer
    emits a row, the stale row must not survive. The volume half of the
    row is left untouched (it came from a different file).
    """
    conn.execute("""UPDATE observations
                       SET bid_yield = NULL, offer_yield = NULL, mid_yield = NULL,
                           bid_price = NULL, offer_price = NULL, raw_ref = NULL
                     WHERE raw_ref = ?""", (raw_ref,))
    _drop_empty_observations(conn)


def clear_volumes(conn, obs_date):
    """Forget the traded volumes previously recorded for one date.

    Volume rows carry no raw_ref (the column belongs to the quote half),
    so they are cleared by date — exactly the scope one volumes file owns.
    """
    conn.execute("""UPDATE observations SET volume_lkr = NULL
                     WHERE obs_date = ? AND source = 'pdmo_daily'""", (obs_date,))
    _drop_empty_observations(conn)


def clear_trade_summary(conn, raw_ref):
    """Forget the executed trades a previous parse of this file wrote."""
    conn.execute("DELETE FROM trade_summary WHERE raw_ref = ?", (raw_ref,))


def upsert_auction(conn, auction_date, isin, kind, settlement_date, way_pct,
                   offered_lkr, bids_lkr, accepted_lkr, raw_ref):
    """One bond's result from one auction (replace on re-parse)."""
    conn.execute(
        """INSERT INTO auctions (auction_date, isin, kind, settlement_date, way_pct,
                                 offered_lkr, bids_lkr, accepted_lkr, raw_ref)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(auction_date, isin, kind) DO UPDATE SET
               settlement_date = excluded.settlement_date,
               way_pct         = excluded.way_pct,
               offered_lkr     = excluded.offered_lkr,
               bids_lkr        = excluded.bids_lkr,
               accepted_lkr    = excluded.accepted_lkr,
               raw_ref         = excluded.raw_ref""",
        (auction_date, isin, kind, settlement_date, way_pct,
         offered_lkr, bids_lkr, accepted_lkr, raw_ref))


def upsert_auction_observation(conn, obs_date, isin, way_pct, volume_lkr, raw_ref):
    """The auction's weighted-average yield as an observation.

    executable=1: unlike the dealers' indicative daily quotes, an auction
    yield is a level at which money actually changed hands.
    """
    conn.execute(
        """INSERT INTO observations (obs_date, isin, source, mid_yield,
                                     volume_lkr, executable, raw_ref)
           VALUES (?, ?, 'auction', ?, ?, 1, ?)
           ON CONFLICT(obs_date, isin, source) DO UPDATE SET
               mid_yield  = excluded.mid_yield,
               volume_lkr = excluded.volume_lkr,
               executable = 1,
               raw_ref    = excluded.raw_ref""",
        (obs_date, isin, way_pct, volume_lkr, raw_ref))


def clear_auction(conn, raw_ref):
    """Forget what a previous parse of this release wrote."""
    conn.execute("DELETE FROM auctions WHERE raw_ref = ?", (raw_ref,))
    conn.execute("DELETE FROM observations WHERE source='auction' AND raw_ref = ?", (raw_ref,))


def record_file(conn, url, **fields):
    """Insert or update one row in `files`. Only the passed fields change.

    Called twice per file in practice: once after download (sha256,
    file_type, posted_date, downloaded_at) and once after parsing
    (report_date, parse_status, parse_note).
    """
    allowed = {"sha256", "file_type", "report_date", "posted_date",
               "downloaded_at", "parse_status", "parse_note"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown files columns: {unknown}")
    conn.execute("INSERT INTO files (url) VALUES (?) ON CONFLICT(url) DO NOTHING", (url,))
    if fields:
        assignments = ", ".join(f"{column} = ?" for column in fields)
        conn.execute(f"UPDATE files SET {assignments} WHERE url = ?",
                     (*fields.values(), url))


def lkr_from_millions(millions: float) -> int:
    """Rs. millions (as the sources report) -> integer rupees, exactly."""
    return round(millions * 1_000_000)
