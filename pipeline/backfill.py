"""`python -m pipeline.backfill` — ingest the ENTIRE archive, idempotently.

Scrapes every configured index page, downloads whatever is not already in
the data/raw/ cache (politely: sequential, 1.5s apart), parses everything,
and upserts into data/sgcp.sqlite. Safe to re-run at any time: cached files
are not re-downloaded and re-parsing overwrites rows with identical values.

Expect the first run to take ~20-30 minutes — ~550 files at a polite pace.
Files that cannot be parsed are recorded in the `files` table with
parse_status='failed' and a one-line reason; the run itself never stops.
"""

import logging
import sys

from pipeline import ingest


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    tallies = ingest.run(only_new=False)
    print(f"backfill finished: {tallies['ok']} parsed ok, "
          f"{tallies['failed']} failed (see the files table for reasons)")
    # A few failures are expected (the site has genuinely broken files);
    # only a fully failed run should look like an error to the shell.
    sys.exit(1 if tallies["ok"] == 0 else 0)


if __name__ == "__main__":
    main()
