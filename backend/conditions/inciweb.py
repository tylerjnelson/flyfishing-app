"""
InciWeb wildfire fetcher — 2-hour interval via APScheduler.

Fetches active wildfire incidents and filters to Washington fires.
Wrapped with the inciweb_breaker circuit breaker.

NOTE: verify https://inciweb.nwcg.gov/incidents.json is current at Phase 1
exit — this URL migrated from inciweb.wildfire.gov (§4.2).
"""

import logging
from datetime import datetime, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import inciweb_breaker
from conditions.normalizer import normalize_inciweb

log = logging.getLogger(__name__)

_INCIDENTS_URL = "https://inciweb.nwcg.gov/incidents.json"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)


async def fetch_inciweb() -> dict | None:
    """
    Fetch active WA wildfire incidents.

    Returns normalised data dict, or None when the circuit is open.
    """
    try:
        incidents = await _fetch()
        return normalize_inciweb(incidents, fetched_at=datetime.now(tz=timezone.utc))
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "inciweb"})
        return None


@inciweb_breaker
async def _fetch() -> list:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_INCIDENTS_URL)
        resp.raise_for_status()
        data = resp.json()
        # API may return a list directly or {"incidents": [...]}
        if isinstance(data, list):
            return data
        return data.get("incidents", data.get("data", []))
