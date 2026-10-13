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
from datetime import datetime, timedelta, timezone


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

# Readings older than this are treated as missing. A discontinued gauge keeps
# returning its last value forever (Elwha 12044900's latest is from 2011).
USGS_MAX_READING_AGE = timedelta(hours=24)
_USGS_NO_DATA = -999999.0  # legacy WaterServices sentinel; the OGC API sends null
_USGS_INSTANTANEOUS = "00011"  # statistic_id for continuous (instantaneous) values
_USGS_PARAMS = ("00060", "00065", "00010")


def _usgs_series(raw: dict) -> dict[str, list[tuple[datetime, float | None]]]:
    """
    Group OGC `continuous` features by parameter code, oldest→newest.
    Value is None for a null/sentinel/unparseable reading. The API does not
    guarantee result order, so this sorts by time.
    """
    series: dict[str, list[tuple[datetime, float | None]]] = {}
    for feature in raw.get("features") or []:
        props = feature.get("properties") or {}
        code = props.get("parameter_code")
        if code not in _USGS_PARAMS or props.get("statistic_id", _USGS_INSTANTANEOUS) != _USGS_INSTANTANEOUS:
            continue
        try:
            observed = datetime.fromisoformat(props["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        try:
            value = float(props.get("value"))
        except (TypeError, ValueError):
            value = None
        if value == _USGS_NO_DATA:
            value = None
        series.setdefault(code, []).append((observed, value))
    for points in series.values():
        points.sort(key=lambda p: p[0])
    return series


def normalize_usgs(raw: dict, fetched_at: datetime) -> dict:
    """
    Normalizes a USGS Water Data API (OGC) `continuous` items response for one
    gauge (GeoJSON FeatureCollection, one feature per reading).

    Parameter codes:
      00060 — discharge (CFS)
      00065 — gauge height (ft)
      00010 — water temperature (°C)

    A parameter is reported as None, and listed in `dropped` with the reason,
    when its only values are null (no data) or its latest reading is older
    than USGS_MAX_READING_AGE. The caller pops `dropped` for logging.
    """
    by_param: dict[str, float] = {}
    dropped: dict[str, str] = {}
    cfs_points: list[tuple[datetime, float]] = []
    observed_at = None
    for code, points in _usgs_series(raw).items():
        valid = [(t, v) for t, v in points if v is not None]
        if not valid:
            dropped[code] = "no_data_sentinel"
            continue
        observed, value = valid[-1]
        if fetched_at - observed > USGS_MAX_READING_AGE:
            dropped[code] = f"stale:{observed.isoformat()}"
            continue
        by_param[code] = value
        if code == "00060":
            cfs_points, observed_at = valid, observed

    cfs = by_param.get("00060")
    temp_c = by_param.get("00010")
    temp_f = round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None

    # Trend: latest vs the newest reading at least 45 min older (~1h back).
    # By time, not record count: USGS posts in hourly batches about an hour
    # late, so a fixed-length window often holds only 4 readings.
    trend = None
    if cfs is not None:
        hour_ago = [v for t, v in cfs_points if t <= observed_at - timedelta(minutes=45)]
        if hour_ago:
            trend = _flow_trend(cfs, hour_ago[-1])

    return {
        "source": "usgs",
        "fetched_at": fetched_at.isoformat(),
        "observed_at": observed_at.isoformat() if observed_at else None,
        "cfs": cfs,
        "gauge_height_ft": by_param.get("00065"),
        "temp_f": temp_f,
        "turbidity_fnu": None,  # USGS turbidity is parameter 63680, not requested
        "trend": trend,
        "stale": False,
        "dropped": dropped,
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


AIRNOW_MAX_READING_AGE = timedelta(hours=3)
# AirNow reports local wall-clock time plus a zone abbreviation.
_AIRNOW_TZ_OFFSETS = {"PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST": -6, "CDT": -5,
                      "EST": -5, "EDT": -4, "AKST": -9, "AKDT": -8, "HST": -10}
_AIRNOW_POLLUTANTS = {"OZONE": "ozone", "PM2.5": "PM2.5", "PM10": "PM10"}


def _airnow_observed_at(obs: dict) -> datetime | None:
    offset = _AIRNOW_TZ_OFFSETS.get((obs.get("localTimeZone") or "").upper())
    if offset is None:
        return None
    try:
        local = datetime.strptime(f"{obs['dateObserved']} {obs['hourObserved']}", "%Y-%m-%d %H:%M")
    except (KeyError, TypeError, ValueError):
        return None
    return local.replace(tzinfo=timezone(timedelta(hours=offset)))


def normalize_airnow(raw, fetched_at: datetime) -> dict:
    """
    Normalizes an AirNow "current observations by lat/long" response
    (/aq/observation/current/ziplatlong/, which replaced the latLong/current
    service retired 2026-10-01). raw is one record per pollutant: the closest
    monitor reporting it within 50 miles. Uses the highest NowCast AQI.

    "No monitors within 50 miles" arrives as HTTP 200 with a WebServiceError
    envelope; that's a normal no-data result (aqi None). Any other envelope
    raises ValueError so the fetcher logs it as a failure. Readings older than
    AIRNOW_MAX_READING_AGE are ignored.
    """
    if isinstance(raw, dict):
        errors = raw.get("WebServiceError") or [{}]
        message = errors[0].get("Message") or str(raw)
        if "no observations" not in message.lower():
            raise ValueError(f"AirNow error: {message}")
        raw = []

    best = None
    for obs in raw:
        aqi = obs.get("nowcastAQI")
        if not isinstance(aqi, (int, float)) or aqi < 0:
            continue
        observed = _airnow_observed_at(obs)
        if observed is not None and fetched_at - observed > AIRNOW_MAX_READING_AGE:
            continue
        if best is None or aqi > best[0]:
            best = (aqi, obs, observed)

    aqi, obs, observed = best if best else (None, {}, None)
    pollutant = obs.get("parameterName")
    return {
        "source": "airnow",
        "fetched_at": fetched_at.isoformat(),
        "observed_at": observed.isoformat() if observed else None,
        "aqi": int(aqi) if aqi is not None else None,
        "category": obs.get("aqiCategoryName"),
        "pollutant": _AIRNOW_POLLUTANTS.get(pollutant, pollutant),
        "site": obs.get("siteName"),
        "stale": False,
    }


def _flow_trend(cfs: float, prior_cfs) -> str | None:
    """rising / dropping (>=5% change vs ~1h prior) or stable; None if unknown."""
    try:
        prior = float(prior_cfs)
    except (TypeError, ValueError):
        return None
    if prior <= 0:
        return None
    pct_change = (cfs - prior) / prior
    if pct_change >= 0.05:
        return "rising"
    if pct_change <= -0.05:
        return "dropping"
    return "stable"


def nwps_series_fields(series: dict) -> tuple[str, float, str | None] | None:
    """
    For an NWPS stageflow series ("observed" / "forecast"), return
    (flow_key, multiplier_to_cfs, stage_key). None if the series has no flow.

    Field order is NOT fixed: most WA gauges are primary=Stage(ft) /
    secondary=Flow(kcfs), but AUBW1 (Green), TOLW1 (Tolt) and NISW1
    (Nisqually) are primary=River Discharge(kcfs) / secondary=Stage(ft).
    Reading "secondary" as kcfs turned Green River's 57 ft stage into a
    57,000 cfs forecast (found 2026-10-11).
    """
    flow = stage = None
    mult = 0.0
    for key in ("primary", "secondary"):
        name = (series.get(f"{key}Name") or "").lower()
        units = (series.get(f"{key}Units") or "").lower()
        if ("flow" in name or "discharge" in name) and units in ("kcfs", "cfs"):
            flow, mult = key, (1000.0 if units == "kcfs" else 1.0)
        elif "stage" in name:
            stage = key
    return (flow, mult, stage) if flow else None


def normalize_nwps_observed(raw: dict, gauge_id: str, fetched_at: datetime) -> dict | None:
    """
    USGS-shaped flow reading from a NOAA NWPS stageflow *observed* series.

    Fallback for rivers whose USGS gauge is discontinued but whose NWPS gauge
    still reports (Naches NACW1, Stillaguamish ARLW1, Yakima PARW1, as of
    2026-10). Which field is flow comes from the series metadata
    (nwps_series_fields), not position. Negative values
    are NWPS's missing-data markers (ARLW1 had 535 of them). Returns None
    when there's no usable reading within USGS_MAX_READING_AGE.
    """
    series = raw.get("observed") or {}
    fields = nwps_series_fields(series)
    if fields is None:
        return None
    flow_key, mult, stage_key = fields
    points = []
    for p in series.get("data") or []:
        try:
            flow = float(p[flow_key])
            observed = datetime.fromisoformat(p["validTime"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if flow < 0:
            continue
        stage = p.get(stage_key) if stage_key else None
        points.append((observed, flow * mult, stage if isinstance(stage, (int, float)) and stage >= 0 else None))
    if not points:
        return None
    points.sort(key=lambda x: x[0])
    observed, cfs, stage = points[-1]
    if fetched_at - observed > USGS_MAX_READING_AGE:
        return None
    hour_ago = [p for p in points if p[0] <= observed - timedelta(minutes=45)]
    return {
        "source": "usgs",  # same slot/shape consumers read; provider says where it came from
        "provider": "noaa_nwps_observed",
        "nwps_gauge_id": gauge_id,
        "fetched_at": fetched_at.isoformat(),
        "observed_at": observed.isoformat(),
        "cfs": round(cfs, 1),
        "gauge_height_ft": stage,
        "temp_f": None,
        "turbidity_fnu": None,
        "trend": _flow_trend(cfs, hour_ago[-1][1]) if hour_ago else None,
        "stale": False,
    }


def normalize_noaa_nwrfc(raw: dict, gauge_id: str, fetched_at: datetime) -> dict:
    """
    Normalizes a NOAA NWPS stageflow forecast for a single gauge.

    Stageflow response shape: {observed: {...}, forecast: {primaryName, primaryUnits,
    secondaryName, secondaryUnits, data: [{validTime, primary, secondary}]}}.
    Each entry gets `flow_cfs` and `stage_ft` from whichever fields the
    metadata says are flow and stage (field order varies by gauge; see
    nwps_series_fields). Values that can't be determined are None, and
    consumers must not guess. stage_ft feeds the flood-stage gate.
    """
    series = raw.get("forecast") or {}
    fields = nwps_series_fields(series)
    forecasts = []
    for entry in series.get("data") or []:
        flow_cfs = stage_ft = None
        if fields:
            try:
                value = float(entry[fields[0]])
                flow_cfs = round(value * fields[1], 1) if value >= 0 else None
            except (KeyError, TypeError, ValueError):
                flow_cfs = None
            if fields[2]:
                try:
                    value = float(entry[fields[2]])
                    stage_ft = value if value > -999 else None
                except (KeyError, TypeError, ValueError):
                    stage_ft = None
        forecasts.append({**entry, "flow_cfs": flow_cfs, "stage_ft": stage_ft})
    return {
        "source": "noaa_nwrfc",
        "fetched_at": fetched_at.isoformat(),
        "gauge_id": gauge_id,
        "forecast": forecasts,  # [{validTime, primary, secondary, flow_cfs, stage_ft}]
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
      (whole record)    → source_record_id, see _stocking_record_ids
    """
    normalized = []
    for r, record_id in zip(records, _stocking_record_ids(records)):
        normalized.append({
            "source": "wdfw_stocking",
            "fetched_at": fetched_at.isoformat(),
            "water_name": r.get("release_location"),
            "county": (r.get("county") or "").strip() or None,
            "stocked_date": r.get("release_start_date") or r.get("release_end_date"),
            "species": r.get("species"),
            "count": _safe_int(r.get("number_released")),
            "size_description": r.get("lifestage"),
            "source_record_id": record_id,
        })
    return normalized


def _stocking_record_ids(records: list) -> list[str]:
    """
    Stable dedupe key per stocking record: a hash of the record's content plus
    an occurrence number among identical records ("<sha1[:24]>-<n>").

    Socrata's `:id` can't be used: it changes every time WDFW republishes the
    dataset, so the unique constraint never matched and each daily run
    re-inserted everything (~100x duplicates by 2026-10). No subset of fields is
    unique either, and the source contains genuinely identical rows, so hash the
    full record and number repeats to keep them distinct but stable.
    """
    seen: dict[str, int] = {}
    ids = []
    for r in records:
        content = {k: v for k, v in r.items() if not k.startswith(":")}
        digest = hashlib.sha1(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        seen[digest] = seen.get(digest, 0) + 1
        ids.append(f"{digest}-{seen[digest]}")
    return ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
