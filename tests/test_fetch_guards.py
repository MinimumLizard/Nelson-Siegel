"""Guards against the pipeline's worst failure mode: green, quiet and wrong.

treasury.gov.lk sits behind a Sucuri firewall that answers a client it
dislikes with HTTP 307 and a JavaScript challenge page instead of the
report. Nothing about that looks like an error: `raise_for_status()` ignores
3xx and the challenge is valid HTML. Left alone it would reach the index
parser, yield no rows, and let a run report success having fetched nothing.
"""

import datetime as dt

import pytest
import requests

from dashboard import build
from pipeline import fetch, ingest


class _Response:
    """Just enough of requests.Response for the content check."""

    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content


# ---------------------------------------------------------------------------
# A challenge page must never reach a parser
# ---------------------------------------------------------------------------

def test_an_unfollowed_redirect_is_not_content():
    """requests follows ordinary redirects itself, so a response still sitting
    at 3xx is one the server would not complete — the firewall's calling card."""
    with pytest.raises(requests.RequestException, match="307"):
        fetch._reject_if_not_content(_Response(307, b"<html>You are being redirected"))


def test_a_challenge_body_is_not_content():
    body = (b"<html><title>You are being redirected...</title>"
            b"<script>var sucuri_cloudproxy_js='';</script></html>")
    with pytest.raises(requests.RequestException, match="bot-challenge"):
        fetch._reject_if_not_content(_Response(200, body))


def test_real_content_passes_untouched():
    fetch._reject_if_not_content(_Response(200, b"<html><table><tr><td>report</td>"))
    # A large binary report can never trip the marker sniff, which only looks
    # at small bodies — an .xls could contain those bytes by coincidence.
    big = b"\xd0\xcf\x11\xe0" + b"x" * 9000 + b"sucuri_cloudproxy"
    fetch._reject_if_not_content(_Response(200, big))


# ---------------------------------------------------------------------------
# An index that suddenly lists nothing is broken, not quiet
# ---------------------------------------------------------------------------

def _worklist_with(monkeypatch, per_index):
    """Run build_worklist with a stub index whose parse result we choose."""
    monkeypatch.setattr(fetch, "polite_get", lambda url: _Response(200, b"x"))
    monkeypatch.setattr(ingest.fetch, "polite_get", lambda url: _Response(200, b"x"))
    sources = [("stub", {year: f"http://x/{year}" for year in per_index},
                lambda content: _worklist_with.answer.pop(0))]
    monkeypatch.setattr(ingest, "INDEX_SOURCES", sources)
    _worklist_with.answer = list(per_index.values())
    return ingest.build_worklist()


def test_every_index_empty_raises_rather_than_reporting_success(monkeypatch):
    """This is the case the firewall produces: eight pages that parse to
    nothing. Continuing would commit a run that fetched precisely nothing."""
    with pytest.raises(RuntimeError, match="unreachable or empty"):
        _worklist_with(monkeypatch, {2025: [], 2026: []})


def test_one_empty_index_is_survivable(monkeypatch):
    """A year that has not started yet legitimately lists nothing, so a single
    empty page must not stop a run that is otherwise fine."""
    entry = ingest.scrape_index.IndexEntry(
        url="http://x/f.xls", kind="daily_summary",
        label_date=dt.date(2026, 9, 18), posted_date=dt.date(2026, 9, 18))
    items = _worklist_with(monkeypatch, {2025: [], 2026: [entry]})
    assert [item.entry.url for item in items] == ["http://x/f.xls"]


# ---------------------------------------------------------------------------
# Saying a day has no trades without saying when they arrive reads as a fault
# ---------------------------------------------------------------------------

def test_friday_trades_are_due_on_monday():
    assert build._next_business_day("2026-09-18") == dt.date(2026, 9, 21)   # Fri -> Mon
    assert build._next_business_day("2026-09-17") == dt.date(2026, 9, 18)   # Thu -> Fri
    assert build._next_business_day("2026-09-19") == dt.date(2026, 9, 21)   # Sat -> Mon
