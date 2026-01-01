"""Utilities for persisting and validating refresh tokens."""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.trading import RefreshToken


def hash_refresh_token(token: str) -> str:
    """Return a stable hash for storing refresh tokens securely."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def save_refresh_token(
    db: AsyncSession, user_id: int, refresh_token: str
) -> RefreshToken:
    """Persist a new refresh token record for the user."""
    expires_at = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    token_record = RefreshToken(
        user_id=user_id,
        token_hash=hash_refresh_token(refresh_token),
        expires_at=expires_at,
        revoked=False,
    )
    db.add(token_record)
    await db.flush()
    return token_record


async def get_valid_refresh_token(
    db: AsyncSession, user_id: int, refresh_token: str
) -> RefreshToken | None:
    """Return an active, non-revoked refresh token if it exists."""
    token_hash = hash_refresh_token(refresh_token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False,  # noqa: E712
        )
    )
    token_record = result.scalar_one_or_none()
    if not token_record:
        return None

    if token_record.expires_at <= datetime.now(UTC):
        return None

    return token_record


async def revoke_refresh_token(db: AsyncSession, token_record: RefreshToken) -> None:
    """Mark a single refresh token as revoked."""
    token_record.revoked = True


async def revoke_all_refresh_tokens(db: AsyncSession, user_id: int) -> int:
    """Revoke all active refresh tokens for a user. Returns affected rows."""
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked == False)  # noqa: E712
        .values(revoked=True)
    )
    return result.rowcount or 0
