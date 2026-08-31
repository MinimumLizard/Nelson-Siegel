"""`python -m pipeline.backfill` — scrape the PDMO index pages, download every
report PDF into data/raw/YYYY/, and parse them all into the database.

STATUS: stub. The scraper and parser are built only after real sample PDFs
have been inspected and their table structure confirmed (see
pipeline/inspect_samples.py). This file exists so the command-line surface is
fixed from day one.
"""

import sys


def main() -> None:
    sys.exit(
        "backfill: not implemented yet — waiting on sample-PDF inspection "
        "(see python -m pipeline.inspect_samples --help)."
    )


if __name__ == "__main__":
    main()
