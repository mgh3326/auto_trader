from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import MCP_USER_ID
from app.models.user_settings import UserSetting
from app.services.manual_cash_settings import (
    MANUAL_CASH_KEY,
    normalize_generic_manual_cash_write,
)
from app.services.parking_exclusion_settings import (
    PARKING_EXCLUSION_CURRENCIES,
    PARKING_EXCLUSION_KEY,
    normalize_generic_parking_exclusion_write,
    parse_parking_exclusion_value,
)


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
    if key == MANUAL_CASH_KEY:
        # manual_cash is the deployment-cap parking term (#671): bounded amount,
        # writer-stamped provenance. Raises ValueError on an invalid value.
        value = normalize_generic_manual_cash_write(value)
    elif key == PARKING_EXCLUSION_KEY:
        # parking_exclusion (#883) feeds the cash-sweep exclusion read; the
        # same closed shape as the reader is enforced on writes so a typo
        # cannot be stored and later surface as "unknown". Raises ValueError
        # on an invalid value.
        value = normalize_generic_parking_exclusion_write(value)

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
                    "updated_at": func.now(),
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
    row = await _get_setting_row(MANUAL_CASH_KEY)
    if row is None:
        return None
    return _serialize_setting(row)


async def get_parking_exclusion() -> dict[str, Any]:
    """Read the operator-set ``parking_exclusion`` user setting (#883).

    Typed single-key read for the cash-sweep playbook — it takes no key
    parameter and can only ever read ``user_settings.parking_exclusion`` for
    the MCP user. Closed ``status`` vocabulary:

    * ``"ok"``      — ``exclusions`` maps every sweep currency to an exact
      decimal string; a currency absent from the stored value reports ``"0"``.
    * ``"unknown"`` — the stored value is malformed or the read failed;
      ``exclusions`` is null. Callers must park nothing: an unknown exclusion
      is never equivalent to zero.
    """
    try:
        row = await _get_setting_row(PARKING_EXCLUSION_KEY)
    except Exception:  # noqa: BLE001 - no DB detail in the tool result
        return {
            "success": False,
            "status": "unknown",
            "exclusions": None,
            "reason": "read_failed",
            "updated_at": None,
        }
    if row is None:
        return {
            "success": True,
            "status": "ok",
            "exclusions": dict.fromkeys(PARKING_EXCLUSION_CURRENCIES, "0"),
            "reason": None,
            "updated_at": None,
        }
    parsed = parse_parking_exclusion_value(row.value)
    if parsed is None:
        return {
            "success": True,
            "status": "unknown",
            "exclusions": None,
            "reason": "malformed_value",
            "updated_at": row.updated_at.isoformat(),
        }
    return {
        "success": True,
        "status": "ok",
        "exclusions": {
            currency: format(parsed.get(currency, Decimal(0)), "f")
            for currency in PARKING_EXCLUSION_CURRENCIES
        },
        "reason": None,
        "updated_at": row.updated_at.isoformat(),
    }
