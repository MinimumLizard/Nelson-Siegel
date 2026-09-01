"""Turn the messy PDMO index pages into a clean list of files to download.

The index markup is hand-edited HTML with real hazards (all observed, see
docs/DATA_NOTES.md): anchor text split mid-word across two anchors, empty
anchors that sometimes point at a *different* row's file, stray spaces
inside dates, and one "Amended Daily Summary Report".

Defensive rules used here:

* Work row by row (each business day is one <p> element).
* Within a row, group anchors by their href and join their text fragments —
  that reassembles "D" + "aily Summary Report" into one label.
* Classify a file ONLY by its (joined) label. Empty-text anchors are
  ignored: they are the ones observed pointing at other rows' files.
* Dedupe by URL across the whole page; the first (newest) labelled
  appearance wins.

The scraper never opens a file — it only reports (url, kind, dates); the
parsers are the source of truth for what a file actually contains.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup

from pipeline import dates

log = logging.getLogger(__name__)


@dataclass
class IndexEntry:
    """One downloadable file discovered on an index page."""
    url: str
    kind: str                    # 'daily_summary' | 'volumes' | 'trade_summary'
    label_date: date | None      # date carried by the link text / row, if any
    posted_date: date | None     # the "> DD.MM.YYYY" date of the index row
    amended: bool = False        # True for "Amended Daily Summary Report"


def _content_paragraphs(html: bytes | str):
    """The <p> rows of the page body (falling back to the whole page).

    Scoping to .page-template--body__content matters: the site duplicates
    all content inside a JSON hydration blob elsewhere in the page.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one(".page-template--body__content") or soup
    return body.find_all("p")


def _file_links_by_href(paragraph):
    """{href: joined anchor text} for /api/file/ links in one row.

    Joining the fragments of same-href anchors repairs split labels;
    empty-text hrefs stay in the dict with "" so callers can ignore them.
    """
    joined: dict[str, str] = {}
    for anchor in paragraph.find_all("a", href=True):
        href = anchor["href"]
        if "/api/file/" not in href:
            continue
        if href.startswith("/"):
            href = "https://www.treasury.gov.lk" + href
        text = dates.collapse_ws(anchor.get_text(" ", strip=True))
        joined[href] = (joined.get(href, "") + text).strip()
    return joined


def parse_daily_index(html: bytes | str) -> list[IndexEntry]:
    """One year's daily-report page -> volumes + daily-summary entries."""
    entries: list[IndexEntry] = []
    seen: set[str] = set()
    for paragraph in _content_paragraphs(html):
        links = _file_links_by_href(paragraph)
        if not links:
            continue
        row_text = dates.collapse_ws(paragraph.get_text(" ", strip=True))
        posted = dates.parse_dmy(row_text)  # the leading "> DD.MM.YYYY"

        for href, label in links.items():
            if href in seen:
                continue
            if "outright" in label.lower():
                # The volumes label carries its own "as at" date.
                entries.append(IndexEntry(href, "volumes",
                                          dates.parse_dmy(label), posted))
                seen.add(href)
            elif "summary repor" in label.lower():
                # Matches "Daily Summary Report", the truncated
                # "Daily Summary Repor", and "Amended Daily Summary Report".
                entries.append(IndexEntry(href, "daily_summary", None, posted,
                                          amended="amended" in label.lower()))
                seen.add(href)
            elif label:
                log.warning("unrecognised labelled link %r -> %s", label, href)
            # Unlabelled hrefs: deliberately skipped (observed pointing at
            # other rows' files); if genuinely new they also appear labelled
            # on a neighbouring row and are picked up there.
    return entries


def parse_trade_summary_index(html: bytes | str) -> list[IndexEntry]:
    """One year's trade-summary page -> one entry per PDF.

    Label variants observed: the date in the anchor text itself
    ("... Trade Summary - 28 August 2026"), or an anchor saying just
    "Download" with the date in an ancestor's text, or (rarely) an empty
    anchor. The PDF's own title is authoritative anyway, so undated
    entries are still worth downloading.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one(".page-template--body__content") or soup
    entries: list[IndexEntry] = []
    seen: set[str] = set()
    for anchor in body.find_all("a", href=True):
        href = anchor["href"]
        if "/api/file/" not in href:
            continue
        if href.startswith("/"):
            href = "https://www.treasury.gov.lk" + href
        if href in seen:
            continue
        seen.add(href)

        text = dates.collapse_ws(anchor.get_text(" ", strip=True))
        label_date = dates.parse_spelled(text) or dates.parse_dmy(text)
        if label_date is None:
            # Climb until some ancestor's text yields a date (the
            # "Download"-style rows keep it one or two levels up).
            node = anchor
            for _ in range(4):
                if node.parent is None:
                    break
                node = node.parent
                context = dates.collapse_ws(node.get_text(" ", strip=True))
                label_date = dates.parse_spelled(context) or dates.parse_dmy(context)
                if label_date:
                    break
        entries.append(IndexEntry(href, "trade_summary", label_date, None))
    return entries


def file_uuid(url: str) -> str:
    """The /api/file/<uuid> tail — used as the cache filename and raw_ref."""
    return url.rstrip("/").rsplit("/", maxsplit=1)[-1]
