from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from auth.middleware import get_current_user
from conditions import here_budget
from config import settings
from db.connection import get_db
from db.models import User
from spots.service import get_spot, list_saved_spots
from users.service import update_display_name, update_preferences

router = APIRouter()


class ProfileUpdate(BaseModel):
    display_name: str | None = None
    preferences: dict | None = None


@router.get("/geocode")
async def geocode_location(
    q: str = Query(..., min_length=2),
    current_user: User = Depends(get_current_user),
):
    """Geocode a free-text location string via HERE. Returns {label, lat, lon} or 404."""
    # Counts against the monthly HERE budget (§19.6). If the cap is exhausted,
    # report the location as unresolved rather than issuing a billable call.
    if await here_budget.reserve(1) == 0:
        return {"result": None}
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        resp = await client.get(
            "https://geocode.search.hereapi.com/v1/geocode",
            params={"q": q, "in": "countryCode:USA", "apiKey": settings.here_api_key},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    if not items:
        return {"result": None}
    item = items[0]
    pos = item.get("position", {})
    return {
        "result": {
            "label": item.get("address", {}).get("label", q),
            "lat": pos.get("lat"),
            "lon": pos.get("lng"),
        }
    }


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "display_name": current_user.display_name,
        "preferences": current_user.preferences or {},
        "created_at": current_user.created_at.isoformat(),
    }


@router.get("/me/saved-spots")
async def get_saved_spots(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return all spots saved by the current user with spot details, most-recently saved first."""
    saved = await list_saved_spots(current_user.id, db)
    result = []
    for s in saved:
        spot = await get_spot(s.spot_id, db)
        if not spot:
            continue
        result.append({
            "spot_id": str(s.spot_id),
            "saved_at": s.saved_at.isoformat() if s.saved_at else None,
            "name": spot.name,
            "type": spot.type,
            "county": spot.county,
            "score": float(spot.score) if spot.score is not None else None,
            "fly_fishing_legal": spot.fly_fishing_legal,
            "latitude": float(spot.latitude) if spot.latitude is not None else None,
            "longitude": float(spot.longitude) if spot.longitude is not None else None,
            "has_realtime_conditions": spot.has_realtime_conditions,
        })
    return {"saved_spots": result}


@router.patch("/me")
async def update_me(
    body: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.display_name is not None:
        current_user = await update_display_name(current_user, body.display_name, db)
    if body.preferences is not None:
        current_user = await update_preferences(current_user, body.preferences, db)
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "display_name": current_user.display_name,
        "preferences": current_user.preferences or {},
    }
