"""
NRCS SNOTEL fetcher — daily 3AM Pacific via APScheduler.

Fetches snow water equivalent (WTEQ) and snow depth (SNWD) for a single
SNOTEL station triplet (e.g. '679:WA:SNTL').
Wrapped with the snotel_breaker circuit breaker.
"""

import logging
import statistics
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
        if result["pct_of_median"] is None and result["snow_water_equivalent_in"] is not None:
            result["pct_of_median"] = await _compute_historical_median(
                station_triplet, result["snow_water_equivalent_in"]
            )
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


async def _compute_historical_median(station_triplet: str, current_swe: float) -> float | None:
    """
    Fallback for stations where NRCS doesn't publish a 30-year median.
    Fetches 10 years of WTEQ history, filters to the current month/day,
    and computes a Python median. Requires ≥3 historical points.
    Not circuit-broken — failure here is non-fatal.
    """
    today = date.today()
    params = {
        "stationTriplets": station_triplet,
        "elements": "WTEQ",
        "beginDate": (today - timedelta(days=3653)).isoformat(),
        "endDate": (today - timedelta(days=365)).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_STATION_DATA_URL, params=params)
            resp.raise_for_status()
            raw = resp.json()
    except Exception as exc:
        log.warning(
            "snotel_historical_fetch_failed",
            extra={"source": "snotel", "station": station_triplet, "error": str(exc)},
        )
        return None

    station_data = raw[0].get("data", []) if raw else []
    elements_map = {e["stationElement"]["elementCode"]: e for e in station_data}
    series = elements_map.get("WTEQ", {}).get("values", [])

    historical_values = []
    for entry in series:
        date_str = entry.get("date")
        value = entry.get("value")
        if date_str is None or value is None:
            continue
        try:
            entry_date = date.fromisoformat(date_str)
            if entry_date.month == today.month and entry_date.day == today.day:
                historical_values.append(float(value))
        except (ValueError, TypeError):
            pass

    if len(historical_values) < 3:
        log.debug(
            "snotel_historical_median_insufficient_data",
            extra={"source": "snotel", "station": station_triplet, "points": len(historical_values)},
        )
        return None

    computed_median = statistics.median(historical_values)
    if computed_median == 0:
        return None

    pct = round(current_swe / computed_median * 100, 1)
    log.info(
        "snotel_historical_median_computed",
        extra={"source": "snotel", "station": station_triplet, "pct_of_median": pct, "points": len(historical_values)},
    )
    return pct
