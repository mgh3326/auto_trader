from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_user_settings_table() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("SELECT to_regclass('user_settings')"))
            if row.scalar_one_or_none() is None:
                pytest.skip("user_settings table is not migrated")
    except Exception:
        pytest.skip("database is not available for integration persistence checks")


async def _create_user() -> int:
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (username, email, role, tz, base_currency, is_active)
                    VALUES (:username, :email, 'viewer', 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {
                    "username": f"user_settings_test_{suffix}",
                    "email": f"user_settings_{suffix}@example.com",
                },
            )
        ).scalar_one()
        await session.commit()
        return user_id


async def _cleanup_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id}
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_settings_unique_per_user_and_key() -> None:
    """Each (user_id, key) pair must be unique."""
    await _ensure_user_settings_table()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO user_settings (user_id, key, value)
                    VALUES (:user_id, 'manual_cash', '{"amount": 1000000}'::jsonb)
                    """
                ),
                {"user_id": user_id},
            )
            await session.commit()

        async with SessionLocal() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        """
                        INSERT INTO user_settings (user_id, key, value)
                        VALUES (:user_id, 'manual_cash', '{"amount": 2000000}'::jsonb)
                        """
                    ),
                    {"user_id": user_id},
                )
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_settings_cascades_on_user_delete() -> None:
    """User settings must be deleted when the user is deleted."""
    await _ensure_user_settings_table()
    user_id = await _create_user()

    async with SessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO user_settings (user_id, key, value)
                VALUES (:user_id, 'manual_cash', '{"amount": 1000000}'::jsonb)
                """
            ),
            {"user_id": user_id},
        )
        await session.commit()

    async with SessionLocal() as session:
        count_before = (
            await session.execute(
                text("SELECT COUNT(*) FROM user_settings WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
        ).scalar_one()
        assert count_before == 1

    await _cleanup_user(user_id)

    async with SessionLocal() as session:
        count_after = (
            await session.execute(
                text("SELECT COUNT(*) FROM user_settings WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
        ).scalar_one()
        assert count_after == 0
