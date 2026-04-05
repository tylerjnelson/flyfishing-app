"""
WDFW stocking fetcher — daily 3AM Pacific via APScheduler.

Fetches stocking history and upcoming plants from the WDFW / Data.WA.gov
Socrata SODA API.  Paginates with $limit=1000 until all records for the
current year are retrieved.

No API key required.  Wrapped with the wdfw_stocking_breaker circuit breaker.
"""

import logging
from datetime import datetime, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import wdfw_stocking_breaker
from conditions.normalizer import normalize_wdfw_stocking

log = logging.getLogger(__name__)

_SOCRATA_URL = "https://data.wa.gov/resource/9b4n-hquz.json"
_PAGE_SIZE = 1000
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=5.0, pool=5.0)


async def fetch_wdfw_stocking(year: int | None = None) -> list[dict] | None:
    """
    Fetch all stocking records for the given year (defaults to current year).

    Returns a flat list of normalised stocking dicts for upsert into
    stocking_events, or None when the circuit is open.
    """
    if year is None:
        year = datetime.now(tz=timezone.utc).year
    try:
        raw = await _fetch_all(year)
        return normalize_wdfw_stocking(raw, fetched_at=datetime.now(tz=timezone.utc))
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "wdfw_stocking", "year": year})
        return None


@wdfw_stocking_breaker
async def _fetch_all(year: int) -> list:
    records = []
    offset = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while True:
            params = {
                "$limit": _PAGE_SIZE,
                "$offset": offset,
                "$where": f"year = {year}",
                "$order": ":id",
            }
            resp = await client.get(_SOCRATA_URL, params=params)
            resp.raise_for_status()
            page = resp.json()
            records.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    return records
