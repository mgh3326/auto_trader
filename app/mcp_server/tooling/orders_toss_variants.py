# app/mcp_server/tooling/orders_toss_variants.py
"""Toss Securities live MCP order tools.

Every tool is hard-pinned to ``account_mode="toss_live"``. They:
- Validate ``validate_toss_api_config`` before any side effect.
- Default mutation tools to ``dry_run=True`` and require ``confirm=True`` before any POST.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.core.config import settings, validate_toss_api_config
from app.mcp_server.tooling.account_modes import (
    ACCOUNT_MODE_TOSS_LIVE,
    normalize_account_mode,
)
from app.services.brokers.toss import (
    TossApiDisabled,
    TossMissingCredentials,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TOSS_LIVE_ORDER_TOOL_NAMES: set[str] = {
    "toss_preview_order",
    "toss_place_order",
    "toss_modify_order",
    "toss_cancel_order",
    "toss_get_order_history",
    "toss_get_positions",
    "toss_get_orderable_cash",
}


def _config_error() -> None:
    if not settings.toss_api_enabled:
        raise TossApiDisabled("Toss API is disabled.")
    missing = validate_toss_api_config()
    if missing:
        raise TossMissingCredentials(
            f"Toss API is missing required configuration: {', '.join(missing)}"
        )


def _check_mode_arg(account_mode: str | None, account_type: str | None) -> None:
    routing = normalize_account_mode(account_mode, account_type)
    if routing.account_mode != ACCOUNT_MODE_TOSS_LIVE:
        raise ValueError(
            f"Invalid account_mode resolving to {routing.account_mode!r}. "
            f"Toss live tools only support account_mode='{ACCOUNT_MODE_TOSS_LIVE}'."
        )


async def toss_preview_order(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_place_order(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_modify_order(
    order_id: str,
    new_price: str | int | None = None,
    new_quantity: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_cancel_order(
    order_id: str,
    dry_run: bool = True,
    confirm: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_get_order_history(
    status: Literal["open", "closed"] = "closed",
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_get_positions(
    symbol: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_get_orderable_cash(
    currency: Literal["KRW", "USD"] = "KRW",
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


def register_toss_live_order_tools(mcp: FastMCP) -> None:
    mcp.tool(name="toss_preview_order", description="Preview a live order on Toss Securities.")(toss_preview_order)
    mcp.tool(name="toss_place_order", description="Place a live order on Toss Securities.")(toss_place_order)
    mcp.tool(name="toss_modify_order", description="Modify a pending live order on Toss Securities.")(toss_modify_order)
    mcp.tool(name="toss_cancel_order", description="Cancel a pending live order on Toss Securities.")(toss_cancel_order)
    mcp.tool(name="toss_get_order_history", description="Retrieve live order history from Toss Securities.")(toss_get_order_history)
    mcp.tool(name="toss_get_positions", description="Retrieve current holding positions from Toss Securities.")(toss_get_positions)
    mcp.tool(name="toss_get_orderable_cash", description="Retrieve available cash/buying power from Toss Securities.")(toss_get_orderable_cash)
