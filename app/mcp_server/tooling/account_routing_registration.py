from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.account_routing_tools import suggest_order_account_impl

if TYPE_CHECKING:
    from fastmcp import FastMCP


ACCOUNT_ROUTING_TOOL_NAMES: set[str] = {"suggest_order_account"}


def register_account_routing_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="suggest_order_account",
        description=(
            "Read-only advisory: compare KIS/Toss buy-account costs for KR/US "
            "stocks using commission, FX spread, orderable cash, Toss notional "
            "limits, and existing-position consolidation. Never submits or "
            "routes an order automatically; operator final decision required."
        ),
    )
    async def suggest_order_account(
        symbol: str,
        market: str | None = None,
        side: str = "buy",
        quantity: float = 0,
        price: float | None = None,
        usd_krw: float | None = None,
    ) -> dict[str, Any]:
        return await suggest_order_account_impl(
            symbol=symbol,
            market=market,
            side=side,
            quantity=quantity,
            price=price,
            usd_krw=usd_krw,
        )


__all__ = ["ACCOUNT_ROUTING_TOOL_NAMES", "register_account_routing_tools"]
