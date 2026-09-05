"""Paper Trading account management MCP tool registration."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.models.paper_trading import PaperAccount
from app.services.paper_trading_service import PaperTradingService

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

PAPER_ACCOUNT_TOOL_NAMES: set[str] = {"list_paper_accounts"}


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _serialize_account(
    account: PaperAccount,
    *,
    positions_count: int | None = None,
    total_evaluated: Decimal | None = None,
    total_pnl_pct: Decimal | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": account.id,
        "name": account.name,
        "initial_capital": float(account.initial_capital),
        "cash_krw": float(account.cash_krw),
        "cash_usd": float(account.cash_usd),
        "description": account.description,
        "strategy_name": account.strategy_name,
        "is_active": account.is_active,
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
    }
    if positions_count is not None:
        data["positions_count"] = positions_count
        data["total_evaluated_krw"] = _to_float(total_evaluated)
        data["total_pnl_pct"] = _to_float(total_pnl_pct)
    return data


def register_paper_account_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="list_paper_accounts",
        description=(
            "List paper trading accounts with per-account summary "
            "(positions_count, total_evaluated_krw, total_pnl_pct). "
            "Note: total_evaluated_krw sums KRW and USD position values verbatim "
            "— it does not convert USD to KRW. "
            "is_active=True (default) filters to active accounts only. "
            "strategy_name (optional) filters to accounts with a matching strategy slug."
        ),
    )
    async def list_paper_accounts(
        is_active: bool = True,
        strategy_name: str | None = None,
    ) -> dict[str, Any]:
        async with _session_factory()() as db:
            service = PaperTradingService(db)
            accounts = await service.list_accounts(
                is_active=is_active,
                strategy_name=strategy_name,
            )

            out: list[dict[str, Any]] = []
            for account in accounts:
                try:
                    summary = await service.get_portfolio_summary(account.id)
                    out.append(
                        _serialize_account(
                            account,
                            positions_count=summary["positions_count"],
                            total_evaluated=summary.get("total_evaluated"),
                            total_pnl_pct=summary.get("total_pnl_pct"),
                        )
                    )
                except Exception as exc:  # summary is best-effort
                    logger.warning(
                        "get_portfolio_summary failed for account %s: %s",
                        account.id,
                        exc,
                    )
                    out.append(_serialize_account(account, positions_count=0))
            return {"success": True, "accounts": out}


__all__ = ["PAPER_ACCOUNT_TOOL_NAMES", "register_paper_account_tools"]
