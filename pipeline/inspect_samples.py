"""One-off diagnostic tool for the "important first step": look at real data
BEFORE designing the parser and schema.

This is deliberately not part of the production pipeline. It has three modes,
meant to be run in order:

1. List every PDF link an index page exposes (and save the raw HTML so the
   messy markup can be studied offline):

       python -m pipeline.inspect_samples --list 2026

2. Download a handful of chosen sample PDFs into data/samples/:

       python -m pipeline.inspect_samples --download <url> <url> ...

3. Dump the internal structure of downloaded PDFs — page count, tables that
   pdfplumber finds, their first rows, and the raw text — which is what the
   parser and final schema get designed around:

       python -m pipeline.inspect_samples --dump data/samples/*.pdf
"""

import argparse
import logging
import sys
from pathlib import Path

from bs4 import BeautifulSoup

from pipeline import config, fetch

log = logging.getLogger(__name__)

SAMPLES_DIR = config.DATA_DIR / "samples"
INDEX_HTML_DIR = config.DATA_DIR / "index_html"


def list_links(year: int) -> None:
    """Fetch one year's index page and print every /api/file/ link with context."""
    url = config.PDMO_INDEX_URLS[year]
    response = fetch.polite_get(url)

    # Keep the raw HTML: the markup is reportedly messy, and having the file
    # on disk lets us study it without hammering the site again.
    INDEX_HTML_DIR.mkdir(parents=True, exist_ok=True)
    saved = INDEX_HTML_DIR / f"index_{year}.html"
    saved.write_bytes(response.content)
    print(f"# saved raw HTML to {saved}")

    soup = BeautifulSoup(response.content, "html.parser")
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/api/file/" not in href:
            continue
        if href.startswith("/"):
            href = config.PDMO_BASE + href
        duplicate = " [DUPLICATE]" if href in seen else ""
        seen.add(href)
        text = " ".join(anchor.get_text(" ", strip=True).split())
        # The parent element's text usually carries the report date; print a
        # trimmed version of it so links can be matched to business days.
        parent_text = " ".join(anchor.parent.get_text(" ", strip=True).split())[:160]
        print(f"{href}\n    text:    {text!r}{duplicate}\n    context: {parent_text!r}")
    print(f"# {len(seen)} unique /api/file/ links found on {url}")


def download_samples(urls: list[str]) -> None:
    """Download specific PDF URLs into data/samples/ (named by their uuid)."""
    for url in urls:
        uuid = url.rstrip("/").rsplit("/", maxsplit=1)[-1]
        destination = SAMPLES_DIR / f"{uuid}.pdf"
        was_downloaded = fetch.download_file(url, destination)
        status = "downloaded" if was_downloaded else "already cached"
        print(f"{status}: {destination}")


def dump_structure(paths: list[str]) -> None:
    """Print what pdfplumber sees inside each PDF: tables, rows, raw text."""
    import pdfplumber  # imported here so --list/--download work without it

    for path in paths:
        print(f"\n{'=' * 78}\nFILE: {path}\n{'=' * 78}")
        with pdfplumber.open(path) as pdf:
            print(f"pages: {len(pdf.pages)}, metadata: {pdf.metadata}")
            for page in pdf.pages:
                print(f"\n--- page {page.page_number} ---")
                tables = page.extract_tables()
                print(f"tables found: {len(tables)}")
                for table_number, table in enumerate(tables, start=1):
                    n_cols = max((len(row) for row in table), default=0)
                    print(f"  table {table_number}: {len(table)} rows x {n_cols} cols")
                    for row in table[:5]:  # first rows show headers + shape
                        print(f"    {row}")
                    if len(table) > 5:
                        print(f"    ... ({len(table) - 5} more rows)")
                text = page.extract_text() or ""
                lines = text.splitlines()
                print(f"raw text ({len(lines)} lines), first 20:")
                for line in lines[:20]:
                    print(f"  | {line}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", type=int, metavar="YEAR",
                       help="fetch YEAR's index page and list all PDF links")
    group.add_argument("--download", nargs="+", metavar="URL",
                       help="download sample PDFs into data/samples/")
    group.add_argument("--dump", nargs="+", metavar="PDF",
                       help="show table structure of downloaded PDFs")
    args = parser.parse_args()

    if args.list is not None:
        if args.list not in config.PDMO_INDEX_URLS:
            sys.exit(f"no index URL configured for {args.list}; "
                     f"known years: {sorted(config.PDMO_INDEX_URLS)}")
        list_links(args.list)
    elif args.download:
        download_samples(args.download)
    else:
        dump_structure(args.dump)


if __name__ == "__main__":
    main()
