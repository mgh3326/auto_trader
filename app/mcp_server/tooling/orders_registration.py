"""Orders MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from app.mcp_server.tooling import orders_history
from app.mcp_server.tooling.orders_modify_cancel import (
    cancel_order_impl,
    modify_order_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

ORDER_TOOL_NAMES: set[str] = {
    "place_order",
    "modify_order",
    "cancel_order",
    "get_order_history",
}


def register_order_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_order_history",
        description=(
            "Get order history for a symbol. Supports Upbit (crypto) and KIS "
            "(KR/US equities). Returns normalized order information."
        ),
    )
    async def get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
    ):
        return await orders_history.get_order_history_impl(
            symbol=symbol,
            status=status,
            order_id=order_id,
            market=market,
            side=side,
            days=days,
            limit=limit,
        )

    @mcp.tool(
        name="place_order",
        description=(
            "Place buy/sell orders for stocks or crypto. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "Always returns dry_run preview unless explicitly set to False. "
            "Safety limit: max 20 orders/day. "
            "dry_run=True by default for safety."
        ),
    )
    async def place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit", "market"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        dry_run: bool = True,
        reason: str = "",
    ):
        return await orders_history._place_order_impl(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
            dry_run=dry_run,
            reason=reason,
        )

    @mcp.tool(
        name="cancel_order",
        description=(
            "Cancel a pending order. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "For KIS orders, automatically retrieves order details if not provided."
        ),
    )
    async def cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
    ):
        return await cancel_order_impl(order_id=order_id, symbol=symbol, market=market)

    @mcp.tool(
        name="modify_order",
        description=(
            "Modify a pending order (price/quantity). "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "dry_run=True by default for safety. "
            "Upbit: only limit orders in wait state. "
            "KIS: uses API modify endpoint."
        ),
    )
    async def modify_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        new_price: float | None = None,
        new_quantity: float | None = None,
        dry_run: bool = True,
        reason: str = "",
    ):
        del reason
        return await modify_order_impl(
            order_id=order_id,
            symbol=symbol,
            market=market,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
        )


__all__ = ["ORDER_TOOL_NAMES", "register_order_tools"]
