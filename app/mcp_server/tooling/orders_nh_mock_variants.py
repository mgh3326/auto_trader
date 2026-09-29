"""NH Namuh (NHPLUG) mock-only MCP tools (#849, Stage 2).

Mirrors the kiwoom_mock KR family, hard-pinned to ``account_mode="nh_mock"``.
Registered only in the DEFAULT profile when ``settings.nh_mock_mcp_enabled``
is true; no live profile or lane allowlist lists these names.

This module only declares tool schemas. Every check and the single send path
live in ``app.services.nhplug_mock.operations``, which ends in the #711
dispatcher (claim, fence, uncertain, reconcile). Schemas are strict so JSON
``"true"``/``1``/``"5"`` cannot become booleans or integers. Order-like tools
default to ``dry_run=True`` and send nothing unless ``dry_run=False`` and
``confirm=True``; the only accepted ``order_type`` is ``"limit"``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field, StrictBool, StrictInt, StrictStr

from app.services.nhplug_mock import operations

if TYPE_CHECKING:
    from fastmcp import FastMCP

NH_MOCK_MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "nh_mock_place_order",
        "nh_mock_modify_order",
        "nh_mock_cancel_order",
    }
)
NH_MOCK_TOOL_NAMES: frozenset[str] = NH_MOCK_MUTATION_TOOL_NAMES | frozenset(
    {
        "nh_mock_preview_order",
        "nh_mock_get_order_detail",
        "nh_mock_get_order_history",
        "nh_mock_get_orderable_cash",
        "nh_mock_get_positions",
        "nh_mock_reconcile_orders",
    }
)

_OrderType = Annotated[
    StrictStr | None,
    Field(
        description=(
            "Only the exact string 'limit' is accepted; market and every other "
            "type is refused."
        )
    ),
]
_IdempotencyKey = Annotated[
    StrictStr | None,
    Field(
        description="Required when dry_run=False: 16-64 of [A-Za-z0-9_-]. Reuse it on retry."
    ),
]


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="nh_mock_preview_order",
        description=(
            "Offline check of an NH mock KRX limit order (no network). "
            "order_type must be 'limit'."
        ),
    )
    async def nh_mock_preview_order(
        symbol: StrictStr,
        side: StrictStr,
        quantity: StrictInt,
        price: StrictInt | None = None,
        order_type: _OrderType = "limit",
    ) -> dict[str, Any]:
        return await operations.preview_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
        )

    @mcp.tool(
        name="nh_mock_place_order",
        description=(
            "Place an NH mock KRX limit order. dry_run defaults to True; a send "
            "needs dry_run=False, confirm=True and an idempotency_key. Every "
            "send ends 'uncertain' until nh_mock_reconcile_orders binds it."
        ),
    )
    async def nh_mock_place_order(
        symbol: StrictStr,
        side: StrictStr,
        quantity: StrictInt,
        price: StrictInt | None = None,
        order_type: _OrderType = "limit",
        idempotency_key: _IdempotencyKey = None,
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> dict[str, Any]:
        return await operations.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            confirm=confirm,
        )

    @mcp.tool(
        name="nh_mock_modify_order",
        description=(
            "Modify an NH mock limit order this ledger dispatched and bound. "
            "Needs new_price and new_quantity (<= broker open quantity). "
            "dry_run defaults to True."
        ),
    )
    async def nh_mock_modify_order(
        order_id: StrictStr,
        new_price: StrictInt | None = None,
        new_quantity: StrictInt | None = None,
        symbol: StrictStr | None = None,
        order_type: _OrderType = "limit",
        idempotency_key: _IdempotencyKey = None,
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> dict[str, Any]:
        return await operations.modify_order(
            order_id=order_id,
            new_price=new_price,
            new_quantity=new_quantity,
            symbol=symbol,
            order_type=order_type,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            confirm=confirm,
        )

    @mcp.tool(
        name="nh_mock_cancel_order",
        description=(
            "Cancel an NH mock order this ledger dispatched and bound. "
            "cancel_quantity=None cancels the whole open quantity. "
            "dry_run defaults to True."
        ),
    )
    async def nh_mock_cancel_order(
        order_id: StrictStr,
        cancel_quantity: StrictInt | None = None,
        symbol: StrictStr | None = None,
        idempotency_key: _IdempotencyKey = None,
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> dict[str, Any]:
        return await operations.cancel_order(
            order_id=order_id,
            cancel_quantity=cancel_quantity,
            symbol=symbol,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            confirm=confirm,
        )

    @mcp.tool(
        name="nh_mock_get_order_history",
        description=(
            "Read NH mock orders for one trading day (YYYYMMDD, default today, "
            "read-only). An empty or incomplete listing is reported as unknown, "
            "never as 'no open orders'."
        ),
    )
    async def nh_mock_get_order_history(
        order_date: StrictStr | None = None,
    ) -> dict[str, Any]:
        return await operations.get_order_history(order_date=order_date)

    @mcp.tool(
        name="nh_mock_get_order_detail",
        description=(
            "Read one NH mock order number: the broker listing row and every "
            "ledger row that binds or targets it (read-only)."
        ),
    )
    async def nh_mock_get_order_detail(
        order_id: StrictStr,
        order_date: StrictStr | None = None,
    ) -> dict[str, Any]:
        return await operations.get_order_detail(
            order_id=order_id, order_date=order_date
        )

    @mcp.tool(
        name="nh_mock_get_positions",
        description="Read NH mock account holdings (read-only).",
    )
    async def nh_mock_get_positions() -> dict[str, Any]:
        return await operations.get_positions()

    @mcp.tool(
        name="nh_mock_get_orderable_cash",
        description="Read NH mock orderable cash in KRW (read-only).",
    )
    async def nh_mock_get_orderable_cash() -> dict[str, Any]:
        return await operations.get_orderable_cash()

    @mcp.tool(
        name="nh_mock_reconcile_orders",
        description=(
            "Manually reconcile NH mock ledger rows for one trading day against "
            "complete broker listings. dry_run defaults to True (reads only); "
            "writes need dry_run=False and confirm=True. Never sends an order."
        ),
    )
    async def nh_mock_reconcile_orders(
        order_date: StrictStr | None = None,
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> dict[str, Any]:
        return await operations.reconcile_orders(
            order_date=order_date, dry_run=dry_run, confirm=confirm
        )
