"""ROB-713 — read-only MCP surface for setup-tagged trade-journal aggregates."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.trade_journal.aggregates import build_trading_scoreboard

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


async def get_trading_scoreboard(
    market: str | None = None,
    account_mode: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    setup_tag: str | None = None,
    min_sample: int = 1,
) -> dict[str, Any]:
    try:
        async with _session_factory()() as db:
            return await build_trading_scoreboard(
                db,
                market=market,
                account_mode=account_mode,
                date_from=_parse_date(date_from),
                date_to=_parse_date(date_to),
                setup_tag=setup_tag,
                min_sample=min_sample,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_trading_scoreboard failed")
        return {
            "count": 0,
            "groups": [],
            "overall": None,
            "as_of": None,
            "error": str(exc),
        }
