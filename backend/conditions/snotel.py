"""
NRCS SNOTEL fetcher — daily 3AM Pacific via APScheduler.

Fetches snow water equivalent (WTEQ) and snow depth (SNWD) for a single
SNOTEL station triplet (e.g. '679:WA:SNTL').
Wrapped with the snotel_breaker circuit breaker.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import snotel_breaker
from conditions.normalizer import normalize_snotel

log = logging.getLogger(__name__)

_STATION_DATA_URL = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)


async def fetch_snotel(station_triplet: str) -> dict | None:
    """
    Fetch current snowpack data for a SNOTEL station triplet.

    Returns normalised data dict, or None when the circuit is open.
    """
    try:
        raw = await _fetch(station_triplet)
        result = normalize_snotel(
            raw, station_id=station_triplet, fetched_at=datetime.now(tz=timezone.utc)
        )
        if result["snow_water_equivalent_in"] is None and result["snow_depth_in"] is None:
            log.warning("snotel_empty_response", extra={"source": "snotel", "station": station_triplet})
            return None
        return result
    except pybreaker.CircuitBreakerError:
        log.warning(
            "circuit_open",
            extra={"source": "snotel", "station": station_triplet},
        )
        return None


@snotel_breaker
async def _fetch(station_triplet: str) -> list:
    today = date.today()
    # SNOTEL stations report 1-3 days behind; look back 7 days to ensure we
    # capture the latest reading even when recent dates have no data yet.
    seven_days_ago = today - timedelta(days=7)
    params = {
        "stationTriplets": station_triplet,
        "elements": "WTEQ,SNWD",
        "beginDate": seven_days_ago.isoformat(),
        "endDate": today.isoformat(),
        "centralTendencyType": "MEDIAN",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_STATION_DATA_URL, params=params)
        resp.raise_for_status()
        return resp.json()
