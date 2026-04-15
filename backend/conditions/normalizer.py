"""
Normalizes raw fetcher output to the conditions_cache data shape and computes
the conditions_hash used as the response_cache key.

Hash formula (§2.4):
  MD5 of JSON-serialised {cfs, temp_f, turbidity_fnu, fetched_at} where
  fetched_at is rounded DOWN to the nearest fetch-interval boundary.

  Weather forecast and AirNow fields are intentionally excluded from the hash:
  their changes must not bust a cached LLM response for a spot whose river
  conditions are unchanged.

Fetch-interval boundaries:
  15 minutes — session-open (real-time) sources: USGS, NOAA NWS, AirNow
  120 minutes — scheduled sources: NOAA NWRFC, WDFW emergency, InciWeb,
                WDFW stocking, WTA, SNOTEL
"""

import hashlib
import json
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Interval constants (minutes)
# ---------------------------------------------------------------------------

INTERVAL_REALTIME = 15
INTERVAL_SCHEDULED = 120


# ---------------------------------------------------------------------------
# Timestamp rounding
# ---------------------------------------------------------------------------

def _round_down(dt: datetime, interval_minutes: int) -> datetime:
    """Round a UTC datetime down to the nearest interval boundary."""
    total_seconds = int(dt.timestamp())
    interval_seconds = interval_minutes * 60
    rounded = (total_seconds // interval_seconds) * interval_seconds
    return datetime.fromtimestamp(rounded, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Conditions hash
# ---------------------------------------------------------------------------

def compute_conditions_hash(
    *,
    cfs: float | None,
    temp_f: float | None,
    turbidity_fnu: float | None,
    fetched_at: datetime,
    interval_minutes: int,
) -> str:
    """
    Returns the MD5 hex digest used to key response_cache rows.

    Only river-condition fields are included (cfs, temp_f, turbidity_fnu).
    Weather and AirNow data are excluded by design — see module docstring.
    """
    boundary = _round_down(fetched_at, interval_minutes)
    payload = {
        "cfs": cfs,
        "temp_f": temp_f,
        "turbidity_fnu": turbidity_fnu,
        "fetched_at": boundary.isoformat(),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(serialized.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Per-source normalizers
# ---------------------------------------------------------------------------

def normalize_usgs(raw: dict, fetched_at: datetime) -> dict:
    """
    Normalizes a single USGS instantaneous-values API response for one gauge.

    Expected raw keys (parameterCd):
      00060 — discharge (CFS)
      00065 — gauge height (ft)
      00010 — water temperature (°C)
    """
    values = raw.get("value", {}).get("timeSeries", [])
    by_param: dict[str, float | None] = {}
    cfs_records: list = []
    for ts in values:
        code = ts.get("variable", {}).get("variableCode", [{}])[0].get("value")
        records = ts.get("values", [{}])[0].get("value", [])
        if records:
            try:
                by_param[code] = float(records[-1]["value"])
            except (ValueError, KeyError):
                by_param[code] = None
            if code == "00060":
                cfs_records = records

    cfs = by_param.get("00060")
    gauge_height_ft = by_param.get("00065")
    temp_c = by_param.get("00010")
    temp_f = round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None

    # Compute flow trend by comparing latest reading to ~1hr prior (4 steps back at 15-min intervals)
    trend = None
    if cfs is not None and len(cfs_records) >= 5:
        try:
            prior_cfs = float(cfs_records[-5]["value"])
            if prior_cfs > 0:
                pct_change = (cfs - prior_cfs) / prior_cfs
                if pct_change >= 0.05:
                    trend = "rising"
                elif pct_change <= -0.05:
                    trend = "dropping"
                else:
                    trend = "stable"
        except (ValueError, KeyError, ZeroDivisionError):
            trend = None

    return {
        "source": "usgs",
        "fetched_at": fetched_at.isoformat(),
        "cfs": cfs,
        "gauge_height_ft": gauge_height_ft,
        "temp_f": temp_f,
        "turbidity_fnu": None,  # USGS turbidity requires separate parameterCd 63680
        "trend": trend,
        "stale": False,
    }


def normalize_noaa_nws(raw: dict, fetched_at: datetime) -> dict:
    """
    Normalizes a NOAA NWS forecast payload.

    raw must contain:
      current_obs  — current observation dict (from points → observationStations)
      daily        — daily forecast periods list
      hourly       — hourly forecast periods list

    NWS returns temperature in Celsius (wmoUnit:degC) and wind speed in km/h
    (wmoUnit:km_h-1). Both are converted to imperial before storage.
    """
    obs = raw.get("current_obs", {})
    temp_c = obs.get("temperature", {}).get("value")
    temp_f = round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None
    wind_kmh = obs.get("windSpeed", {}).get("value")
    wind_mph = round(wind_kmh * 0.621371, 1) if wind_kmh is not None else None
    return {
        "source": "noaa_nws",
        "fetched_at": fetched_at.isoformat(),
        "current": {
            "temp_f": temp_f,
            "wind_speed_mph": wind_mph,
            "short_forecast": obs.get("textDescription"),
        },
        "daily_forecast": raw.get("daily", [])[:7],
        "hourly_forecast": raw.get("hourly", [])[:24],
        "stale": False,
    }


def normalize_airnow(raw: list, fetched_at: datetime) -> dict:
    """
    Normalizes AirNow observation list.  raw is the JSON array from the API.
    Uses the highest AQI value across all reported pollutants.
    """
    aqi = None
    category = None
    pollutant = None
    for obs in raw:
        val = obs.get("AQI")
        if val is not None and (aqi is None or val > aqi):
            aqi = val
            category = obs.get("Category", {}).get("Name")
            pollutant = obs.get("ParameterName")

    return {
        "source": "airnow",
        "fetched_at": fetched_at.isoformat(),
        "aqi": aqi,
        "category": category,
        "pollutant": pollutant,
        "stale": False,
    }


def normalize_noaa_nwrfc(raw: dict, gauge_id: str, fetched_at: datetime) -> dict:
    """
    Normalizes a NOAA NWPS stageflow forecast for a single gauge.

    Stageflow response shape: {observed: {...}, forecast: {data: [{validTime, primary, secondary}]}}
    primary = stage (ft), secondary = flow (kcfs).
    """
    forecasts = raw.get("forecast", {}).get("data", [])
    return {
        "source": "noaa_nwrfc",
        "fetched_at": fetched_at.isoformat(),
        "gauge_id": gauge_id,
        "forecast": forecasts,  # list of {validTime, primary (stage ft), secondary (flow kcfs)}
        "stale": False,
    }


def normalize_inciweb(incidents: list, fetched_at: datetime) -> dict:
    """
    Filters InciWeb incidents to Washington wildfires and normalises the list.
    """
    wa_fires = [
        {
            "id": inc.get("id"),
            "name": inc.get("name"),
            "incident_type": inc.get("incident_type"),
            "state": inc.get("state"),
            "latitude": inc.get("latitude"),
            "longitude": inc.get("longitude"),
            "modified": inc.get("modified"),
        }
        for inc in incidents
        if inc.get("state") == "WA"
        and "fire" in (inc.get("incident_type") or "").lower()
    ]
    return {
        "source": "inciweb",
        "fetched_at": fetched_at.isoformat(),
        "active_wa_fires": wa_fires,
        "stale": False,
    }


def normalize_snotel(raw: list, station_id: str, fetched_at: datetime) -> dict:
    """
    Normalizes NRCS SNOTEL station data from the v1/data endpoint.

    raw is a list; raw[0]["data"] is a flat list of element records:
      {"stationElement": {"elementCode": "WTEQ"}, "values": [{"date": ..., "value": ..., "median": ...}]}

    pct_of_median is computed as value/median*100 from the WTEQ element's latest entry.
    """
    station_data = raw[0].get("data", []) if raw else []
    elements = {e["stationElement"]["elementCode"]: e for e in station_data}

    def latest(code: str) -> float | None:
        series = elements.get(code, {}).get("values", [])
        for entry in reversed(series):
            if entry.get("value") is not None:
                try:
                    return float(entry["value"])
                except ValueError:
                    pass
        return None

    def latest_pct_of_median(code: str) -> float | None:
        series = elements.get(code, {}).get("values", [])
        for entry in reversed(series):
            value = entry.get("value")
            median = entry.get("median")
            if value is not None and median is not None:
                try:
                    return round(float(value) / float(median) * 100, 1)
                except (ValueError, ZeroDivisionError):
                    pass
        return None

    return {
        "source": "snotel",
        "fetched_at": fetched_at.isoformat(),
        "station_id": station_id,
        "snow_water_equivalent_in": latest("WTEQ"),
        "snow_depth_in": latest("SNWD"),
        "pct_of_median": latest_pct_of_median("WTEQ"),
        "stale": False,
    }


def normalize_wdfw_stocking(records: list, fetched_at: datetime) -> list[dict]:
    """
    Normalizes WDFW Fish Plants Socrata records (dataset 6fex-3r7d) into a
    flat list suitable for upsert into the stocking_events table.

    Field mapping for dataset 6fex-3r7d:
      release_location  → water_name
      county            → county
      release_start_date → stocked_date
      species           → species
      number_released   → count
      lifestage         → size_description
      geo_code          → source_record_id
    """
    normalized = []
    for r in records:
        normalized.append({
            "source": "wdfw_stocking",
            "fetched_at": fetched_at.isoformat(),
            "water_name": r.get("release_location"),
            "county": (r.get("county") or "").strip() or None,
            "stocked_date": r.get("release_start_date") or r.get("release_end_date"),
            "species": r.get("species"),
            "count": _safe_int(r.get("number_released")),
            "size_description": r.get("lifestage"),
            "source_record_id": r.get("geo_code") or r.get(":id"),
        })
    return normalized


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
