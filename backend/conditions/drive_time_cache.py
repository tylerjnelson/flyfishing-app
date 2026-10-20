"""
HERE drive times reused for 90 days (migration 0019, TODO-19).

Planning-mode HERE routes have no traffic and no date, so the answer for one
origin and one access point only changes when the roads do. A user with a saved
home location pays for each card's route once a quarter; a second chat about
the same waters costs nothing.

Keyed on the origin to ~100 m, the destination to ~1 m, and the avoid[areas]
string (a winter route around a closed pass is a different answer). The ferry
wait depends on the trip date, so it is never stored; see ferries.py.

Uses its own short session so a cache write never rides on (or rolls back
with) the chat request's transaction. A cache failure is logged and treated as
a miss.
"""

import json
import logging
from decimal import Decimal

from sqlalchemy import text

from conditions.routing import Route
from db.connection import AsyncSessionLocal

log = logging.getLogger(__name__)

TTL_DAYS = 90


def _dec(x, places: int) -> Decimal:
    # Exact decimals: a float bound against a NUMERIC column is compared as its
    # binary expansion (47.606 -> 47.60599999...), so a stored key never matches.
    return Decimal(f"{float(x):.{places}f}")


def _key(origin_lat, origin_lon, dest_lat, dest_lon, avoid) -> dict:
    return {
        "olat": _dec(origin_lat, 3),
        "olon": _dec(origin_lon, 3),
        "dlat": _dec(dest_lat, 5),
        "dlon": _dec(dest_lon, 5),
        "avoid": avoid or "",
    }


async def get(origin_lat, origin_lon, dest_lat, dest_lon, avoid: str | None) -> Route | None:
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(text(
                "SELECT minutes, ferries FROM drive_time_cache "
                "WHERE origin_lat = :olat AND origin_lon = :olon AND dest_lat = :dlat AND dest_lon = :dlon "
                "AND avoid = :avoid AND fetched_at > now() - make_interval(days => :ttl)"
            ), {**_key(origin_lat, origin_lon, dest_lat, dest_lon, avoid), "ttl": TTL_DAYS})).first()
    except Exception as exc:
        log.warning("drive_time_cache_unavailable", extra={"reason": type(exc).__name__})
        return None
    if row is None:
        return None
    return Route(minutes=int(row.minutes), is_fallback=False, ferries=[tuple(f) for f in (row.ferries or [])])


async def put(origin_lat, origin_lon, dest_lat, dest_lon, avoid: str | None, route: Route) -> None:
    if route.is_fallback:
        return
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text(
                "INSERT INTO drive_time_cache (origin_lat, origin_lon, dest_lat, dest_lon, avoid, minutes, ferries, fetched_at) "
                "VALUES (:olat, :olon, :dlat, :dlon, :avoid, :minutes, CAST(:ferries AS jsonb), now()) "
                "ON CONFLICT (origin_lat, origin_lon, dest_lat, dest_lon, avoid) "
                "DO UPDATE SET minutes = EXCLUDED.minutes, ferries = EXCLUDED.ferries, fetched_at = now()"
            ), {**_key(origin_lat, origin_lon, dest_lat, dest_lon, avoid),
                "minutes": route.minutes, "ferries": json.dumps([list(f) for f in route.ferries])})
            await db.commit()
    except Exception as exc:
        log.warning("drive_time_cache_write_failed", extra={"reason": type(exc).__name__})
