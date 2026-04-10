"""
NOAA NWRFC (Northwest River Forecast Center) fetcher — 2-hour interval.

Fetches 3–10 day river flow forecasts per gauge.  gauge_id maps from a
spot's usgs_site_ids via _USGS_TO_NWRFC_LID (hardcoded — NWPS list endpoint
does not reliably filter by usgsId).
Wrapped with the noaa_nwrfc_breaker circuit breaker.

LID mapping validated 2026-04-10 against https://api.water.noaa.gov/nwps/v1/gauges/{lid}
"""

import logging
from datetime import datetime, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import noaa_nwrfc_breaker
from conditions.normalizer import normalize_noaa_nwrfc

log = logging.getLogger(__name__)

_STAGEFLOW_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{gauge_id}/stageflow"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)

# USGS site ID → NWRFC LID mapping for WA gauges.
# Sourced from GET /nwps/v1/gauges/{lid} → usgsId field (2026-04-10).
# The NWPS list endpoint's ?usgsId= filter is unreliable (silently ignored or
# returns Not Found depending on ID).  Hardcoded mapping is faster and stable.
_USGS_TO_NWRFC_LID: dict[str, str] = {
    "12505000": "PARW1",   # Yakima River near Parker
    "12510500": "KIOW1",   # Yakima River at Kiona
    "12462500": "MONW1",   # Wenatchee River at Monitor
    "12459000": "PESW1",   # Wenatchee River at Peshastin
    "12447383": "MZMW1",   # Methow River above Goat Creek near Mazama
    "12449950": "PATW1",   # Methow River near Pateros
    "12149000": "CRNW1",   # Snoqualmie River near Carnation
    "12144500": "SQUW1",   # Snoqualmie River at Snoqualmie Falls
    "12134500": "GLBW1",   # Skykomish River near Gold Bar
    "12131500": "SSSW1",   # South Fork Skykomish River at Skykomish
    "12181000": "SRMW1",   # Skagit River at Marblemount
    "12194000": "CONW1",   # Skagit River near Concrete
    "12447200": "OKMW1",   # Okanogan River at Malott
}


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


def resolve_gauge_id(usgs_site_id: str) -> str | None:
    """
    Return the NWRFC LID for a USGS site ID, or None if not in the mapping.

    Previously attempted a live NWPS API lookup (?usgsId= filter) which proved
    unreliable — the filter is silently ignored for some IDs and returns
    Not Found for others.  Replaced with a hardcoded mapping (see _USGS_TO_NWRFC_LID).
    """
    lid = _USGS_TO_NWRFC_LID.get(str(usgs_site_id))
    if not lid:
        log.debug("nwrfc_no_lid_for_usgs", extra={"usgs_site_id": usgs_site_id})
    return lid


@noaa_nwrfc_breaker
async def _fetch_stageflow(gauge_id: str) -> dict:
    url = _STAGEFLOW_URL.format(gauge_id=gauge_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
