"""
Auth router — magic link request, verification, refresh, logout, model health.
"""

import logging

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from auth.service import (
    invalidate_session,
    request_magic_link,
    rotate_refresh_token,
    verify_magic_link,
)
from config import settings
from db.connection import get_db
from db.models import User

log = logging.getLogger(__name__)
router = APIRouter()

_REFRESH_COOKIE = "refresh_token"
_COOKIE_OPTS = dict(
    httponly=True,
    secure=True,
    samesite="lax",
    max_age=90 * 24 * 60 * 60,  # 90 days in seconds
    path="/api/auth",
)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class MagicLinkRequest(BaseModel):
    email: EmailStr


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/magic-link", status_code=status.HTTP_202_ACCEPTED)
async def request_link(
    body: MagicLinkRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Send a magic link to the provided email address.
    Always returns 202 regardless of whether the email exists (anti-enumeration).
    """
    base_url = str(request.base_url).rstrip("/")
    await request_magic_link(email=body.email, db=db, base_url=base_url)
    return {"detail": "If that email is registered, a sign-in link has been sent."}


@router.get("/verify")
async def verify_token(
    token: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Validate a magic link token. On success:
    - Sets httpOnly refresh_token cookie
    - Returns access JWT + onboarding flag
    """
    try:
        user, access_token, refresh_token = await verify_magic_link(token, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    response.set_cookie(_REFRESH_COOKIE, refresh_token, **_COOKIE_OPTS)
    onboarding_required = not user.preferences

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "onboarding_required": onboarding_required,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
    }


@router.post("/refresh")
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    """
    Rotate refresh token and return a new access JWT.
    The old refresh token is invalidated immediately.
    """
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    try:
        user, access_token, new_refresh = await rotate_refresh_token(refresh_token, db)
    except ValueError as exc:
        response.delete_cookie(_REFRESH_COOKIE, path="/api/auth")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    response.set_cookie(_REFRESH_COOKIE, new_refresh, **_COOKIE_OPTS)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    if refresh_token:
        await invalidate_session(refresh_token, db)
    response.delete_cookie(_REFRESH_COOKIE, path="/api/auth")


# ---------------------------------------------------------------------------
# Ollama model health — authenticated, not publicly accessible (§6.1 / §10.2)
# ---------------------------------------------------------------------------

@router.get("/health/models")
async def health_models(current_user: User = Depends(get_current_user)):
    """
    Returns currently loaded Ollama models and their keep_alive status.
    Requires valid access JWT.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/ps")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        log.warning("ollama_health_check_failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ollama not reachable",
        )
