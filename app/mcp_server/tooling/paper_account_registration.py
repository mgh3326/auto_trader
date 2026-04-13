"""Paper Trading account management MCP tool registration."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.models.paper_trading import PaperAccount

if TYPE_CHECKING:
    from fastmcp import FastMCP

PAPER_ACCOUNT_TOOL_NAMES: set[str] = {
    "create_paper_account",
    "list_paper_accounts",
    "reset_paper_account",
    "delete_paper_account",
}


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
        name="create_paper_account",
        description="Create a new paper trading account (stub).",
    )
    async def create_paper_account(
        name: str,
        initial_capital: float = 100_000_000.0,
        initial_capital_usd: float = 0.0,
        description: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @mcp.tool(
        name="list_paper_accounts",
        description="List paper trading accounts (stub).",
    )
    async def list_paper_accounts(is_active: bool = True) -> dict[str, Any]:
        raise NotImplementedError

    @mcp.tool(
        name="reset_paper_account",
        description="Reset a paper trading account (stub).",
    )
    async def reset_paper_account(name: str) -> dict[str, Any]:
        raise NotImplementedError

    @mcp.tool(
        name="delete_paper_account",
        description="Delete a paper trading account (stub).",
    )
    async def delete_paper_account(name: str) -> dict[str, Any]:
        raise NotImplementedError


__all__ = ["PAPER_ACCOUNT_TOOL_NAMES", "register_paper_account_tools"]
