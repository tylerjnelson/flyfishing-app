"""
conditions_cache writes — shared by the scheduler jobs and the chat context
builder's real-time fetch path.

Every write prunes that (water_body, source)'s rows older than the source TTL.
Readers only ever use the freshest row per source; history is kept briefly for
debugging. Before this was shared, the real-time path (NWS/AirNow/USGS at
session open) inserted without pruning, and conditions_cache grew to ~28k rows.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from db.models import ConditionsCache

# How long to retain rows per source before pruning on write.
CACHE_TTL_HOURS: dict[str, int] = {
    "inciweb":    6,
    "nps_alerts": 6,
    "noaa_nws":   6,
    "usgs":       6,
    "airnow":     6,
    "noaa_nwrfc": 48,
    "snotel":     48,
    "wta":        48,
}


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
