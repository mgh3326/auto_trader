"""ROB-703 — MCP tools for paper resting-limit orders (Upbit shadow sim).

Adds four operator-facing tools to the DEFAULT MCP profile:
  * ``paper_place_limit_order`` — rest a buy/sell limit on a paper account
  * ``paper_reconcile_orders``  — walk pending orders, fill any that crossed
  * ``paper_cancel_pending_order`` — cancel a pending order + release cash
  * ``paper_list_pending_orders``   — list pending orders with distance-to-fill

Pure simulation: no real broker/Upbit mutation. Reads live OHLCV + writes
only the ``paper.paper_pending_orders`` / ``paper.paper_accounts`` /
``paper.paper_trades`` / ``paper.paper_positions`` tables.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.paper_limit_order_service import PaperLimitOrderService

if TYPE_CHECKING:
    from fastmcp import FastMCP

PAPER_LIMIT_ORDER_TOOL_NAMES: set[str] = {
    "paper_place_limit_order",
    "paper_reconcile_orders",
    "paper_cancel_pending_order",
    "paper_list_pending_orders",
}


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _serialize_order(order: dict[str, Any]) -> dict[str, Any]:
    """Coerce Decimal fields to float for the JSON MCP transport."""
    return {
        **order,
        "limit_price": _to_float(
            order.get("limit_price")
            if isinstance(order.get("limit_price"), Decimal)
            else None
        )
        if isinstance(order.get("limit_price"), Decimal)
        else order.get("limit_price"),
        "quantity": _to_float(
            order.get("quantity")
            if isinstance(order.get("quantity"), Decimal)
            else None
        )
        if isinstance(order.get("quantity"), Decimal)
        else order.get("quantity"),
        "reserved_krw": _to_float(
            order.get("reserved_krw")
            if isinstance(order.get("reserved_krw"), Decimal)
            else None
        )
        if isinstance(order.get("reserved_krw"), Decimal)
        else order.get("reserved_krw"),
        "fill_price": _to_float(
            order.get("fill_price")
            if isinstance(order.get("fill_price"), Decimal)
            else None
        )
        if isinstance(order.get("fill_price"), Decimal)
        else order.get("fill_price"),
    }


async def _preview_place(
    *,
    db: AsyncSession,
    account_id: int,
    symbol: str,
    side: str,
    limit_price: float,
    quantity: float | None,
    amount_krw: float | None,
    thesis: str | None,
) -> dict[str, Any]:
    """dry-run validation only: no DB write."""
    from app.core.money import quantize_crypto_qty, quantize_money
    from app.mcp_server.tooling.shared import resolve_market_type
    from app.services.paper_fills import snap_limit_down

    pts_factory = PaperLimitOrderService
    svc = pts_factory(db)
    account = await svc.pts.get_account(account_id)
    if account is None:
        return {"success": False, "error": f"Account {account_id} not found"}
    if not account.is_active:
        return {"success": False, "error": f"Account {account_id} is inactive"}

    try:
        market_type, resolved_symbol = resolve_market_type(symbol, None)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if market_type != "crypto":
        return {
            "success": False,
            "error": f"Resting-limit sim only supports crypto markets; got {market_type!r}",
        }

    snapped = quantize_money(snap_limit_down(Decimal(str(limit_price))))
    if quantity is not None:
        qty = quantize_crypto_qty(Decimal(str(quantity)))
    elif amount_krw is not None:
        qty = quantize_crypto_qty(Decimal(str(amount_krw)) / snapped)
    else:
        return {
            "success": False,
            "error": "Either quantity or amount_krw must be provided",
        }

    return {
        "success": True,
        "dry_run": True,
        "preview": {
            "account_id": account_id,
            "symbol": resolved_symbol,
            "side": side,
            "order_type": "limit",
            "limit_price": snapped,
            "quantity": qty,
            "thesis": thesis,
            "cash_krw": float(account.cash_krw),
        },
    }


def register_paper_limit_order_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="paper_place_limit_order",
        description=(
            "Place a RESTING limit order on a paper trading account. "
            "Fills ONLY when live Upbit OHLCV crosses the limit — book via "
            "paper_reconcile_orders. side=buy reserves gross+fee KRW from "
            "cash_krw at place; side=sell requires an existing position "
            "large enough to cover quantity. minimum 5000 KRW notional "
            "(Upbit). dry_run=True (default) returns a preview without "
            "writing; set confirm=True with dry_run=False to commit."
        ),
    )
    async def paper_place_limit_order(
        account_id: int,
        symbol: str,
        side: Literal["buy", "sell"],
        limit_price: float,
        quantity: float | None = None,
        amount_krw: float | None = None,
        thesis: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not dry_run and not confirm:
            return {
                "success": False,
                "error": "paper_place_limit_order requires confirm=True when dry_run=False.",
            }
        try:
            async with _session_factory()() as db:
                if dry_run:
                    return await _preview_place(
                        db=db,
                        account_id=account_id,
                        symbol=symbol,
                        side=side,
                        limit_price=limit_price,
                        quantity=quantity,
                        amount_krw=amount_krw,
                        thesis=thesis,
                    )
                svc = PaperLimitOrderService(db)
                return await svc.place_limit_order(
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    limit_price=Decimal(str(limit_price)),
                    quantity=Decimal(str(quantity)) if quantity is not None else None,
                    amount=Decimal(str(amount_krw)) if amount_krw is not None else None,
                    thesis=thesis,
                )
        except Exception as exc:  # noqa: BLE001 — surface unexpected error
            return {"success": False, "error": f"unexpected error: {exc}"}

    @mcp.tool(
        name="paper_reconcile_orders",
        description=(
            "Reconcile all pending paper resting-limit orders for the "
            "given account against the most-recent live Upbit OHLCV. Buys "
            "fill when any bar's low <= limit; sells when any bar's high "
            ">= limit. Fills release the reservation and book the trade "
            "through PaperTradingService at the limit price (no live "
            "re-fetch). Returns {success, reconciled, filled}."
        ),
    )
    async def paper_reconcile_orders(account_id: int) -> dict[str, Any]:
        try:
            async with _session_factory()() as db:
                svc = PaperLimitOrderService(db)
                return await svc.reconcile_pending_orders(account_id=account_id)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"unexpected error: {exc}"}

    @mcp.tool(
        name="paper_cancel_pending_order",
        description=(
            "Cancel a pending paper resting-limit order and release its "
            "reserved cash back to cash_krw. No-op if the order is already "
            "filled or cancelled."
        ),
    )
    async def paper_cancel_pending_order(
        account_id: int, order_id: int
    ) -> dict[str, Any]:
        try:
            async with _session_factory()() as db:
                svc = PaperLimitOrderService(db)
                return await svc.cancel_pending_order(
                    account_id=account_id, order_id=order_id
                )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"unexpected error: {exc}"}

    @mcp.tool(
        name="paper_list_pending_orders",
        description=(
            "List pending paper resting-limit orders for the given "
            "account (status='pending' by default). status=None returns "
            "all statuses."
        ),
    )
    async def paper_list_pending_orders(
        account_id: int, status: str | None = "pending"
    ) -> dict[str, Any]:
        try:
            async with _session_factory()() as db:
                svc = PaperLimitOrderService(db)
                pending = await svc.list_pending_orders(
                    account_id=account_id, status=status
                )
                return {
                    "success": True,
                    "account_id": account_id,
                    "status": status,
                    "pending": [_serialize_order(o) for o in pending],
                    "count": len(pending),
                }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"unexpected error: {exc}"}


__all__ = [
    "PAPER_LIMIT_ORDER_TOOL_NAMES",
    "register_paper_limit_order_tools",
]
