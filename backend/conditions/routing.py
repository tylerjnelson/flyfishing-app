"""
HERE Routing integration — §6.3.

Uses planning mode (departureTime=any, traffic-free) to avoid HERE's premium
Time-Aware Routing SKU — see the BILLING NOTE in _here_request().

get_drive_time() is the public entry point. On circuit breaker trip or timeout
> 3 s, falls back to Haversine straight-line × 1.4 road factor.

IMPORTANT: Haversine fallback values are for internal filtering ONLY.
They must never be shown to the user as drive time. context_builder.py
sets drive_time_unavailable=True on the session when fallback fires;
the frontend renders a persistent banner and labels distances as
straight-line mileage — never fabricated drive minutes.
"""

import asyncio
import logging
import math
from datetime import datetime

import httpx
import pybreaker

from conditions.circuit_breaker import routing_breaker
from config import settings

log = logging.getLogger(__name__)

_HERE_ROUTING_URL = "https://router.hereapi.com/v8/routes"
_TIMEOUT = httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0)
_ROAD_FACTOR = 1.4      # straight-line → estimated road distance
_AVG_SPEED_KMH = 60.0  # used only in Haversine fallback


# ---------------------------------------------------------------------------
# Distance helpers (exported for pre-filter use in context_builder.py)
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points in kilometres."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in miles — shown when HERE is unavailable."""
    return round(haversine_km(lat1, lon1, lat2, lon2) * 0.621371, 1)


def haversine_drive_minutes(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> int:
    """
    Estimate drive time: Haversine × 1.4 road factor at 60 km/h.
    For internal filtering only — never shown to the user as drive time.
    """
    road_km = haversine_km(lat1, lon1, lat2, lon2) * _ROAD_FACTOR
    return max(1, round(road_km / _AVG_SPEED_KMH * 60))


# ---------------------------------------------------------------------------
# HERE Time-Aware Routing
# ---------------------------------------------------------------------------

@routing_breaker
async def _here_request(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    departure_time: datetime,
) -> int:
    """
    HERE Routing API call in planning mode (departureTime=any, traffic-free).
    Wrapped by routing_breaker (fail_max=2, reset_timeout=60 per §6.3).
    Returns drive time in minutes.

    NOTE: planning mode is used deliberately to stay off HERE's premium
    Time-Aware Routing SKU — see the BILLING NOTE on the params below.
    """
    # BILLING NOTE — departureTime=any ("planning mode"), intentionally NOT the
    # real departure time. Sending a concrete departureTime makes HERE bill the
    # call as "Time-Aware Routing", a PREMIUM SKU (this caused unexpected charges,
    # see spec §19.6). "any" computes a traffic-free route in the non-premium,
    # free-tier-eligible category. Trade-off: durations no longer reflect
    # historical traffic. The `departure_time` arg is retained (callers/signature
    # unchanged, still used elsewhere in the pipeline) so this is a one-line
    # revert if a HERE spend cap is configured and time-aware routing is wanted.
    params = {
        "transportMode": "car",
        "origin": f"{lat1},{lon1}",
        "destination": f"{lat2},{lon2}",
        "departureTime": "any",
        "return": "summary",
        "apikey": settings.here_api_key,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_HERE_ROUTING_URL, params=params)
        resp.raise_for_status()
    duration_s = resp.json()["routes"][0]["sections"][0]["summary"]["duration"]
    return max(1, round(duration_s / 60))


async def get_drive_time(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    departure_time: datetime,
) -> tuple[int, bool]:
    """
    Returns (drive_minutes, is_fallback).

    is_fallback=True when HERE is unavailable (circuit open or > 3 s timeout).
    Callers must surface the drive-time-unavailable UI banner and label any
    distances as straight-line mileage — never present fallback minutes as
    drive time to the user.
    """
    # HERE kill-switch (batch/benchmark only — spec §11.1). Return a deterministic
    # Haversine estimate as if it were a real drive time (is_fallback=False) so the
    # pipeline filters and renders normally without touching the HERE API.
    if settings.here_disabled:
        return haversine_drive_minutes(origin_lat, origin_lon, dest_lat, dest_lon), False

    try:
        minutes = await asyncio.wait_for(
            _here_request(origin_lat, origin_lon, dest_lat, dest_lon, departure_time),
            timeout=3.0,
        )
        log.debug("here_routing_ok", extra={"minutes": minutes})
        return minutes, False
    except Exception as exc:
        log.warning(
            "routing_fallback",
            extra={"reason": type(exc).__name__, "detail": str(exc)[:120]},
        )
        return haversine_drive_minutes(origin_lat, origin_lon, dest_lat, dest_lon), True
