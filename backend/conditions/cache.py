"""
conditions_cache writes — shared by the scheduler jobs and the chat context
builder's real-time fetch path.

Every write prunes that (water_body, source)'s rows older than the source TTL,
and the nightly cache_prune job prunes every source (rows for waters nobody
fetches again otherwise stayed forever). Readers use the freshest row per source
that is within MAX_AGE_HOURS (fresh()); an older row is no reading at all
(DI-0: July weather was still being served in October). Before this was shared, the real-time path (NWS/AirNow/USGS at
session open) inserted without pruning, and conditions_cache grew to ~28k rows.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, or_

from db.models import ConditionsCache

# How long to retain rows per source before pruning on write.
CACHE_TTL_HOURS: dict[str, int] = {
    "inciweb":    6,
    "nps_alerts": 6,
    "noaa_nws":   6,
    "usgs":       6,
    "usgs_bands": 6,   # readings at flow_bands gauges other than the water body's own
    "airnow":     6,
    "noaa_nwrfc": 48,
    "noaa_nwrfc_bands": 48,
    "snotel":     48,
    "wta":        48,
}


# The oldest row a reader may use, per source. Session-open fetches (USGS, NWS,
# AirNow) refresh candidates; the 2-hourly NWRFC and daily SNOTEL/WTA jobs the
# rest. A source missing here (inciweb, nps_alerts: global, every 2 h) is not
# age-limited at read.
MAX_AGE_HOURS: dict[str, int] = {
    "usgs":       6,
    "usgs_bands": 6,
    "airnow":     6,
    "noaa_nws":   12,
    "noaa_nwrfc": 12,
    "noaa_nwrfc_bands": 12,
    "snotel":     72,
    "wta":        7 * 24,
}


def fresh(now: datetime | None = None):
    """WHERE clause: ConditionsCache rows young enough to read (MAX_AGE_HOURS)."""
    now = now or datetime.now(tz=timezone.utc)
    return or_(
        ConditionsCache.source.not_in(list(MAX_AGE_HOURS)),
        *[and_(ConditionsCache.source == src, ConditionsCache.fetched_at >= now - timedelta(hours=h))
          for src, h in MAX_AGE_HOURS.items()],
    )


async def prune_expired(session, now: datetime | None = None) -> int:
    """Delete every per-water row older than both its source's TTL and its read
    limit. Global sources (inciweb, nps_alerts) keep their write-time pruning, so
    their latest row survives an outage. Returns the number of rows removed."""
    now = now or datetime.now(tz=timezone.utc)
    removed = 0
    for src in sorted(MAX_AGE_HOURS):
        hours = max(CACHE_TTL_HOURS.get(src, 0), MAX_AGE_HOURS.get(src, 0))
        result = await session.execute(
            delete(ConditionsCache)
            .where(ConditionsCache.source == src)
            .where(ConditionsCache.fetched_at < now - timedelta(hours=hours))
        )
        removed += result.rowcount or 0
    return removed


async def write_conditions_cache(session, water_body_id, source: str, data: dict) -> None:
    """Insert a new conditions_cache row and prune rows older than the source TTL.

    water_body_id may be None for global sources (inciweb, nps_alerts).
    """
    now = datetime.now(tz=timezone.utc)
    ttl_hours = CACHE_TTL_HOURS.get(source)
    if ttl_hours:
        await session.execute(
            delete(ConditionsCache)
            .where(ConditionsCache.water_body_id == water_body_id)
            .where(ConditionsCache.source == source)
            .where(ConditionsCache.fetched_at < now - timedelta(hours=ttl_hours))
        )

    data_hash = hashlib.md5(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    session.add(ConditionsCache(
        water_body_id=water_body_id,
        source=source,
        data=data,
        data_hash=data_hash,
        fetched_at=now,
    ))
