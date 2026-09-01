"""Orchestration: index pages -> downloads -> parsers -> database.

This is the only module that combines the scraper, the fetcher, the parsers
and the database. Both CLIs are thin wrappers around `run()`:

    backfill  ->  run(only_new=False)   # everything, idempotently
    update    ->  run(only_new=True)    # skip files already parsed ok

Order matters within a run. Daily summaries name bonds only by series
label, never by ISIN, so everything that DOES carry an ISIN is ingested
first: auction releases (which print the series label beside its ISIN),
then volumes and trade summaries. Quotes are then resolved by that label,
falling back to maturity date for bonds no auction release covers.
Amended daily summaries simply overwrite: files are processed
oldest-posted first, so the amended file, posted later, wins the upsert.

Raw files are cached under data/raw/<index-year>/<uuid>.<ext> and never
modified; re-running any ingest re-reads the cache, so the whole database
can always be rebuilt offline with the network untouched.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from pipeline import (config, db, fetch, isin, parse_auction, parse_daily,
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
    for year, url in sorted(config.BOND_AUCTION_INDEX_URLS.items()):
        html = fetch.polite_get(url).content
        entries = scrape_index.parse_auction_index(html)
        log.info("bond-auction index %s: %d English releases", year, len(entries))
        items += [WorkItem(entry, year) for entry in entries]

    # Auctions first: they are the only source naming a series label next to
    # its ISIN, and the daily summaries (last) need those labels to resolve
    # their quotes. Volumes and trades in between contribute further ISINs.
    # Within a kind, oldest first, so an amended report overwrites its
    # original rather than the other way round.
    kind_order = {"bond_auction": 0, "volumes": 1, "trade_summary": 2,
                  "daily_summary": 3}
    items.sort(key=lambda item: (kind_order[item.entry.kind],
                                 item.entry.posted_date or item.entry.label_date
                                 or dt.date.min))
    return items


def _cache_path(item: WorkItem):
    extension = ".xls" if item.entry.kind in ("volumes", "daily_summary") else ".pdf"
    return (config.RAW_DIR / str(item.index_year)
            / (scrape_index.file_uuid(item.entry.url) + extension))


def _bond_lookup(conn) -> tuple[dict, dict]:
    """Indexes for resolving a quote row to a real ISIN.

    Returns ({series_label: isin}, {maturity_iso: [bond rows]}) — the first
    is exact and comes from the auction releases, the second is the older
    maturity-based fallback for bonds no release has covered.
    """
    by_label: dict[str, str] = {}
    by_maturity: dict[str, list] = {}
    for row in conn.execute("SELECT * FROM bonds WHERE isin LIKE 'LKB%'"):
        if row["series_label"]:
            by_label[row["series_label"]] = row["isin"]
        by_maturity.setdefault(row["maturity_date"], []).append(row)
    return by_label, by_maturity


def _resolve_isin(lookup, quote) -> tuple[str | None, str]:
    """The bond a quote row belongs to, and how it was identified.

    The series label is authoritative when known: it is what the auction
    release printed next to the ISIN. Maturity is the fallback, and it can
    be ambiguous (two bonds maturing the same day), in which case the
    coupon decides and anything still unclear is left unresolved rather
    than guessed.
    """
    by_label, by_maturity = lookup
    label = quote["series_label"]
    if label and label in by_label:
        return by_label[label], "label"

    candidates = by_maturity.get(quote["maturity_date"].isoformat(), [])
    if len(candidates) == 1:
        return candidates[0]["isin"], "maturity"
    coupon = quote["coupon_pct"]
    matching = [bond for bond in candidates
                if bond["coupon_pct"] is not None and coupon is not None
                and abs(bond["coupon_pct"] - coupon) < 0.005]
    if len(matching) == 1:
        return matching[0]["isin"], "maturity+coupon"
    return None, "unresolved"


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
    how = {"label": 0, "maturity": 0, "maturity+coupon": 0}
    for quote in parsed.quotes:
        matched_isin, method = _resolve_isin(lookup, quote)
        if matched_isin is None:
            unmatched += 1
            continue
        how[method] += 1
        db.upsert_bond(conn, matched_isin, quote["coupon_pct"],
                       quote["maturity_date"].isoformat(), None, iso,
                       notes=quote["printed_label"],
                       series_label=quote["series_label"])
        db.upsert_quote(conn, iso, matched_isin,
                        quote["bid_yield"], quote["offer_yield"],
                        quote["bid_price"], quote["offer_price"], raw_ref)
        matched += 1
    note = (f"{matched} quotes ({how['label']} by label, "
            f"{how['maturity'] + how['maturity+coupon']} by maturity; "
            f"{unmatched} unresolved)")
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


def _ingest_auction(conn, item: WorkItem, path, raw_ref):
    """Auction / issuance release -> bonds (with series labels) + results.

    This is the only ingester that teaches the database a series label, so
    it also fills in the coupon and maturity it prints. The weighted
    average yield is additionally written as an observation with
    source='auction' and executable=1 — an auction level is a price at
    which money actually moved, unlike the indicative daily quotes.
    """
    parsed = parse_auction.parse(path)
    iso = parsed.auction_date.isoformat()
    db.clear_auction(conn, raw_ref)
    labelled = 0
    for bond in parsed.bonds:
        tenor, _ = isin.decode(bond["isin"])
        db.upsert_bond(conn, bond["isin"], bond["coupon_pct"],
                       bond["maturity_date"].isoformat(), tenor, iso,
                       series_label=bond["series_label"])
        labelled += 1 if bond["series_label"] else 0
        db.upsert_auction(
            conn, iso, bond["isin"], parsed.kind,
            parsed.settlement_date.isoformat() if parsed.settlement_date else None,
            bond["way_pct"], bond["offered_lkr"], bond["bids_lkr"],
            bond["accepted_lkr"], raw_ref)
        if bond["way_pct"] is not None:
            db.upsert_auction_observation(conn, iso, bond["isin"],
                                          bond["way_pct"], bond["accepted_lkr"],
                                          raw_ref)
    return iso, f"{parsed.kind}: {len(parsed.bonds)} bonds ({labelled} with a series label)"


INGESTERS = {
    "bond_auction": _ingest_auction,
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
