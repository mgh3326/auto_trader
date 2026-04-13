"""Paper Trading analytics MCP tool registration."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.paper_trading_service import PaperTradingService

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

PAPER_ANALYTICS_TOOL_NAMES: set[str] = {
    "get_paper_performance",
    "get_paper_trade_log",
    "compare_paper_accounts",
}

PeriodLiteral = Literal["1d", "1w", "1m", "3m", "all"]


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _parse_period(period: str, today: date) -> date | None:
    if period == "all":
        return None
    deltas = {"1d": 1, "1w": 7, "1m": 30, "3m": 90}
    if period not in deltas:
        raise ValueError(f"Unsupported period: {period}")
    return today - timedelta(days=deltas[period])


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    return float(v)


def register_paper_analytics_tools(mcp: FastMCP) -> None:
    pass  # Tools added in subsequent steps


__all__ = [
    "PAPER_ANALYTICS_TOOL_NAMES",
    "register_paper_analytics_tools",
    "_parse_period",
    "_session_factory",
    "_to_float",
]
