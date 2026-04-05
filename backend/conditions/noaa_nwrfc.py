"""
NOAA NWRFC (Northwest River Forecast Center) fetcher — 2-hour interval.

Fetches 3–10 day river flow forecasts per gauge.  gauge_id maps from a
spot's usgs_site_ids via NWPS station lookup.
Wrapped with the noaa_nwrfc_breaker circuit breaker.
"""

import logging
from datetime import datetime, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import noaa_nwrfc_breaker
from conditions.normalizer import normalize_noaa_nwrfc

log = logging.getLogger(__name__)

_STAGEFLOW_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{gauge_id}/stageflow"
_STATIONS_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)


async def fetch_noaa_nwrfc(gauge_id: str) -> dict | None:
    """
    Fetch river flow forecast for a single NWPS gauge ID.

    Returns normalised data dict, or None when the circuit is open.
    """
    try:
        raw = await _fetch_stageflow(gauge_id)
        return normalize_noaa_nwrfc(
            raw, gauge_id=gauge_id, fetched_at=datetime.now(tz=timezone.utc)
        )
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "noaa_nwrfc", "gauge_id": gauge_id})
        return None


async def resolve_gauge_id(usgs_site_id: str) -> str | None:
    """
    Look up the NWPS gauge ID for a USGS site ID via the NWPS stations catalog.

    The NWPS uses its own gauge IDs (typically matching USGS station IDs with
    different zero-padding).  Returns None if no match found.
    """
    try:
        return await _resolve(usgs_site_id)
    except (httpx.HTTPError, KeyError, StopIteration):
        log.warning(
            "nwrfc_gauge_lookup_failed",
            extra={"usgs_site_id": usgs_site_id},
        )
        return None


@noaa_nwrfc_breaker
async def _fetch_stageflow(gauge_id: str) -> dict:
    url = _STAGEFLOW_URL.format(gauge_id=gauge_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _resolve(usgs_site_id: str) -> str | None:
    """Query NWPS stations catalog and match on usgsId field."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_STATIONS_URL, params={"usgsId": usgs_site_id})
        resp.raise_for_status()
        data = resp.json()
        gauges = data if isinstance(data, list) else data.get("gauges", [])
        match = next(
            (g for g in gauges if str(g.get("usgsId")) == str(usgs_site_id)), None
        )
        if match:
            return match.get("lid") or match.get("id")
        return None
