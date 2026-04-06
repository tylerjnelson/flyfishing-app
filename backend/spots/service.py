"""
Spot query service — list, detail, and search.
"""

import logging
from datetime import date
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EmergencyClosure, Spot

log = logging.getLogger(__name__)


async def list_spots(
    db: AsyncSession,
    *,
    type_filter: str | None = None,
    fly_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Spot]:
    """Return spots sorted by score desc, with optional type and legality filters."""
    q = select(Spot)
    if type_filter:
        q = q.where(Spot.type == type_filter)
    if fly_only:
        q = q.where(Spot.fly_fishing_legal.is_(True))
    q = q.order_by(Spot.score.desc()).limit(limit).offset(offset)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_spot(spot_id: UUID, db: AsyncSession) -> Spot | None:
    result = await db.execute(select(Spot).where(Spot.id == spot_id))
    return result.scalar_one_or_none()


async def get_spot_closures(spot_id: UUID, db: AsyncSession) -> list[EmergencyClosure]:
    """Return active (non-expired) closures for a spot."""
    result = await db.execute(
        select(EmergencyClosure)
        .where(EmergencyClosure.spot_id == spot_id)
        .where(
            EmergencyClosure.expires.is_(None)
            | (EmergencyClosure.expires >= date.today())
        )
        .order_by(EmergencyClosure.effective)
    )
    return list(result.scalars().all())


async def search_spots(query: str, db: AsyncSession, *, limit: int = 10) -> list[Spot]:
    """
    Fuzzy name search via pg_trgm similarity.
    Falls back to ilike prefix match if no trgm hits above 0.1 threshold.
    """
    clean = query.strip()
    if not clean:
        return []

    # pg_trgm similarity — index active from Phase 1 migration
    trgm_result = await db.execute(
        text(
            "SELECT id FROM spots "
            "WHERE similarity(name, :q) > 0.1 "
            "ORDER BY similarity(name, :q) DESC "
            "LIMIT :limit"
        ),
        {"q": clean, "limit": limit},
    )
    ids = [row[0] for row in trgm_result.all()]

    if ids:
        result = await db.execute(select(Spot).where(Spot.id.in_(ids)))
        spots_by_id = {str(s.id): s for s in result.scalars().all()}
        # Preserve trgm relevance order
        return [spots_by_id[str(i)] for i in ids if str(i) in spots_by_id]

    # Fallback: prefix ilike for short queries or low-similarity cases
    result = await db.execute(
        select(Spot).where(Spot.name.ilike(f"{clean}%")).limit(limit)
    )
    return list(result.scalars().all())
