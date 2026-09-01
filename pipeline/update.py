"""`python -m pipeline.update` — incremental backfill for daily use.

Re-scrapes the index pages (cheap: four requests) and ingests only files
that are not yet parsed ok — new business days, plus any file that failed
last time and might parse after a fix. Run it as often as you like.
"""

import logging
import sys

from pipeline import ingest


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    tallies = ingest.run(only_new=True)
    print(f"update finished: {tallies['ok']} new files parsed ok, "
          f"{tallies['failed']} failed, {tallies['skipped']} already done")
    sys.exit(1 if tallies["failed"] and not tallies["ok"] else 0)


if __name__ == "__main__":
    main()
