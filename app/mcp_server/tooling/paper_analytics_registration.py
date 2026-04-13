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
    @mcp.tool(
        name="get_paper_performance",
        description=(
            "Return paper trading account performance: total_return_pct, realized/unrealized PnL, "
            "total_trades (closed round trips), win_rate, avg_holding_days, max_drawdown_pct, "
            "sharpe_ratio (annualised, 252 trading days), best_trade, worst_trade. "
            "period ∈ {1d, 1w, 1m, 3m, all}. "
            "Drawdown/Sharpe require PaperDailySnapshot rows in the period; otherwise null."
        ),
    )
    async def get_paper_performance(
        name: str,
        period: str = "all",
    ) -> dict[str, Any]:
        try:
            today = now_kst().date()
            start = _parse_period(period, today)
            end = None if period == "all" else today
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        async with _session_factory()() as db:
            service = PaperTradingService(db)
            account = await service.get_account_by_name(name)
            if account is None:
                return {
                    "success": False,
                    "error": f"Paper account '{name}' not found",
                }
            try:
                perf = await service.calculate_performance(
                    account_id=account.id, start_date=start, end_date=end
                )
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            return {
                "success": True,
                "account_name": name,
                "period": period,
                "performance": perf,
            }


__all__ = [
    "PAPER_ANALYTICS_TOOL_NAMES",
    "register_paper_analytics_tools",
    "_parse_period",
    "_session_factory",
    "_to_float",
]
