"""
Spots API — list, detail, search, and creation endpoints.

GET  /api/spots                    — paginated list, sorted by score desc
GET  /api/spots/search?q=yakima    — fuzzy name search (pg_trgm)
GET  /api/spots/unresolved         — spots needing geocoding (seed_confidence=unvalidated, null coords)
GET  /api/spots/{spot_id}          — full detail with closures and regs
POST /api/spots                    — create a new spot from user input (debrief / manual)
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from db.connection import get_db
from db.models import User
from spots.service import (
    create_spot,
    get_spot,
    get_spot_closures,
    list_saved_spots,
    list_spots,
    list_unresolved_spots,
    save_spot,
    search_spots,
    unsave_spot,
)

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response serialisers
# ---------------------------------------------------------------------------

def _spot_summary(wb) -> dict:
    return {
        "id": str(wb.id),
        "name": wb.name,
        "type": wb.type,
        "latitude": float(wb.latitude) if wb.latitude is not None else None,
        "longitude": float(wb.longitude) if wb.longitude is not None else None,
        "county": wb.county,
        "score": float(wb.score) if wb.score is not None else 0.0,
        "fly_fishing_legal": wb.fly_fishing_legal,
        "seed_confidence": wb.seed_confidence,
        "has_realtime_conditions": wb.has_realtime_conditions,
        "last_stocked_date": wb.last_stocked_date.isoformat() if wb.last_stocked_date else None,
    }


def _spot_detail(wb, closures: list) -> dict:
    base = _spot_summary(wb)
    base.update({
        "aliases": wb.aliases or [],
        "elevation_ft": wb.elevation_ft,
        "is_alpine": wb.is_alpine,
        "permit_required": wb.permit_required,
        "permit_url": wb.permit_url,
        "species_primary": wb.species_primary or [],
        "min_cfs": wb.min_cfs,
        "max_cfs": wb.max_cfs,
        "min_temp_f": float(wb.min_temp_f) if wb.min_temp_f is not None else None,
        "max_temp_f": float(wb.max_temp_f) if wb.max_temp_f is not None else None,
        "fishing_regs": wb.fishing_regs,
        "last_stocked_species": wb.last_stocked_species or [],
        "wta_trail_url": wb.wta_trail_url,
        "score_updated": wb.score_updated.isoformat() if wb.score_updated else None,
        "emergency_closures": [
            {
                "rule_text": c.rule_text,
                "effective": c.effective.isoformat() if c.effective else None,
                "expires": c.expires.isoformat() if c.expires else None,
                "source_url": c.source_url,
            }
            for c in closures
        ],
    })
    return base


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_spots_endpoint(
    type: str | None = Query(None, description="river | lake | creek | coastal"),
    fly_only: bool = Query(False, description="Only return spots where fly fishing is legal"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    spots = await list_spots(db, type_filter=type, fly_only=fly_only, limit=limit, offset=offset)
    return {"spots": [_spot_summary(s) for s in spots], "count": len(spots)}


@router.get("/search")
async def search_spots_endpoint(
    q: str = Query(..., min_length=1, description="Spot name search string"),
    limit: int = Query(10, ge=1, le=50),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    spots = await search_spots(q, db, limit=limit)
    return {"spots": [_spot_summary(s) for s in spots]}


@router.get("/unresolved")
async def list_unresolved_spots_endpoint(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return spots with null coordinates that need geocoding before they appear
    in recommendations. Created when a debrief references an unknown location.
    """
    spots = await list_unresolved_spots(db)
    return {
        "spots": [
            {
                "id": str(s.id),
                "name": s.name,
                "type": s.type,
                "source": s.source,
                "seed_confidence": s.seed_confidence,
            }
            for s in spots
        ]
    }


@router.post("", status_code=201)
async def create_spot_endpoint(
    body: dict,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new spot from user input.

    Body: {"name": "...", "type": "river"|"lake"|"creek"|"coastal"}

    Spot is created with seed_confidence='unvalidated' and null coordinates.
    It will appear in GET /api/spots/unresolved until geocoded.
    """
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    spot_type = body.get("type", "river")
    if spot_type not in ("river", "lake", "creek", "coastal"):
        raise HTTPException(status_code=400, detail="type must be river, lake, creek, or coastal")
    spot = await create_spot(name=name, spot_type=spot_type, db=db)
    await db.commit()
    return {"spot_id": str(spot.id), "name": spot.name, "type": spot.type}


@router.get("/{spot_id}")
async def get_spot_endpoint(
    spot_id: UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    spot = await get_spot(spot_id, db)
    if not spot:
        raise HTTPException(status_code=404, detail="Spot not found")
    closures = await get_spot_closures(spot_id, db)
    return _spot_detail(spot, closures)


@router.post("/{spot_id}/save", status_code=201)
async def save_spot_endpoint(
    spot_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save a spot for the current user. Idempotent."""
    try:
        saved = await save_spot(current_user.id, spot_id, db)
    except ValueError:
        raise HTTPException(status_code=404, detail="Spot not found")
    await db.commit()
    return {"spot_id": str(spot_id), "saved_at": saved.saved_at.isoformat() if saved.saved_at else None}


@router.delete("/{spot_id}/save", status_code=204)
async def unsave_spot_endpoint(
    spot_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a spot from the current user's saved list."""
    await unsave_spot(current_user.id, spot_id, db)
    await db.commit()
