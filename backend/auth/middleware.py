"""
Auth middleware — JWT validation FastAPI dependency.

get_current_user() is the standard dependency injected into any route that
requires authentication.  Routes that allow unauthenticated access (login,
verify, refresh) do not use this dependency.
"""

import logging

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.service import decode_access_token
from db.connection import get_db
from db.models import User

log = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/magic-link")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Validate the Bearer JWT and return the authenticated User.
    Raises 401 on any validation failure.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id: str = payload.get("sub")
        if not user_id:
            raise credentials_exception
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise credentials_exception

    return user
