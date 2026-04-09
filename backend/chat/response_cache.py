"""
Response cache — §2.4, §6.3 Step 3.

Cache key: (spot_id, conditions_hash)

The hash is computed by normalizer.compute_conditions_hash() from CFS, temp,
turbidity, and a time-bucketed fetched_at timestamp. Weather and AQI are
intentionally excluded so minor forecast changes do not bust the cache when
river conditions are unchanged.

Cache invalidation (DELETE WHERE spot_id = ?) is performed by the wildfire
and emergency closure fetchers, not here.
"""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from db.models import ResponseCache

log = logging.getLogger(__name__)


async def get_cached_response(
    db, spot_id: str, conditions_hash: str
) -> str | None:
    """Return cached LLM response text, or None on cache miss."""
    result = await db.execute(
        select(ResponseCache.response_text).where(
            ResponseCache.spot_id == spot_id,
            ResponseCache.conditions_hash == conditions_hash,
        )
    )
    row = result.one_or_none()
    if row:
        log.debug("response_cache_hit", extra={"spot_id": str(spot_id)})
        return row.response_text
    return None


async def store_response(
    db, spot_id: str, conditions_hash: str, response_text: str
) -> None:
    """
    Upsert a response into the cache.

    ON CONFLICT on (spot_id, conditions_hash) — update response_text so
    stale entries are refreshed on conditions change.
    """
    stmt = (
        insert(ResponseCache)
        .values(
            spot_id=spot_id,
            conditions_hash=conditions_hash,
            response_text=response_text,
        )
        .on_conflict_do_update(
            index_elements=["spot_id", "conditions_hash"],
            set_={"response_text": response_text},
        )
    )
    await db.execute(stmt)
    await db.commit()
    log.debug("response_cache_stored", extra={"spot_id": str(spot_id)})
