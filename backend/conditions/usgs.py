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
from conditions.fetch_errors import log_fetch_failure
from conditions.noaa_nwrfc import fetch_nwps_observed, resolve_gauge_id
from conditions.normalizer import normalize_usgs

log = logging.getLogger(__name__)

# Was "api.waterservices.usgs.gov" — a host that doesn't exist — from the first
# commit until 2026-10-11, so this fetcher had never returned data.
_BASE_URL = "https://waterservices.usgs.gov/nwis/iv/"
_PARAMS = "00060,00065,00010"  # discharge (CFS), gauge height, water temp
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


async def fetch_usgs_gauge(site_id: str) -> dict | None:
    """
    Fetch the current flow reading for one USGS gauge site.

    If USGS has no usable discharge (discontinued gauge, outage) and the site
    has an NWPS gauge (noaa_nwrfc._USGS_TO_NWRFC_LID), fall back to NWPS
    observed flow. The result has the same shape, with provider=
    "noaa_nwps_observed". Returns None when neither source has data (logged).
    """
    result = await _fetch_usgs_reading(site_id)
    if result is not None and result["cfs"] is not None:
        return result

    lid = resolve_gauge_id(site_id)
    if lid:
        fallback = await fetch_nwps_observed(lid)
        if fallback is not None:
            if result is not None and result.get("temp_f") is not None:
                fallback["temp_f"] = result["temp_f"]  # keep USGS temp if only Q was missing
            log.info("usgs_fallback_nwps", extra={"source": "usgs", "site_id": site_id, "nwps_gauge_id": lid})
            return fallback
    return result


async def _fetch_usgs_reading(site_id: str) -> dict | None:
    """USGS only. None on failure or when neither discharge nor stage is usable."""
    try:
        raw = await _fetch(site_id)
        result = normalize_usgs(raw, fetched_at=datetime.now(tz=timezone.utc))
        dropped = result.pop("dropped", {})
        if result["cfs"] is None and result["gauge_height_ft"] is None:
            # Expected (INFO) for discontinued gauges that have an NWPS fallback.
            level = logging.INFO if resolve_gauge_id(site_id) else logging.WARNING
            log.log(level, "usgs_empty_response", extra={"source": "usgs", "site_id": site_id, "dropped": dropped})
            return None
        if dropped:
            log.info("usgs_values_dropped", extra={"source": "usgs", "site_id": site_id, "dropped": dropped})
        return result
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "usgs", "site_id": site_id})
        return None
    except Exception as exc:
        log_fetch_failure("usgs", exc, site_id=site_id)
        return None


@usgs_breaker
async def _fetch(site_id: str) -> dict:
    params = {
        "sites": site_id,
        "format": "json",
        "parameterCd": _PARAMS,
        # Without a period the service returns only the latest value, and the
        # normalizer's trend (latest vs ~1h prior) needs >=5 readings.
        "period": "PT2H",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()
