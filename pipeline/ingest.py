"""Orchestration: index pages -> downloads -> parsers -> database.

This is the only module that combines the scraper, the fetcher, the parsers
and the database. Both CLIs are thin wrappers around `run()`:

    backfill  ->  run(only_new=False)   # everything, idempotently
    update    ->  run(only_new=True)    # skip files already parsed ok

Order matters within a run: volumes and trade-summary files carry real
ISINs, daily summaries do not — so all ISIN-bearing files are ingested
first and the quote rows are then joined to the bonds table by maturity
date (coupon as tie-break). Amended daily summaries simply overwrite:
files are processed oldest-posted first, and the amended file, posted
later, wins the upsert.

Raw files are cached under data/raw/<index-year>/<uuid>.<ext> and never
modified; re-running any ingest re-reads the cache, so the whole database
can always be rebuilt offline with the network untouched.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from pipeline import (config, db, fetch, isin, parse_daily,
                      parse_trade_summary, scrape_index)
from pipeline.parse_daily import ParseError

log = logging.getLogger(__name__)


@dataclass
class WorkItem:
    entry: scrape_index.IndexEntry
    index_year: int  # which index page listed it (also the cache subfolder)


def build_worklist() -> list[WorkItem]:
    """Fetch every index page and return all files, in ingest order."""
    items: list[WorkItem] = []
    for year, url in sorted(config.PDMO_INDEX_URLS.items()):
        html = fetch.polite_get(url).content
        entries = scrape_index.parse_daily_index(html)
        log.info("daily index %s: %d files", year, len(entries))
        items += [WorkItem(entry, year) for entry in entries]
    for year, url in sorted(config.TRADE_SUMMARY_INDEX_URLS.items()):
        html = fetch.polite_get(url).content
        entries = scrape_index.parse_trade_summary_index(html)
        log.info("trade-summary index %s: %d files", year, len(entries))
        items += [WorkItem(entry, year) for entry in entries]

    # ISIN-bearing kinds first, then daily summaries; within a kind oldest
    # first so amended (later-posted) reports overwrite their originals.
    kind_order = {"volumes": 0, "trade_summary": 1, "daily_summary": 2}
    items.sort(key=lambda item: (kind_order[item.entry.kind],
                                 item.entry.posted_date or item.entry.label_date
                                 or dt.date.min))
    return items


def _cache_path(item: WorkItem):
    extension = ".pdf" if item.entry.kind == "trade_summary" else ".xls"
    return (config.RAW_DIR / str(item.index_year)
            / (scrape_index.file_uuid(item.entry.url) + extension))


def _bond_lookup(conn) -> dict:
    """{maturity_date_iso: [bond rows]} for joining quotes to real ISINs."""
    lookup: dict[str, list] = {}
    for row in conn.execute("SELECT * FROM bonds WHERE isin LIKE 'LKB%'"):
        lookup.setdefault(row["maturity_date"], []).append(row)
    return lookup


def _resolve_isin(lookup: dict, maturity_iso: str, coupon: float) -> str | None:
    """Pick the bond a quote row belongs to; None if unknown or ambiguous."""
    candidates = lookup.get(maturity_iso, [])
    if len(candidates) == 1:
        return candidates[0]["isin"]
    # Two bonds sharing a maturity date: the coupon decides.
    matching = [bond for bond in candidates
                if bond["coupon_pct"] is not None
                and abs(bond["coupon_pct"] - coupon) < 0.005]
    if len(matching) == 1:
        return matching[0]["isin"]
    return None


def _ingest_volumes(conn, item: WorkItem, path, raw_ref):
    parsed = parse_daily.parse_volumes(path)
    # The derived date is arithmetic on the data itself; the index label is
    # the fallback. Titles are untrustworthy (typos, month-first dates).
    obs_date = parsed.derived_date or item.entry.label_date
    if obs_date is None:
        raise ParseError(f"no usable observation date (title {parsed.title!r})")
    if item.entry.label_date and obs_date != item.entry.label_date:
        log.warning("%s: derived date %s != index label %s — trusting the data",
                    path.name, obs_date, item.entry.label_date)
    iso = obs_date.isoformat()
    # Drop whatever a previous parse of this date's volumes recorded, so the
    # database always mirrors the CURRENT parser rather than accumulating
    # rows a since-fixed parser once emitted.
    db.clear_volumes(conn, iso)
    for row in parsed.rows:
        tenor, maturity = isin.decode(row["isin"])  # parse_volumes verified it
        db.upsert_bond(conn, row["isin"], None, maturity.isoformat(), tenor, iso)
        db.upsert_volume(conn, iso, row["isin"], row["volume_lkr"])
    return iso, f"{len(parsed.rows)} volume rows"


def _ingest_daily_summary(conn, item: WorkItem, path, raw_ref):
    parsed = parse_daily.parse_daily_summary(path)
    iso = parsed.reporting_date.isoformat()
    db.clear_quotes(conn, raw_ref)  # see the note in _ingest_volumes
    lookup = _bond_lookup(conn)
    matched = unmatched = 0
    for quote in parsed.quotes:
        maturity_iso = quote["maturity_date"].isoformat()
        matched_isin = _resolve_isin(lookup, maturity_iso, quote["coupon_pct"])
        if matched_isin is None:
            unmatched += 1
            continue
        db.upsert_bond(conn, matched_isin, quote["coupon_pct"], maturity_iso,
                       None, iso, notes=quote["series_label"])
        db.upsert_quote(conn, iso, matched_isin,
                        quote["bid_yield"], quote["offer_yield"],
                        quote["bid_price"], quote["offer_price"], raw_ref)
        matched += 1
    note = f"{matched} quotes ({unmatched} without a known ISIN)"
    if matched == 0:
        raise ParseError("no quote row matched a known bond — " + note)
    return iso, note


def _ingest_trade_summary(conn, item: WorkItem, path, raw_ref):
    parsed = parse_trade_summary.parse(path, label_date=item.entry.label_date)
    iso = parsed.obs_date.isoformat()
    db.clear_trade_summary(conn, raw_ref)  # see the note in _ingest_volumes
    for row in parsed.rows:
        decoded = isin.decode(row["isin"])
        if decoded:  # bonds enrich the bonds table; bills (LKA...) don't
            tenor, maturity = decoded
            db.upsert_bond(conn, row["isin"], None, maturity.isoformat(), tenor, iso)
        db.upsert_trade_summary(conn, iso, row["isin"], row["security_type"],
                                row["open_yield"], row["high_yield"],
                                row["low_yield"], row["close_yield"],
                                row["wavg_yield"], row["volume_lkr"],
                                row["n_trades"], raw_ref)
    return iso, f"{len(parsed.rows)} trade rows"


INGESTERS = {
    "volumes": _ingest_volumes,
    "daily_summary": _ingest_daily_summary,
    "trade_summary": _ingest_trade_summary,
}


def ingest_item(conn, item: WorkItem) -> str:
    """Download (if not cached) and parse one file. Returns the parse status.

    Failures are recorded in `files` and never propagate: one bad file must
    not stop a 500-file backfill.
    """
    entry = item.entry
    path = _cache_path(item)
    downloaded = fetch.download_file(entry.url, path)
    if downloaded:
        db.record_file(conn, entry.url, sha256=fetch.sha256_of(path),
                       downloaded_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
    db.record_file(
        conn, entry.url, file_type=entry.kind,
        posted_date=entry.posted_date.isoformat() if entry.posted_date else None)

    raw_ref = scrape_index.file_uuid(entry.url)
    try:
        report_date, note = INGESTERS[entry.kind](conn, item, path, raw_ref)
        status = "ok"
    except ParseError as error:
        report_date, note, status = None, str(error), "failed"
        log.error("PARSE FAILED %s %s: %s", entry.kind, path.name, error)
    except Exception as error:  # noqa: BLE001 — a crash must not kill the run
        report_date, note, status = None, f"{type(error).__name__}: {error}", "failed"
        log.exception("UNEXPECTED FAILURE on %s %s", entry.kind, path.name)
    db.record_file(conn, entry.url, report_date=report_date,
                   parse_status=status, parse_note=note)
    conn.commit()
    log.info("%-13s %s -> %s (%s)", entry.kind, path.name, status, note)
    return status


def run(only_new: bool) -> dict:
    """Ingest the whole archive (or just unseen files). Returns tallies."""
    conn = db.connect()
    items = build_worklist()

    already_ok = {row["url"] for row in
                  conn.execute("SELECT url FROM files WHERE parse_status = 'ok'")}
    tallies = {"ok": 0, "failed": 0, "skipped": 0}
    for item in items:
        if only_new and item.entry.url in already_ok:
            tallies["skipped"] += 1
            continue
        tallies[ingest_item(conn, item)] += 1

    log.info("run complete: %(ok)d ok, %(failed)d failed, %(skipped)d skipped", tallies)
    return tallies
