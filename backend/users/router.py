from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from db.connection import get_db
from db.models import User
from users.service import update_display_name, update_preferences

router = APIRouter()


class ProfileUpdate(BaseModel):
    display_name: str | None = None
    preferences: dict | None = None


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "display_name": current_user.display_name,
        "preferences": current_user.preferences or {},
        "created_at": current_user.created_at.isoformat(),
    }


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
