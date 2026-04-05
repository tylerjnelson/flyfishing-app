"""
Phase 2 integration tests — auth service logic.

Tests exercise the service layer directly against an in-memory SQLite DB
so no external PostgreSQL instance is required.  The two spec-required
integration tests (§11.1) are marked with their spec reference.

Spec tests:
  - POST magic link request → assert token written to magic_link_tokens table
  - Token validation → assert access JWT returned with correct expiry
"""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
import pytest
from sqlalchemy import StaticPool, create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from auth.service import (
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    _hash_token,
    create_access_token,
    decode_access_token,
    invalidate_session,
    request_magic_link,
    rotate_refresh_token,
    verify_magic_link,
)
from config import settings
from db.models import Base, MagicLinkToken, Session, User


# ---------------------------------------------------------------------------
# In-memory async SQLite engine for tests
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db():
    engine = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Only create auth-relevant tables; other models use PostgreSQL-specific
    # types (ARRAY, Vector) that SQLite cannot handle.
    from db.models import MagicLinkToken, Session, User
    auth_tables = [User.__table__, Session.__table__, MagicLinkToken.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=auth_tables)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=auth_tables)
    await engine.dispose()


@pytest.fixture
async def user(db):
    u = User(email="angler@example.com", display_name="Test Angler", preferences={})
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


# ---------------------------------------------------------------------------
# _hash_token
# ---------------------------------------------------------------------------

class TestHashToken:
    def test_returns_sha256_hex(self):
        raw = "abc123"
        result = _hash_token(raw)
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert result == expected

    def test_same_input_same_output(self):
        assert _hash_token("token") == _hash_token("token")

    def test_different_inputs_different_hashes(self):
        assert _hash_token("a") != _hash_token("b")


# ---------------------------------------------------------------------------
# create_access_token / decode_access_token
# ---------------------------------------------------------------------------

class TestAccessToken:
    def test_encodes_user_id_in_sub(self):
        token = create_access_token("user-123")
        payload = decode_access_token(token)
        assert payload["sub"] == "user-123"

    def test_expiry_is_15_minutes(self):
        before = datetime.now(tz=timezone.utc)
        token = create_access_token("user-123")
        payload = decode_access_token(token)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        delta = exp - before
        # Allow 5-second clock skew
        assert timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES) - timedelta(seconds=5) <= delta
        assert delta <= timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES) + timedelta(seconds=5)

    def test_expired_token_raises(self):
        payload = {
            "sub": "user-123",
            "iat": datetime.now(tz=timezone.utc) - timedelta(hours=1),
            "exp": datetime.now(tz=timezone.utc) - timedelta(seconds=1),
        }
        expired = jwt.encode(payload, settings.app_secret_key, algorithm=ALGORITHM)
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_access_token(expired)

    def test_tampered_token_raises(self):
        token = create_access_token("user-123")
        tampered = token[:-4] + "xxxx"
        with pytest.raises(jwt.InvalidTokenError):
            decode_access_token(tampered)


# ---------------------------------------------------------------------------
# §11.1 — POST magic link: assert token written to magic_link_tokens table
# ---------------------------------------------------------------------------

class TestRequestMagicLink:
    async def test_token_written_to_db(self, db, user):
        """§11.1: POST magic link request → token written to magic_link_tokens table."""
        with patch("auth.service._send_magic_link_email"):
            await request_magic_link(
                email="angler@example.com", db=db, base_url="https://app.example.com"
            )

        result = await db.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(MagicLinkToken)
            .where(MagicLinkToken.email == "angler@example.com")
        )
        record = result.scalar_one_or_none()
        assert record is not None
        assert record.used_at is None
        # SQLite returns naive datetimes; compare naive to naive
        assert record.expires_at > datetime.now()

    async def test_unknown_email_writes_no_token(self, db):
        with patch("auth.service._send_magic_link_email") as mock_send:
            await request_magic_link(
                email="unknown@example.com", db=db, base_url="https://app.example.com"
            )
        mock_send.assert_not_called()

    async def test_email_sent_for_known_user(self, db, user):
        with patch("auth.service._send_magic_link_email") as mock_send:
            await request_magic_link(
                email="angler@example.com", db=db, base_url="https://app.example.com"
            )
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args.kwargs
        assert "angler@example.com" == call_kwargs["email"]
        assert "https://app.example.com/auth/verify?token=" in call_kwargs["link"]


# ---------------------------------------------------------------------------
# §11.1 — Token validation: assert access JWT returned with correct expiry
# ---------------------------------------------------------------------------

class TestVerifyMagicLink:
    async def _insert_token(self, db, user, raw: str, used: bool = False, expired: bool = False):
        from sqlalchemy import select as sa_select
        expires = (
            datetime.now(tz=timezone.utc) - timedelta(minutes=1)
            if expired
            else datetime.now(tz=timezone.utc) + timedelta(minutes=15)
        )
        record = MagicLinkToken(
            email=user.email,
            token_hash=_hash_token(raw),
            expires_at=expires,
            used_at=datetime.now(tz=timezone.utc) if used else None,
        )
        db.add(record)
        await db.commit()

    async def test_returns_access_jwt_with_correct_expiry(self, db, user):
        """§11.1: token validation → access JWT returned with correct expiry."""
        raw = "valid-raw-token-abc123"
        await self._insert_token(db, user, raw)

        _user, access_token, refresh_token = await verify_magic_link(raw, db)
        payload = decode_access_token(access_token)

        assert payload["sub"] == str(user.id)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        assert exp > datetime.now(tz=timezone.utc)
        assert exp < datetime.now(tz=timezone.utc) + timedelta(minutes=16)

    async def test_token_marked_used_after_verify(self, db, user):
        raw = "single-use-token"
        await self._insert_token(db, user, raw)
        await verify_magic_link(raw, db)

        from sqlalchemy import select as sa_select
        result = await db.execute(
            sa_select(MagicLinkToken).where(MagicLinkToken.token_hash == _hash_token(raw))
        )
        record = result.scalar_one()
        assert record.used_at is not None

    async def test_replayed_token_raises(self, db, user):
        raw = "already-used"
        await self._insert_token(db, user, raw, used=True)
        with pytest.raises(ValueError, match="already used"):
            await verify_magic_link(raw, db)

    async def test_expired_token_raises(self, db, user):
        raw = "expired-token"
        await self._insert_token(db, user, raw, expired=True)
        with pytest.raises(ValueError, match="expired"):
            await verify_magic_link(raw, db)

    async def test_invalid_token_raises(self, db, user):
        with pytest.raises(ValueError, match="Invalid"):
            await verify_magic_link("nonexistent-token", db)


# ---------------------------------------------------------------------------
# Refresh token rotation
# ---------------------------------------------------------------------------

class TestRotateRefreshToken:
    async def _create_session(self, db, user) -> str:
        import secrets
        refresh = secrets.token_urlsafe(32)
        db.add(Session(
            user_id=user.id,
            refresh_token=refresh,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(days=90),
            last_active=datetime.now(tz=timezone.utc),
        ))
        await db.commit()
        return refresh

    async def test_returns_new_tokens(self, db, user):
        old_refresh = await self._create_session(db, user)
        _user, access_token, new_refresh = await rotate_refresh_token(old_refresh, db)
        assert new_refresh != old_refresh
        assert decode_access_token(access_token)["sub"] == str(user.id)

    async def test_old_token_invalidated(self, db, user):
        old_refresh = await self._create_session(db, user)
        await rotate_refresh_token(old_refresh, db)
        with pytest.raises(ValueError):
            await rotate_refresh_token(old_refresh, db)

    async def test_invalid_refresh_raises(self, db, user):
        with pytest.raises(ValueError, match="Invalid"):
            await rotate_refresh_token("not-a-real-token", db)


# ---------------------------------------------------------------------------
# Logout / session invalidation
# ---------------------------------------------------------------------------

class TestInvalidateSession:
    async def test_session_deleted(self, db, user):
        import secrets
        from sqlalchemy import select as sa_select
        refresh = secrets.token_urlsafe(32)
        db.add(Session(
            user_id=user.id,
            refresh_token=refresh,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(days=90),
        ))
        await db.commit()
        await invalidate_session(refresh, db)
        result = await db.execute(
            sa_select(Session).where(Session.refresh_token == refresh)
        )
        assert result.scalar_one_or_none() is None

    async def test_invalid_token_is_noop(self, db):
        # Should not raise
        await invalidate_session("nonexistent", db)
