"""Polite HTTP: the only module that talks to the network.

Every download in the pipeline goes through `polite_get`, which enforces the
politeness rules from config.py in one place:

  * a shared requests.Session with an honest User-Agent,
  * a mandatory pause between consecutive requests (module-level bookkeeping,
    so the rule holds even across different callers),
  * retries with exponential backoff on transient failures,
  * a refusal to hand back anything that is not the content we asked for.

That last one is not fussiness. treasury.gov.lk sits behind a Sucuri web
application firewall which, when it decides a client looks like a bot,
answers with **HTTP 307 and a JavaScript challenge page** instead of the
report. `raise_for_status()` ignores 3xx, and the challenge is valid HTML,
so without the check below a challenged run would sail through: the index
parser would find no rows in it, `build_worklist` would add nothing, and
the job would report success having fetched precisely nothing. That is the
worst failure this pipeline can have — green, quiet and wrong — so a
challenge is turned into a loud exception instead.

`download_file` adds idempotent caching: if the target file already exists it
returns immediately without touching the network, which is what makes
re-running the backfill cheap and safe.
"""

import hashlib
import logging
import time
from pathlib import Path

import requests

from pipeline import config

log = logging.getLogger(__name__)

# One session for the whole process: connection reuse is faster AND gentler
# on the server than opening a fresh connection per request.
_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT

# When we last hit the network — used to enforce the delay between requests.
_last_request_time = 0.0


def _wait_politely() -> None:
    """Sleep just long enough that requests are REQUEST_DELAY_SECONDS apart."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    remaining = config.REQUEST_DELAY_SECONDS - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_request_time = time.monotonic()


# Markers of an interstitial served INSTEAD of the content. Checked only on
# small HTML-ish bodies, so a genuine report can never trip them.
CHALLENGE_MARKERS = (b"sucuri_cloudproxy", b"You are being redirected",
                     b"Javascript is required")
CHALLENGE_SNIFF_BYTES = 8192


def _reject_if_not_content(response: requests.Response) -> None:
    """Raise unless this response really is the file or page we asked for."""
    # requests follows ordinary redirects itself, so anything still 3xx here
    # is a redirect the server would not complete — the WAF's calling card.
    if response.status_code >= 300:
        raise requests.RequestException(
            f"HTTP {response.status_code} with no usable redirect "
            f"(a bot challenge or an interstitial, not the content)")
    head = response.content[:CHALLENGE_SNIFF_BYTES]
    if len(response.content) <= CHALLENGE_SNIFF_BYTES:
        for marker in CHALLENGE_MARKERS:
            if marker in head:
                raise requests.RequestException(
                    f"a bot-challenge page was served instead of the content "
                    f"(matched {marker.decode()!r})")


def polite_get(url: str) -> requests.Response:
    """GET a URL with delay + retries. Raises after MAX_RETRIES failures."""
    last_error: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        _wait_politely()
        try:
            response = _session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            _reject_if_not_content(response)
            return response
        except requests.RequestException as error:
            last_error = error
            backoff = 2 ** (attempt + 1)  # 2s, 4s, 8s, 16s
            log.warning(
                "GET %s failed (attempt %d/%d): %s — retrying in %ds",
                url, attempt + 1, config.MAX_RETRIES, error, backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(f"GET {url} failed after {config.MAX_RETRIES} attempts") from last_error


def download_file(url: str, destination: Path) -> bool:
    """Download `url` to `destination` unless it is already cached.

    Returns True if a download actually happened, False if the file was
    already there. The write goes through a temporary ".part" file so an
    interrupted download can never leave a half-written PDF that a later run
    would mistake for a valid cache entry.
    """
    if destination.exists():
        log.debug("cached, skipping: %s", destination.name)
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    response = polite_get(url)
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.write_bytes(response.content)
    partial.rename(destination)  # rename is atomic: cache entries are all-or-nothing
    log.info("downloaded %s (%d bytes)", destination.name, len(response.content))
    return True


def sha256_of(path: Path) -> str:
    """Hex SHA-256 of a file — recorded in the `files` table for provenance."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
