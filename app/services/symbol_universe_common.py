from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def normalize_name(value: str) -> str:
    return str(value or "").strip()


def sync_hint(command: str) -> str:
    return f"Sync required: {command}"


async def has_any_rows(db: AsyncSession, column: Any) -> bool:
    result = await db.execute(select(column).limit(1))
    return result.scalar_one_or_none() is not None
