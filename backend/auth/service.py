"""
Auth service — magic link generation, JWT creation, session management.

Magic link tokens:
  - Raw token: secrets.token_urlsafe(32) — sent in email, never stored
  - Stored: SHA-256 hash of raw token (token_hash column)
  - Expires: 15 minutes after generation
  - Single-use: used_at set on first valid click; replay returns 401

Session tokens:
  - Refresh token: secrets.token_urlsafe(32) — stored plain in sessions table,
    sent as httpOnly cookie; rotated on every use
  - Access token: JWT HS256, 15-min expiry, memory-only on the client
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

import jwt
import resend
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import MagicLinkToken, Session, User

log = logging.getLogger(__name__)

ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 90
ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Token utilities
# ---------------------------------------------------------------------------

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_access_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.now(tz=timezone.utc),
        "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure."""
    return jwt.decode(token, settings.app_secret_key, algorithms=[ALGORITHM])


# ---------------------------------------------------------------------------
# Magic link
# ---------------------------------------------------------------------------

async def request_magic_link(email: str, db: AsyncSession, base_url: str) -> None:
    """
    Generate a magic link token and send it via Resend.

    Invite-only: silently does nothing if email is not in the users table.
    Same response either way — prevents email enumeration.
    """
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        log.info("magic_link_unknown_email", extra={"email": email})
        return

    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=15)

    db.add(MagicLinkToken(
        email=email,
        token_hash=token_hash,
        expires_at=expires_at,
    ))
    await db.commit()

    link = f"{base_url}/auth/verify?token={raw}"
    _send_magic_link_email(email=email, link=link, display_name=user.display_name)
    log.info("magic_link_sent", extra={"user_id": str(user.id)})


def _send_magic_link_email(email: str, link: str, display_name: str | None) -> None:
    resend.api_key = settings.resend_api_key
    name = display_name or "there"
    resend.Emails.send({
        "from": settings.mail_from,
        "to": [email],
        "subject": "Your Fly Fish WA sign-in link",
        "html": (
            f"<p>Hey {name},</p>"
            f"<p><a href='{link}'>Click here to sign in to Fly Fish WA</a></p>"
            f"<p>This link expires in 15 minutes and can only be used once.</p>"
        ),
    })


# ---------------------------------------------------------------------------
# Token verification + session creation
# ---------------------------------------------------------------------------

async def verify_magic_link(
    raw_token: str, db: AsyncSession
) -> tuple[User, str, str]:
    """
    Validate a raw magic link token.

    Returns (user, access_token, refresh_token).
    Raises ValueError with a safe message on any failure.
    """
    token_hash = _hash_token(raw_token)
    result = await db.execute(
        select(MagicLinkToken).where(MagicLinkToken.token_hash == token_hash)
    )
    record = result.scalar_one_or_none()

    if not record:
        raise ValueError("Invalid token")
    if record.used_at is not None:
        raise ValueError("Token already used")
    if record.expires_at < datetime.now(tz=timezone.utc):
        raise ValueError("Token expired")

    # Mark used — single-use enforcement
    record.used_at = datetime.now(tz=timezone.utc)

    # Look up user
    result = await db.execute(select(User).where(User.email == record.email))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise ValueError("User not found or inactive")

    # Create session
    refresh_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    db.add(Session(
        user_id=user.id,
        refresh_token=refresh_token,
        expires_at=expires_at,
        last_active=datetime.now(tz=timezone.utc),
    ))
    await db.commit()

    access_token = create_access_token(str(user.id))
    log.info("magic_link_verified", extra={"user_id": str(user.id)})
    return user, access_token, refresh_token


# ---------------------------------------------------------------------------
# Refresh token rotation
# ---------------------------------------------------------------------------

async def rotate_refresh_token(
    current_refresh_token: str, db: AsyncSession
) -> tuple[User, str, str]:
    """
    Validate and rotate a refresh token.

    Returns (user, new_access_token, new_refresh_token).
    Raises ValueError on invalid/expired token.
    """
    result = await db.execute(
        select(Session).where(Session.refresh_token == current_refresh_token)
    )
    session = result.scalar_one_or_none()

    if not session:
        raise ValueError("Invalid refresh token")
    if session.expires_at < datetime.now(tz=timezone.utc):
        raise ValueError("Refresh token expired")

    result = await db.execute(select(User).where(User.id == session.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise ValueError("User not found or inactive")

    # Rotate — invalidate old, issue new
    new_refresh = secrets.token_urlsafe(32)
    session.refresh_token = new_refresh
    session.expires_at = datetime.now(tz=timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    session.last_active = datetime.now(tz=timezone.utc)
    await db.commit()

    access_token = create_access_token(str(user.id))
    return user, access_token, new_refresh


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

async def invalidate_session(refresh_token: str, db: AsyncSession) -> None:
    result = await db.execute(
        select(Session).where(Session.refresh_token == refresh_token)
    )
    session = result.scalar_one_or_none()
    if session:
        await db.delete(session)
        await db.commit()
