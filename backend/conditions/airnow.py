"""
AirNow (EPA) fetcher — real-time, triggered at session open.

Fetches current AQI for a lat/lon within a 25-mile radius.
Wrapped with the airnow_breaker circuit breaker.
"""

import logging
from datetime import datetime, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import airnow_breaker
from conditions.normalizer import normalize_airnow
from config import settings

log = logging.getLogger(__name__)

_OBS_URL = "https://www.airnowapi.org/aq/observation/latLong/current/"
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


async def fetch_airnow(lat: float, lon: float) -> dict | None:
    """
    Fetch current AQI observation for a lat/lon.

    Returns normalised data dict, or None when the circuit is open.
    """
    try:
        raw = await _fetch(lat, lon)
        return normalize_airnow(raw, fetched_at=datetime.now(tz=timezone.utc))
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "airnow", "lat": lat, "lon": lon})
        return None


@airnow_breaker
async def _fetch(lat: float, lon: float) -> list:
    params = {
        "format": "application/json",
        "latitude": lat,
        "longitude": lon,
        "distance": 25,
        "API_KEY": settings.airnow_api_key,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_OBS_URL, params=params)
        resp.raise_for_status()
        return resp.json()
