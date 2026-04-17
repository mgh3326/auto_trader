"""Orders MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.mcp_server.tooling import order_execution, orders_history
from app.mcp_server.tooling.orders_modify_cancel import (
    cancel_order_impl,
    modify_order_impl,
)
from app.mcp_server.tooling.paper_order_handler import (
    _get_paper_order_history,
    _place_paper_order,
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
            "(KR/US equities). Pending orders can be queried without a symbol, "
            "but filled/cancelled/all queries require symbol. "
            "Set account_type='paper' to query the virtual paper-trading "
            "account's trade history instead; pass paper_account to target a "
            "named paper account (defaults to 'default')."
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
        account_type: Literal["real", "paper"] = "real",
        paper_account: str | None = None,
    ):
        if account_type == "paper":
            return await _get_paper_order_history(
                symbol=symbol,
                status=status,
                order_id=order_id,
                market=market,
                side=side,
                days=days,
                limit=limit,
                paper_account_name=paper_account,
            )
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
            "Place buy/sell LIMIT orders for stocks or crypto. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "Only limit orders are supported via MCP — market orders are not allowed. "
            "`order_type` must be 'limit' and `price` is required. "
            "Always returns dry_run preview unless explicitly set to False. "
            "For buy orders (dry_run=False), thesis and strategy are required "
            "so a trade journal can be created automatically. "
            "For sell orders, active trade journals are auto-closed in FIFO order. "
            "Use exit_reason to record the sell thesis in the journal. "
            "Safety limit: max 20 orders/day. "
            "dry_run=True by default for safety. "
            "Set account_type='paper' to route to the virtual paper-trading engine "
            "(no real broker calls, uses PaperTradingService). In paper mode, the "
            "default account is auto-created with 100,000,000 KRW on first use; "
            "pass paper_account to target a named paper account. "
            "Journal features (thesis/strategy/FIFO close) ARE supported in paper mode. "
            "defensive_trim=True enables a sell/limit-only floor bypass path. "
            "ROB-164/ROB-166 defensive_trim requires ALL of: (a) side='sell', "
            "(b) order_type='limit', (c) valid approval_issue_id with approval issue "
            "status=done in Paperclip, and (d) requester_agent_id matching Trader. "
            "requester_agent_id is caller-asserted; ST-3 tracks true caller "
            "attestation."
        ),
    )
    async def place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        order_type: Literal["limit"] = "limit",
        quantity: float | None = None,
        price: float | None = None,
        amount: float | None = None,
        dry_run: bool = True,
        reason: str = "",
        exit_reason: str | None = None,
        thesis: str | None = None,
        strategy: str | None = None,
        target_price: float | None = None,
        stop_loss: float | None = None,
        min_hold_days: int | None = None,
        notes: str | None = None,
        indicators_snapshot: dict[str, Any] | None = None,
        defensive_trim: bool = False,
        approval_issue_id: str | None = None,
        requester_agent_id: str | None = None,
        account_type: Literal["real", "paper"] = "real",
        paper_account: str | None = None,
    ):
        # Defense in depth: reject market orders even if a stale client
        # bypasses the tightened schema and still sends order_type="market".
        if str(order_type).lower().strip() != "limit":
            if defensive_trim:
                return {
                    "success": False,
                    "error": (
                        "defensive_trim requires order_type='limit' "
                        "(market orders are blocked)"
                    ),
                    "source": "mcp",
                    "symbol": symbol,
                    "order_type": order_type,
                }
            return {
                "success": False,
                "error": (
                    "MCP place_order only supports limit orders; "
                    "market orders are not allowed."
                ),
                "source": "mcp",
                "symbol": symbol,
                "order_type": order_type,
            }
        if account_type == "paper":
            return await _place_paper_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                amount=amount,
                dry_run=dry_run,
                reason=reason,
                exit_reason=exit_reason,
                thesis=thesis,
                strategy=strategy,
                target_price=target_price,
                stop_loss=stop_loss,
                min_hold_days=min_hold_days,
                notes=notes,
                indicators_snapshot=indicators_snapshot,
                paper_account_name=paper_account,
            )
        return await order_execution._place_order_impl(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            amount=amount,
            dry_run=dry_run,
            reason=reason,
            exit_reason=exit_reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            indicators_snapshot=indicators_snapshot,
            defensive_trim=defensive_trim,
            approval_issue_id=approval_issue_id,
            requester_agent_id=requester_agent_id,
        )

    @mcp.tool(
        name="cancel_order",
        description=(
            "Cancel a pending order. Supports Upbit (crypto) and KIS (KR/US equities). "
            "For KIS US orders, resolves exchange/order details from symbol lookup and order history when possible."
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
