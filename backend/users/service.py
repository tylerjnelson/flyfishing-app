import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User

log = logging.getLogger(__name__)


async def get_user_by_id(user_id: UUID, db: AsyncSession) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_email(email: str, db: AsyncSession) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def update_preferences(
    user: User, preferences: dict, db: AsyncSession
) -> User:
    """
    Merge new preferences into existing ones and persist.
    Merges at top level — does not deep-merge nested keys.
    """
    updated = {**(user.preferences or {}), **preferences}
    user.preferences = updated
    await db.commit()
    await db.refresh(user)
    log.info("preferences_updated", extra={"user_id": str(user.id)})
    return user


async def update_display_name(
    user: User, display_name: str, db: AsyncSession
) -> User:
    user.display_name = display_name
    await db.commit()
    await db.refresh(user)
    return user
