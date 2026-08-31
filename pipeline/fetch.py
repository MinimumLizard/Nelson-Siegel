"""Polite HTTP: the only module that talks to the network.

Every download in the pipeline goes through `polite_get`, which enforces the
politeness rules from config.py in one place:

  * a shared requests.Session with an honest User-Agent,
  * a mandatory pause between consecutive requests (module-level bookkeeping,
    so the rule holds even across different callers),
  * retries with exponential backoff on transient failures.

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


def polite_get(url: str) -> requests.Response:
    """GET a URL with delay + retries. Raises after MAX_RETRIES failures."""
    last_error: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        _wait_politely()
        try:
            response = _session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
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
