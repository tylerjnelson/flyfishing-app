"""
NOAA NWS fetcher — real-time, triggered at session open.

Two-step fetch per §4.1:
  Step 1: GET https://api.weather.gov/points/{lat},{lon} → forecast URLs
  Step 2: GET daily forecast URL + hourly forecast URL

Wrapped with the noaa_nws_breaker circuit breaker.
"""

import logging
from datetime import datetime, timezone

import httpx
import pybreaker

from conditions.circuit_breaker import noaa_nws_breaker
from conditions.normalizer import normalize_noaa_nws

log = logging.getLogger(__name__)

_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
_HEADERS = {"User-Agent": "FlyFishWA/1.0 (contact via server admin)"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


async def fetch_noaa_nws(lat: float, lon: float) -> dict | None:
    """
    Fetch current conditions and 7-day forecast for a lat/lon.

    Returns normalised data dict, or None when the circuit is open.
    """
    try:
        raw = await _fetch(lat, lon)
        return normalize_noaa_nws(raw, fetched_at=datetime.now(tz=timezone.utc))
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "noaa_nws", "lat": lat, "lon": lon})
        return None


@noaa_nws_breaker
async def _fetch(lat: float, lon: float) -> dict:
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        # Step 1 — resolve forecast endpoint URLs for this location
        points_resp = await client.get(_POINTS_URL.format(lat=round(lat, 4), lon=round(lon, 4)))
        points_resp.raise_for_status()
        props = points_resp.json().get("properties", {})

        forecast_url = props.get("forecast")
        hourly_url = props.get("forecastHourly")
        obs_stations_url = props.get("observationStations")

        # Step 2 — fetch forecast and current observation in parallel
        daily_resp, hourly_resp, stations_resp = await _fetch_all(
            client, forecast_url, hourly_url, obs_stations_url
        )

    daily_periods = daily_resp.get("properties", {}).get("periods", [])
    hourly_periods = hourly_resp.get("properties", {}).get("periods", [])

    # Fetch most recent observation from first station
    current_obs = {}
    station_list = stations_resp.get("features", [])
    if station_list:
        station_url = station_list[0].get("id", "")
        obs_url = f"{station_url}/observations/latest"
        async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
            obs_resp = await client.get(obs_url)
            if obs_resp.is_success:
                current_obs = obs_resp.json().get("properties", {})

    return {
        "current_obs": current_obs,
        "daily": daily_periods,
        "hourly": hourly_periods,
    }


async def _fetch_all(
    client: httpx.AsyncClient,
    forecast_url: str,
    hourly_url: str,
    stations_url: str,
) -> tuple[dict, dict, dict]:
    import asyncio

    async def get(url: str) -> dict:
        if not url:
            return {}
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

    return await asyncio.gather(get(forecast_url), get(hourly_url), get(stations_url))
