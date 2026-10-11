"""The index-page years must roll over on their own.

The PDMO publishes each year on its own page. A hand-typed list of years
stops being complete on 1 January without anything failing: last year's page
still loads and still lists files, so the empty-index guard never fires and
every run reports success while ingesting nothing new. These tests pin the
computed list, and that the January state it produces — the new year's pages
live but empty — is one a run survives.
"""

import datetime as dt
import logging

from pipeline import config, ingest


def test_the_new_year_is_added_on_the_first_of_january():
    assert list(config.index_years(dt.date(2026, 12, 31))) == [2025, 2026]
    assert list(config.index_years(dt.date(2027, 1, 1))) == [2025, 2026, 2027]


def test_every_index_section_covers_exactly_the_computed_years():
    expected = list(config.index_years(dt.date.today()))
    for urls in (config.PDMO_INDEX_URLS, config.TRADE_SUMMARY_INDEX_URLS,
                 config.BOND_AUCTION_INDEX_URLS, config.BOND_ISSUANCE_INDEX_URLS):
        assert sorted(urls) == expected
        # Same URL pattern as before: the year is the last path segment.
        for year, url in urls.items():
            assert url.startswith(config.PDMO_BASE + "/web/")
            assert url.endswith(f"/section/{year}")


def test_early_january_runs_on_last_years_pages(monkeypatch, caplog):
    """Early January as it will actually look: all four sections gain a 2027
    page that answers but lists nothing yet, while 2025 and 2026 still list
    files. Four empty pages out of twelve must warn and carry on — not raise,
    which would stop the curve and dashboard steps too."""

    class _Response:
        status_code = 200

        def __init__(self, url):
            self.content = url.encode()

    def parse(content):
        url = content.decode()
        if url.endswith("/2027"):
            return []
        return [ingest.scrape_index.IndexEntry(
            url=url + "/f.xls", kind="daily_summary",
            label_date=dt.date(2026, 12, 31), posted_date=dt.date(2026, 12, 31))]

    years = config.index_years(dt.date(2027, 1, 5))
    sources = [(label, {y: f"http://x/{label}/{y}" for y in years}, parse)
               for label in ("daily", "trade-summary", "bond-issuance", "bond-auction")]
    monkeypatch.setattr(ingest, "INDEX_SOURCES", sources)
    monkeypatch.setattr(ingest.fetch, "polite_get", _Response)

    with caplog.at_level(logging.WARNING, logger=ingest.log.name):
        items = ingest.build_worklist()

    assert len(items) == 8                       # 4 sections x 2025, 2026
    empty = [r for r in caplog.records if "ZERO files" in r.getMessage()]
    assert len(empty) == 4                       # each 2027 page, warned about
    assert all("2027" in r.getMessage() for r in empty)
