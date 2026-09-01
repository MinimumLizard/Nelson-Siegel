"""One-off diagnostic tool for the "important first step": look at real data
BEFORE designing the parser and schema.

This is deliberately not part of the production pipeline. It has three modes,
meant to be run in order:

1. List every PDF link an index page exposes (and save the raw HTML so the
   messy markup can be studied offline):

       python -m pipeline.inspect_samples --list 2026

2. Download a handful of chosen sample PDFs into data/samples/:

       python -m pipeline.inspect_samples --download <url> <url> ...

3. Dump the internal structure of downloaded files — which is what the parser
   and final schema get designed around:

       python -m pipeline.inspect_samples --dump data/samples/*.pdf

   Inspection finding: despite the .pdf-looking /api/file/ URLs, the daily
   summary and volumes reports are legacy Excel .xls workbooks (the server
   sends content-type application/vnd.ms-excel); only the secondary-market
   trade summary is a real PDF. --dump sniffs the first bytes of each file
   and picks the right dumper, whatever the file extension says.

There is also --list-extra KEY for the additional index pages recorded in
config.PDMO_EXTRA_INDEX_URLS (trade summaries, auction results, ...).
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


def list_extra(key: str) -> None:
    """Fetch one of the extra index pages and print every /api/file/ link.

    These pages differ from the daily-report pages: the date usually sits in
    the link text itself, but some entries are just labelled "Download" with
    the date in an ancestor element — so we print both the anchor text and
    the surrounding text to see where the date actually lives.
    """
    url = config.PDMO_EXTRA_INDEX_URLS[key]
    response = fetch.polite_get(url)
    INDEX_HTML_DIR.mkdir(parents=True, exist_ok=True)
    saved = INDEX_HTML_DIR / f"extra_{key}.html"
    saved.write_bytes(response.content)
    print(f"# saved raw HTML to {saved}")

    soup = BeautifulSoup(response.content, "html.parser")
    count = 0
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/api/file/" not in href:
            continue
        count += 1
        text = " ".join(anchor.get_text(" ", strip=True).split())
        # Walk up until we find an element whose text adds context beyond the
        # anchor itself (that is where "Download"-style links keep their date).
        context = text
        node = anchor
        while node.parent is not None and context == text:
            node = node.parent
            context = " ".join(node.get_text(" ", strip=True).split())[:160]
        print(f"{href}\n    text:    {text!r}\n    context: {context!r}")
    print(f"# {count} /api/file/ links found on {url}")


def download_samples(urls: list[str]) -> None:
    """Download specific PDF URLs into data/samples/ (named by their uuid)."""
    for url in urls:
        uuid = url.rstrip("/").rsplit("/", maxsplit=1)[-1]
        destination = SAMPLES_DIR / f"{uuid}.pdf"
        was_downloaded = fetch.download_file(url, destination)
        status = "downloaded" if was_downloaded else "already cached"
        print(f"{status}: {destination}")


def dump_xls(path: str) -> None:
    """Print every sheet of a legacy .xls workbook: dimensions + cell values."""
    import xlrd  # imported here so --list/--download work without it

    book = xlrd.open_workbook(path)
    print(f"sheets: {book.sheet_names()}")
    for sheet in book.sheets():
        print(f"\n--- sheet {sheet.name!r}: {sheet.nrows} rows x {sheet.ncols} cols")
        for row_number in range(min(sheet.nrows, 40)):
            values = sheet.row_values(row_number)
            while values and values[-1] in ("", None):  # drop trailing blanks
                values.pop()
            if values:
                print(f"  r{row_number}: {values}")
        if sheet.nrows > 40:
            print(f"  ... ({sheet.nrows - 40} more rows)")


def dump_pdf(path: str) -> None:
    """Print what pdfplumber sees inside a real PDF: tables, rows, raw text."""
    import pdfplumber  # imported here so --list/--download work without it

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


# Magic bytes that identify an OLE2 compound document (legacy .xls format).
OLE2_MAGIC = b"\xd0\xcf\x11\xe0"


def dump_structure(paths: list[str]) -> None:
    """Dispatch each file to the right dumper based on its actual content."""
    for path in paths:
        print(f"\n{'=' * 78}\nFILE: {path}\n{'=' * 78}")
        head = Path(path).open("rb").read(8)
        if head.startswith(OLE2_MAGIC):
            dump_xls(path)
        elif head.startswith(b"%PDF"):
            dump_pdf(path)
        else:
            print(f"unrecognised file type (first bytes: {head!r})")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", type=int, metavar="YEAR",
                       help="fetch YEAR's index page and list all PDF links")
    group.add_argument("--list-extra", metavar="KEY",
                       choices=sorted(config.PDMO_EXTRA_INDEX_URLS),
                       help="fetch one of the extra index pages and list its "
                            f"file links; keys: {sorted(config.PDMO_EXTRA_INDEX_URLS)}")
    group.add_argument("--download", nargs="+", metavar="URL",
                       help="download sample files into data/samples/")
    group.add_argument("--dump", nargs="+", metavar="FILE",
                       help="show table structure of downloaded files (.xls or PDF)")
    args = parser.parse_args()

    if args.list is not None:
        if args.list not in config.PDMO_INDEX_URLS:
            sys.exit(f"no index URL configured for {args.list}; "
                     f"known years: {sorted(config.PDMO_INDEX_URLS)}")
        list_links(args.list)
    elif args.list_extra:
        list_extra(args.list_extra)
    elif args.download:
        download_samples(args.download)
    else:
        dump_structure(args.dump)


if __name__ == "__main__":
    main()
