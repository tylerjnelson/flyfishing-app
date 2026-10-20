"""
Access points: which one to route a candidate water to, and how to show it (TODO-5).

A water body has many access points (migration 0018; built from WDFW, State
Parks, Ecology and OpenStreetMap by scripts/build_access_points.py). Ranking
stays per water body, since flow, rules and stocking belong to the whole water.
For each candidate the context builder routes HERE to one access point: the one
with the shortest estimated drive (free, no HERE), skipping any whose vehicle
gate is closed on the first trip day. One HERE call per water, as before.

The estimate is the road network's (conditions/road_network.py, TODO-19). Without
the graph file it is the Haversine drive estimate to the point, or, for river
points, to the point's gateway (where its local roads leave the highway network)
plus the precomputed drive from there: a straight line alone picks the wrong end
of a mountain valley (from Seattle, the top of the 18-mile Icicle Road).

Weaker points cost a few minutes so an official access a little further wins
over a road bridge: OpenStreetMap +1 min, a bridge +2, a low-confidence match +3.
"""

import calendar
import re
from datetime import date
from urllib.parse import quote

from conditions.routing import haversine_drive_minutes

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_WEEKDAYS = {d.lower(): i for i, d in enumerate(calendar.day_name)}
_ORDINALS = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}

_SPEC = r"(?:(\d)(?:st|nd|rd|th)\s+(\w+day)\s+in\s+(\w+)|([A-Za-z]+)\s+(\d{1,2}))"
_RANGE = re.compile(_SPEC + r"\s*[-–]\s*" + _SPEC, re.I)
_CLOSED = re.compile(r"closed to (?:all |street-legal )?vehicles", re.I)

_PENALTY_MIN = {"osm": 1.0, "bridge": 2.0, "low": 3.0}

_SOURCE_LABEL = {
    "wdfw_was": "WDFW access",
    "wdfw_shore": "WDFW shore access",
    "parks_launch": "State Parks launch",
    "ecology": "public beach access",
    "curated": "access",
    "osm": "OpenStreetMap",
}


def _spec_date(year: int, nth, weekday, month_a, month_b, day_b) -> date | None:
    try:
        if nth:
            month = _MONTHS[month_a.lower()]
            wd = _WEEKDAYS[weekday.lower()]
            days = [d for d in calendar.Calendar().itermonthdates(year, month)
                    if d.month == month and d.weekday() == wd]
            return days[int(nth) - 1]
        return date(year, _MONTHS[month_b.lower()], int(day_b))
    except (KeyError, IndexError, ValueError):
        return None


def vehicle_access(seasonal: str | None, day: date) -> bool | None:
    """Can you drive in on `day`? True / False, or None when the text gives no
    dates ("Open to vehicles only during the regulated sport fishing season")."""
    if not seasonal:
        return True
    m = _RANGE.search(seasonal)
    if not m:
        return None
    g = m.groups()
    start = _spec_date(day.year, *g[:5])
    end = _spec_date(day.year, *g[5:])
    if start is None or end is None:
        return None
    inside = start <= day <= end if start <= end else (day >= start or day <= end)
    closed_window = bool(_CLOSED.search(seasonal))
    return not inside if closed_window else inside


def estimated_minutes(ap, origin_lat: float, origin_lon: float) -> float:
    """Free drive-time estimate used only to choose among a water's points."""
    if getattr(ap, "gateway_minutes", None) is not None and ap.gateway_latitude is not None:
        return (haversine_drive_minutes(origin_lat, origin_lon, float(ap.gateway_latitude), float(ap.gateway_longitude))
                + float(ap.gateway_minutes))
    return haversine_drive_minutes(origin_lat, origin_lon, float(ap.latitude), float(ap.longitude))


def _cost(ap, origin_lat: float, origin_lon: float, estimate=None) -> float:
    cost = estimate(ap) if estimate else estimated_minutes(ap, origin_lat, origin_lon)
    if ap.source == "osm":
        cost += _PENALTY_MIN["osm"]
    if ap.kind == "bridge":
        cost += _PENALTY_MIN["bridge"]
    if ap.confidence == "low":
        cost += _PENALTY_MIN["low"]
    return cost


def pick_access_point(points: list, origin_lat: float | None, origin_lon: float | None, day: date,
                      estimate=None):
    """The access point to route to, or None when the water has none.
    `estimate(ap) -> minutes` replaces the straight-line estimate (the road
    network's, TODO-19); it returns inf for a point it cannot reach."""
    if not points:
        return None
    usable = [p for p in points if vehicle_access(p.seasonal, day) is not False] or list(points)
    if origin_lat is None or origin_lon is None:
        rank = {"high": 0, "medium": 1, "low": 2}
        return min(usable, key=lambda p: (rank.get(p.confidence, 3), p.source == "osm", p.key))
    return min(usable, key=lambda p: (_cost(p, origin_lat, origin_lon, estimate), p.key))


_STOCKING_NAME = re.compile(r"^[A-Z0-9 .#/'&-]+(\([A-Z]{3,4}\))?$")


def display_name(ap) -> str:
    if ap.source == "curated" and _STOCKING_NAME.match(ap.name):
        # Hand-placed spots kept the stocking report's name: "LAKE TAHUYA (KITS)".
        return re.sub(r"\s*\([A-Z]{3,4}\)$", "", ap.name).title()
    if ap.source == "wdfw_shore":
        return "WDFW shore access"
    if ap.source == "wdfw_was":
        return f"{ap.name} (WDFW access)"
    if ap.source == "parks_launch":
        return f"{ap.name} boat launch (State Parks)"
    return ap.name


def directions_url(lat: float, lon: float) -> str:
    return "https://www.google.com/maps/dir/?api=1&destination=" + quote(f"{lat:.6f},{lon:.6f}")


def access_summary(ap, day: date) -> dict:
    """JSON-safe dict stored on the candidate; feeds the card and the model."""
    lat, lon = float(ap.latitude), float(ap.longitude)
    return {
        "name": display_name(ap),
        "kind": ap.kind,
        "source": ap.source,
        "source_label": _SOURCE_LABEL.get(ap.source, ap.source),
        "latitude": lat,
        "longitude": lon,
        "directions_url": directions_url(lat, lon),
        "description": ap.description,
        "parking": ap.parking,
        "facilities": list(ap.facilities or []),
        "seasonal": ap.seasonal,
        "vehicle_access": vehicle_access(ap.seasonal, day),
        "pass_required": ap.pass_required,
        "confidence": ap.confidence,
    }


def access_line(access: dict | None) -> str | None:
    """One line for the model: where the drive time goes to and what's there."""
    if not access:
        return None
    kind = access["kind"].replace("_", " ")
    parts = [f"Access: {access['name']} ({kind})"]
    if access.get("description"):
        parts.append(access["description"])
    if access.get("parking"):
        parts.append(access["parking"])
    if access.get("vehicle_access") is False:
        parts.append(f"vehicle gate closed on the trip date ({access['seasonal']}); walk in")
    elif access.get("vehicle_access") is None and access.get("seasonal"):
        parts.append(f"seasonal: {access['seasonal']}")
    if access.get("pass_required"):
        parts.append(access["pass_required"])
    return " · ".join(parts)
