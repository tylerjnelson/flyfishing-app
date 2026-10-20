"""
Drive times for a candidate build: estimate everything, verify the cards (TODO-19).

Drive time does not rank; it filters (the user's max drive) and it is shown.
So:
  1. estimate()   before any conditions fetch: one road-network Dijkstra from
                  the origin (conditions/road_network.py) picks each water's
                  access point and estimates the drive, ferries and closed
                  passes included, on the first trip day. Waters estimated past
                  the limit by more than the margin drop out here.
  2. (the context builder ranks)
  3. verify_top() after ranking and variety rotation: HERE (or its 90-day
                  cache) for the top three only. A card HERE puts past the
                  limit is dropped and the next one is checked, up to a few
                  calls. Every other candidate keeps its estimate, labeled as
                  one, and is dropped if the estimate is past the limit.

The ferry wait depends on the trip date and is added to both the estimate (as
an edge cost) and HERE's time (HERE has no date in planning mode).
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from conditions import drive_time_cache, ferries, here_budget, pass_closures, road_network
from conditions.access_points import access_summary, pick_access_point
from conditions.circuit_breaker import routing_breaker
from conditions.routing import get_route, haversine_drive_minutes
from config import settings

log = logging.getLogger(__name__)

LIMIT_MARGIN = 1.15     # keep waters estimated up to 15% past the limit for HERE to judge
TOP_N = 3               # cards that get a HERE drive time
MAX_HERE_CALLS = 6      # per build: the top 3 plus a few replacements


@dataclass
class DriveEstimate:
    dest: tuple[float, float]
    access: dict | None
    minutes: float | None
    ferries: list[str] = field(default_factory=list)
    passes: list[str] = field(default_factory=list)


def _gateway(ap) -> tuple | None:
    if getattr(ap, "gateway_latitude", None) is None or getattr(ap, "gateway_minutes", None) is None:
        return None
    return (float(ap.gateway_latitude), float(ap.gateway_longitude), float(ap.gateway_minutes))


def estimate(pairs: list[tuple], aps_by_wb: dict, origin_lat, origin_lon, day: date,
             max_minutes: int) -> dict:
    """{spot id: DriveEstimate} for every pair. minutes is None when there is no
    origin, inf when the network cannot reach the water within the margin."""
    limit = max_minutes * LIMIT_MARGIN
    have_origin = origin_lat is not None and origin_lon is not None
    dfield = road_network.drive_field(origin_lat, origin_lon, day, limit) if have_origin else None
    memo: dict = {}

    def est(lat: float, lon: float, gateway=None):
        key = (lat, lon)
        if key not in memo:
            if dfield is not None:
                memo[key] = dfield.to_point(lat, lon, gateway)
            else:
                g = gateway
                minutes = (haversine_drive_minutes(origin_lat, origin_lon, g[0], g[1]) + g[2]) if g \
                    else haversine_drive_minutes(origin_lat, origin_lon, lat, lon)
                memo[key] = road_network.Estimate(minutes=minutes)
        return memo[key]

    def ap_minutes(ap) -> float:
        e = est(float(ap.latitude), float(ap.longitude), _gateway(ap))
        return e.minutes if e else float("inf")

    out: dict = {}
    for fs, wb in pairs:
        points = aps_by_wb.get(wb.id, [])
        ap = pick_access_point(points, origin_lat, origin_lon, day,
                               estimate=ap_minutes if have_origin else None)
        if ap is None:
            dest = (float(fs.latitude), float(fs.longitude))
            e = est(*dest) if have_origin else None
        else:
            dest = (float(ap.latitude), float(ap.longitude))
            e = est(dest[0], dest[1], _gateway(ap)) if have_origin else None
        out[fs.id] = DriveEstimate(
            dest=dest,
            access=access_summary(ap, day) if ap is not None else None,
            minutes=(e.minutes if e else float("inf")) if have_origin else None,
            ferries=list(e.ferries) if e else [],
            passes=list(e.passes) if e else [],
        )
    return out


def within_margin(e: DriveEstimate, max_minutes: int) -> bool:
    return e.minutes is None or e.minutes <= max_minutes * LIMIT_MARGIN


def ferry_summaries(route_ids: list[str], day: date) -> list[dict]:
    return [ferries.ferry_summary(ferries.BY_ID[r], day) for r in route_ids if r in ferries.BY_ID]


def pass_warnings(pass_ids: list[str], day: date) -> list[str]:
    """Advisories for passes on the route that may be closed on the trip date
    (a pass inside its closure window is not on any route: the network drops it)."""
    out = []
    for pid in pass_ids:
        c = pass_closures.BY_ID.get(pid)
        if c and pass_closures.status(c, day) == "uncertain":
            out.append(pass_closures.advisory(c, day))
    return out


def _here_ferries(crossings: list, day: date) -> tuple[list[dict], int]:
    """Ferry summaries for HERE's ferry sections, and the wait to add (HERE in
    planning mode has no date; whatever it counts beyond the crossing is taken
    as wait already included)."""
    out, wait = [], 0
    for lat1, lon1, lat2, lon2, minutes in crossings:
        route = ferries.match_terminal_pair(lat1, lon1, lat2, lon2) if lat1 is not None else None
        if route is None:
            log.info("here_ferry_unmatched", extra={"crossing": [lat1, lon1, lat2, lon2]})
            continue
        s = ferries.ferry_summary(route, day)
        out.append(s)
        wait += max(0, s["wait_minutes"] - max(0, int(minutes) - route.crossing_min))
    return out, wait


async def verify_top(candidates: list[dict], origin_lat, origin_lon, departure_time: datetime,
                     day: date, max_minutes: int) -> tuple[list[dict], bool]:
    """HERE drive times for the first TOP_N candidates that stay within the limit.
    Returns (candidates, drive_time_unavailable): unavailable means HERE failed,
    its breaker is open, or the monthly budget is spent. Candidates carry `route_to`
    ([lat, lon]) and their estimate in `drive_minutes` with drive_estimated=True."""
    if origin_lat is None or origin_lon is None:
        return candidates, False
    if settings.here_disabled:
        # Batch / benchmark: no HERE, no cache. Estimates only, judged against the limit.
        return [c for c in candidates if (c.get("drive_minutes") or 0) <= max_minutes], False

    avoid = pass_closures.here_avoid_areas(day)
    kept: list[dict] = []
    verified = calls = 0
    unavailable = False
    for c in candidates:
        if verified >= TOP_N:
            if (c.get("drive_minutes") or 0) <= max_minutes:
                kept.append(c)
            continue
        lat, lon = c["route_to"]
        route = await drive_time_cache.get(origin_lat, origin_lon, lat, lon, avoid)
        cached = route is not None
        if route is None and calls < MAX_HERE_CALLS:
            if routing_breaker.is_blocking():
                unavailable = True
            elif await here_budget.reserve(1):
                calls += 1
                route = await get_route(origin_lat, origin_lon, lat, lon, departure_time, avoid)
                if route.is_fallback:
                    route = None
                    unavailable = True
                else:
                    await drive_time_cache.put(origin_lat, origin_lon, lat, lon, avoid, route)
            else:
                unavailable = True
                log.warning("here_budget_capped_build", extra={"spot": c.get("water_body_name")})
        if route is None:
            # HERE down, over budget, or out of calls this build: the card keeps
            # its estimate (labeled). Only the first two raise the banner.
            if (c.get("drive_minutes") or 0) <= max_minutes:
                kept.append(c)
                verified += 1
            continue
        fer, wait = _here_ferries(route.ferries, day)
        minutes = route.minutes + wait
        if minutes > max_minutes:
            log.info("here_over_limit", extra={"spot": c.get("water_body_name"), "minutes": minutes,
                                               "estimate": c.get("drive_minutes"), "cached": cached})
            continue
        c["drive_minutes"] = minutes
        c["drive_estimated"] = False
        c["ferries"] = fer
        kept.append(c)
        verified += 1
    log.info("drive_times_verified", extra={"here_calls": calls, "verified": verified, "kept": len(kept)})
    return kept, unavailable
