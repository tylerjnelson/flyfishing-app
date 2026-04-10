"""
InciWeb wildfire fetcher — 2-hour interval via APScheduler.

Fetches active wildfire incidents from RSS feed and filters to Washington fires.
Wrapped with the inciweb_breaker circuit breaker.

URL: https://inciweb.wildfire.gov/incidents/rss.xml
  (JSON API at inciweb.nwcg.gov/incidents.json redirects to 404 — dead as of 2026-04-10)

RSS item description contains:
  "State: Washington"
  "The type of incident is Wildfire"
  "Latitude: 46° 22 91  Longitude: 120° 05 33"
  — lat/lon format is DD° MM SS where decimal minutes = MM.SS/60
  — longitude is positive in the feed; negated here since all US fires are western hemisphere
"""

import logging
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx
import pybreaker

from conditions.circuit_breaker import inciweb_breaker
from conditions.normalizer import normalize_inciweb

log = logging.getLogger(__name__)

_INCIDENTS_URL = "https://inciweb.wildfire.gov/incidents/rss.xml"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0)


async def fetch_inciweb() -> dict | None:
    """
    Fetch active WA wildfire incidents.

    Returns normalised data dict, or None when the circuit is open.
    """
    try:
        incidents = await _fetch()
        return normalize_inciweb(incidents, fetched_at=datetime.now(tz=timezone.utc))
    except pybreaker.CircuitBreakerError:
        log.warning("circuit_open", extra={"source": "inciweb"})
        return None


@inciweb_breaker
async def _fetch() -> list:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(_INCIDENTS_URL)
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    channel = root.find("channel")
    if channel is None:
        return []

    incidents = []
    for item in channel.findall("item"):
        desc = item.findtext("description") or ""

        # State — full name in feed; map Washington → "WA", others kept as-is
        state_m = re.search(r"State:\s*([A-Za-z][A-Za-z ]+)", desc)
        if not state_m:
            continue
        state_name = state_m.group(1).strip()
        state = "WA" if state_name.lower() == "washington" else state_name

        # Incident type from description prose
        type_m = re.search(r"type of incident is\s+([^.\n]+)", desc, re.IGNORECASE)
        incident_type = type_m.group(1).strip() if type_m else ""

        # Lat/lon — "Latitude: 46° 22 91" format (decimal minutes = MM.SS / 60)
        lat_m = re.search(r"Latitude:\s*([\d°\s]+?)(?:\s{2,}|Longitude)", desc)
        lon_m = re.search(r"Longitude:\s*([\d°\s]+?)(?:\s{2,}|$|\n|---)", desc)
        lat = _parse_coord(lat_m.group(1) if lat_m else None)
        lon_raw = _parse_coord(lon_m.group(1) if lon_m else None)
        # US fires are in the western hemisphere; RSS omits the sign
        lon = -lon_raw if lon_raw is not None else None

        incidents.append({
            "id": item.findtext("guid"),
            "name": (item.findtext("title") or "").strip(),
            "incident_type": incident_type,
            "state": state,
            "latitude": lat,
            "longitude": lon,
            "modified": item.findtext("pubDate"),
        })

    return incidents


def _parse_coord(s: str | None) -> float | None:
    """
    Parse RSS coordinate string "DD° MM SS" → decimal degrees.
    The format encodes decimal minutes as MM.SS, so result = DD + MM.SS/60.
    """
    if not s:
        return None
    parts = [p for p in re.split(r"[°\s]+", s.strip()) if p.isdigit() or re.match(r"^\d+$", p)]
    try:
        if len(parts) >= 3:
            d, mm, ss = int(parts[0]), int(parts[1]), int(parts[2])
            return d + float(f"{mm}.{ss:02d}") / 60
        if len(parts) == 2:
            d, mm = int(parts[0]), int(parts[1])
            return d + mm / 60
        if len(parts) == 1:
            return float(parts[0])
    except (ValueError, IndexError):
        return None
    return None
