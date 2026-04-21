"""
Spot query service — list, detail, search, and creation.

list_spots / get_spot / search_spots operate on water_bodies (the display
and scoring entity). save/unsave operate on fishing_spots.
"""

import logging
import uuid
from datetime import date
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EmergencyClosure, FishingSpot, SavedSpot, Spot, WaterBody

log = logging.getLogger(__name__)


async def list_spots(
    db: AsyncSession,
    *,
    type_filter: str | None = None,
    fly_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[WaterBody]:
    """Return water bodies sorted by score desc, with optional type and legality filters."""
    q = select(WaterBody)
    if type_filter:
        q = q.where(WaterBody.type == type_filter)
    if fly_only:
        q = q.where(WaterBody.fly_fishing_legal.is_(True))
    q = q.order_by(WaterBody.score.desc()).limit(limit).offset(offset)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_spot(spot_id: UUID, db: AsyncSession) -> WaterBody | None:
    result = await db.execute(select(WaterBody).where(WaterBody.id == spot_id))
    return result.scalar_one_or_none()


async def get_spot_closures(spot_id: UUID, db: AsyncSession) -> list[EmergencyClosure]:
    """Return active (non-expired) closures for a water body."""
    result = await db.execute(
        select(EmergencyClosure)
        .where(EmergencyClosure.water_body_id == spot_id)
        .where(
            EmergencyClosure.expires.is_(None)
            | (EmergencyClosure.expires >= date.today())
        )
        .order_by(EmergencyClosure.effective)
    )
    return list(result.scalars().all())


async def create_spot(name: str, spot_type: str, db: AsyncSession) -> Spot:
    """
    Create a minimal spot from user input (debrief or manual entry).
    Still creates in the legacy spots table; resolves to water_body in Phase 6.
    """
    from rag.embedder import embed_text

    spot = Spot(
        id=uuid.uuid4(),
        name=name,
        type=spot_type,
        source="notes",
        seed_confidence="unvalidated",
    )
    db.add(spot)
    await db.flush()

    embedding = await embed_text(name)
    spot.name_embedding = embedding
    await db.flush()
    log.info("spot_created_from_note", extra={"spot_id": str(spot.id), "name": name})
    return spot


async def list_unresolved_spots(db: AsyncSession) -> list[Spot]:
    """Return legacy spots with unvalidated confidence and null coordinates."""
    result = await db.execute(
        select(Spot).where(
            Spot.seed_confidence == "unvalidated",
            Spot.latitude.is_(None),
        ).order_by(Spot.name)
    )
    return list(result.scalars().all())


async def save_spot(user_id: UUID, fishing_spot_id: UUID, db: AsyncSession) -> SavedSpot:
    """
    Save a fishing spot for a user. Idempotent.
    Raises ValueError if the fishing spot does not exist.
    """
    fs_result = await db.execute(select(FishingSpot).where(FishingSpot.id == fishing_spot_id))
    fs = fs_result.scalar_one_or_none()
    if not fs:
        raise ValueError("spot_not_found")

    existing = await db.execute(
        select(SavedSpot).where(
            SavedSpot.user_id == user_id,
            SavedSpot.fishing_spot_id == fishing_spot_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        return row

    saved = SavedSpot(id=uuid.uuid4(), user_id=user_id, fishing_spot_id=fishing_spot_id)
    db.add(saved)
    await db.flush()
    log.info("spot_saved", extra={"user_id": str(user_id), "fishing_spot_id": str(fishing_spot_id)})
    return saved


async def unsave_spot(user_id: UUID, fishing_spot_id: UUID, db: AsyncSession) -> bool:
    """Remove a saved spot. Returns True if deleted, False if it was not saved."""
    existing = await db.execute(
        select(SavedSpot).where(
            SavedSpot.user_id == user_id,
            SavedSpot.fishing_spot_id == fishing_spot_id,
        )
    )
    row = existing.scalar_one_or_none()
    if not row:
        return False
    await db.delete(row)
    await db.flush()
    log.info("spot_unsaved", extra={"user_id": str(user_id), "fishing_spot_id": str(fishing_spot_id)})
    return True


async def list_saved_spots(user_id: UUID, db: AsyncSession) -> list[SavedSpot]:
    """Return all saved spots for a user, ordered most-recently saved first."""
    result = await db.execute(
        select(SavedSpot)
        .where(SavedSpot.user_id == user_id)
        .order_by(SavedSpot.saved_at.desc())
    )
    return list(result.scalars().all())


async def search_spots(query: str, db: AsyncSession, *, limit: int = 10) -> list[WaterBody]:
    """
    Fuzzy name search via pg_trgm similarity against water_bodies.
    Falls back to ilike prefix match if no trgm hits above 0.1 threshold.
    """
    clean = query.strip()
    if not clean:
        return []

    trgm_result = await db.execute(
        text(
            "SELECT id FROM water_bodies "
            "WHERE similarity(name, :q) > 0.1 "
            "ORDER BY similarity(name, :q) DESC "
            "LIMIT :limit"
        ),
        {"q": clean, "limit": limit},
    )
    ids = [row[0] for row in trgm_result.all()]

    if ids:
        result = await db.execute(select(WaterBody).where(WaterBody.id.in_(ids)))
        wbs_by_id = {str(wb.id): wb for wb in result.scalars().all()}
        return [wbs_by_id[str(i)] for i in ids if str(i) in wbs_by_id]

    result = await db.execute(
        select(WaterBody).where(WaterBody.name.ilike(f"{clean}%")).limit(limit)
    )
    return list(result.scalars().all())
