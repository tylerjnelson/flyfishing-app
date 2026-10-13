"""
Loads per-gauge flow references (TODO-18): USGS day-of-year flow percentiles
and NWS flood stages. Run weekly and at startup by the gauge_reference job;
the data changes about once a year, so a gauge refreshed in the last
REFRESH_AFTER is skipped.

  USGS statistics API (the WaterWatch / NWIS stats replacement):
    GET https://api.waterdata.usgs.gov/statistics/v0/observationNormals
        ?monitoring_location_id=USGS-{id}&parameter_code=00060&computation_type=percentile
    GeoJSON; properties.data[] is one entry per parent time series, each with
    values[] = {time_of_year "MM-DD", time_of_year_type, percentiles[],
    values[] (strings), sample_count}. Paginated via "next". Rejects `f=json`
    (400 "Invalid parameter ['f']"). Percentiles are of daily MEANS
    (parent_statistic_id 00003), so they're a little smoother than the
    15-minute readings they're compared against.

  NWPS gauge metadata: GET https://api.water.noaa.gov/nwps/v1/gauges/{lid}
    flood.categories.{action,minor,moderate,major}.stage in ft; -9999 = none.
    Stages share the USGS gauge-height datum at every gauge checked (2026-10-13).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from config import settings
from conditions.flow_reference import PERCENTILES

log = logging.getLogger(__name__)

_NORMALS_URL = "https://api.waterdata.usgs.gov/statistics/v0/observationNormals"
_NWPS_GAUGE_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{lid}"
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=5.0, pool=5.0)
_MAX_PAGES = 10

REFRESH_AFTER = timedelta(days=6)

_EXPECTED_PCTS = [str(p) for p in PERCENTILES]


def _usgs_headers() -> dict:
    return {"X-Api-Key": settings.usgs_api_key} if settings.usgs_api_key else {}


def parse_normals(pages: list[dict]) -> list[dict]:
    """
    Day-of-year discharge percentile rows from observationNormals pages:
    [{time_of_year, values: [p5..p95], sample_count}]. Keeps only percentiles
    of the daily mean series, with all seven percentiles present.
    """
    rows: dict[str, dict] = {}
    for page in pages:
        for feature in page.get("features") or []:
            for series in (feature.get("properties") or {}).get("data") or []:
                if series.get("parameter_code") != "00060" or series.get("parent_statistic_id") != "00003":
                    continue
                for v in series.get("values") or []:
                    if v.get("time_of_year_type") != "day_of_year" or v.get("percentiles") != _EXPECTED_PCTS:
                        continue
                    try:
                        values = [float(x) for x in v["values"]]
                    except (KeyError, TypeError, ValueError):
                        continue
                    if len(values) != len(PERCENTILES) or values != sorted(values):
                        continue
                    rows[v["time_of_year"]] = {
                        "time_of_year": v["time_of_year"],
                        "values": values,
                        "sample_count": v.get("sample_count"),
                    }
    return list(rows.values())


async def fetch_flow_normals(site_id: str) -> list[dict]:
    """All day-of-year rows for one gauge ([] when USGS has none). Raises on HTTP errors."""
    url: str | None = _NORMALS_URL
    params: dict | None = {
        "monitoring_location_id": f"USGS-{site_id}",
        "parameter_code": "00060",
        "computation_type": "percentile",
    }
    pages = []
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_usgs_headers()) as client:
        for _ in range(_MAX_PAGES):
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            page = resp.json()
            pages.append(page)
            url, params = page.get("next"), None  # "next" is a full URL
            if not url:
                break
    return parse_normals(pages)


def parse_flood_stages(meta: dict) -> dict | None:
    """{action_ft, minor_ft, moderate_ft, major_ft} from NWPS gauge metadata; None if no stages."""
    cats = (meta.get("flood") or {}).get("categories") or {}
    out = {}
    for key in ("action", "minor", "moderate", "major"):
        stage = (cats.get(key) or {}).get("stage")
        out[f"{key}_ft"] = float(stage) if isinstance(stage, (int, float)) and stage > -9000 else None
    return out if out["minor_ft"] is not None or out["action_ft"] is not None else None


async def fetch_flood_stages(lid: str) -> dict | None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_NWPS_GAUGE_URL.format(lid=lid))
        resp.raise_for_status()
        return parse_flood_stages(resp.json())
