from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import MCP_USER_ID
from app.models.user_settings import UserSetting


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _serialize_setting(row: UserSetting) -> dict[str, Any]:
    """Serialize a UserSetting row to the expected response format."""
    return {
        "key": row.key,
        "value": row.value,
        "updated_at": row.updated_at.isoformat(),
    }


async def _get_setting_row(key: str) -> UserSetting | None:
    """Get a setting row by key for the default MCP user."""
    async with _session_factory()() as session:
        stmt = select(UserSetting).where(
            UserSetting.user_id == MCP_USER_ID,
            UserSetting.key == key,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_user_setting(key: str) -> Any | None:
    """Get a user setting value by key.

    Returns the JSON value if found, None otherwise.
    """
    if not key or not key.strip():
        raise ValueError("key is required")

    row = await _get_setting_row(key.strip())
    if row is None:
        return None
    return row.value


async def set_user_setting(key: str, value: Any) -> dict[str, Any]:
    """Set a user setting value by key (upsert).

    Returns the serialized setting with key, value, and updated_at.
    """
    if not key or not key.strip():
        raise ValueError("key is required")

    key = key.strip()

    async with _session_factory()() as session:
        # Use PostgreSQL upsert (INSERT ... ON CONFLICT DO UPDATE)
        upsert_stmt = (
            insert(UserSetting)
            .values(
                user_id=MCP_USER_ID,
                key=key,
                value=value,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "key"],
                set_={
                    "value": value,
                    # updated_at is handled by onupdate=func.now() on the column
                },
            )
        )
        await session.execute(upsert_stmt)
        await session.commit()

        # Fetch the updated row to return serialized data
        row = await session.execute(
            select(UserSetting).where(
                UserSetting.user_id == MCP_USER_ID,
                UserSetting.key == key,
            )
        )
        setting = row.scalar_one()
        return _serialize_setting(setting)


async def get_manual_cash_setting() -> dict[str, Any] | None:
    """Get the manual cash setting for the default MCP user.

    Returns the full setting dict with key, value, and updated_at,
    or None if not set.
    """
    row = await _get_setting_row("manual_cash")
    if row is None:
        return None
    return _serialize_setting(row)
