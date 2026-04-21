"""
Response cache — §2.4, §6.3 Step 3.

Cache key: (fishing_spot_id, conditions_hash)
"""

import logging

from sqlalchemy import select

from db.models import ResponseCache

log = logging.getLogger(__name__)


async def get_cached_response(
    db, fishing_spot_id: str, conditions_hash: str
) -> str | None:
    """Return cached LLM response text, or None on cache miss."""
    result = await db.execute(
        select(ResponseCache.response_text).where(
            ResponseCache.fishing_spot_id == fishing_spot_id,
            ResponseCache.conditions_hash == conditions_hash,
        )
    )
    row = result.one_or_none()
    if row:
        log.debug("response_cache_hit", extra={"fishing_spot_id": str(fishing_spot_id)})
        return row.response_text
    return None


async def store_response(
    db, fishing_spot_id: str, conditions_hash: str, response_text: str
) -> None:
    """Insert a response into the cache."""
    from sqlalchemy.dialects.postgresql import insert
    stmt = (
        insert(ResponseCache)
        .values(
            fishing_spot_id=fishing_spot_id,
            conditions_hash=conditions_hash,
            response_text=response_text,
        )
        .on_conflict_do_nothing()
    )
    await db.execute(stmt)
    await db.commit()
    log.debug("response_cache_stored", extra={"fishing_spot_id": str(fishing_spot_id)})
