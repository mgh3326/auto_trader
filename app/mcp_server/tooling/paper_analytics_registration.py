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

    @mcp.tool(
        name="get_paper_trade_log",
        description=(
            "Return paper trading execution history (most recent first) for a given account. "
            "Optional filters: symbol, days (look-back window), limit (default 50). "
            "Each row includes symbol, side, quantity, price, fee, realized_pnl, executed_at."
        ),
    )
    async def get_paper_trade_log(
        name: str,
        symbol: str | None = None,
        days: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        async with _session_factory()() as db:
            service = PaperTradingService(db)
            account = await service.get_account_by_name(name)
            if account is None:
                return {
                    "success": False,
                    "error": f"Paper account '{name}' not found",
                }

            rows = await service.get_trade_history(
                account_id=account.id, symbol=symbol, days=days, limit=limit
            )
            trades = [
                {
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "instrument_type": r["instrument_type"],
                    "side": r["side"],
                    "order_type": r["order_type"],
                    "quantity": _to_float(r.get("quantity")),
                    "price": _to_float(r.get("price")),
                    "total_amount": _to_float(r.get("total_amount")),
                    "fee": _to_float(r.get("fee")),
                    "currency": r["currency"],
                    "reason": r.get("reason"),
                    "realized_pnl": _to_float(r.get("realized_pnl")),
                    "executed_at": (
                        r["executed_at"].isoformat()
                        if r.get("executed_at") is not None
                        else None
                    ),
                }
                for r in rows
            ]
            return {"success": True, "account_name": name, "trades": trades}

    @mcp.tool(
        name="compare_paper_accounts",
        description=(
            "Side-by-side performance comparison for multiple paper accounts. "
            "Runs calculate_performance against each name and returns a list. "
            "Missing accounts appear with performance=null and an error message. "
            "period ∈ {1d, 1w, 1m, 3m, all}, defaults to 'all'."
        ),
    )
    async def compare_paper_accounts(
        names: list[str],
        period: str = "all",
    ) -> dict[str, Any]:
        if not names:
            return {
                "success": False,
                "error": "Provide at least one account name",
            }

        try:
            today = now_kst().date()
            start = _parse_period(period, today)
            end = None if period == "all" else today
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        comparison: list[dict[str, Any]] = []
        async with _session_factory()() as db:
            service = PaperTradingService(db)
            for account_name in names:
                account = await service.get_account_by_name(account_name)
                if account is None:
                    comparison.append({
                        "account_name": account_name,
                        "performance": None,
                        "error": f"Paper account '{account_name}' not found",
                    })
                    continue
                try:
                    perf = await service.calculate_performance(
                        account_id=account.id, start_date=start, end_date=end
                    )
                    comparison.append({
                        "account_name": account_name,
                        "performance": perf,
                        "error": None,
                    })
                except ValueError as exc:
                    comparison.append({
                        "account_name": account_name,
                        "performance": None,
                        "error": str(exc),
                    })

        return {"success": True, "period": period, "comparison": comparison}


__all__ = [
    "PAPER_ANALYTICS_TOOL_NAMES",
    "register_paper_analytics_tools",
    "_parse_period",
    "_session_factory",
    "_to_float",
]
