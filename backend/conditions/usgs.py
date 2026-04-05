"""
USGS Water Services fetcher — real-time, triggered at session open.

Fetches CFS (discharge), gauge height, and water temperature for a single
USGS gauge site.  Wrapped with the usgs_breaker circuit breaker.
"""

import logging
from datetime import datetime, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import usgs_breaker
from conditions.normalizer import normalize_usgs

log = logging.getLogger(__name__)

_BASE_URL = "https://api.waterservices.usgs.gov/nwis/iv/"
_PARAMS = "00060,00065,00010"  # discharge (CFS), gauge height, water temp
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


async def fetch_usgs_gauge(site_id: str) -> dict | None:
    """
    Fetch instantaneous values for one USGS gauge site.

    Returns normalised data dict, or None when the circuit is open (caller
    should fall back to the last conditions_cache entry and set stale=True).
    """
    try:
        raw = await _fetch(site_id)
        return normalize_usgs(raw, fetched_at=datetime.now(tz=timezone.utc))
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "usgs", "site_id": site_id})
        return None


@usgs_breaker
async def _fetch(site_id: str) -> dict:
    params = {
        "sites": site_id,
        "format": "json",
        "parameterCd": _PARAMS,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()
