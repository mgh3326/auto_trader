"""Orders MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.core.config import validate_kis_mock_config
from app.mcp_server.tooling import kis_mock_ledger, order_execution, orders_history
from app.mcp_server.tooling.account_modes import (
    apply_account_routing_metadata,
    normalize_account_mode,
)
from app.mcp_server.tooling.orders_modify_cancel import (
    cancel_order_impl,
    modify_order_impl,
)
from app.mcp_server.tooling.paper_order_handler import (
    _get_paper_order_history,
    _place_paper_order,
)
from app.services.orders.ladder_fill_safety import (
    LadderRung,
    evaluate_ladder_fill_safety,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

ORDER_TOOL_NAMES: set[str] = {
    "place_order",
    "modify_order",
    "cancel_order",
    "get_order_history",
    "kis_mock_reconciliation_run",
    "sell_ladder_fill_preview",
    "buy_ladder_fill_preview",
}


def _ladder_fill_preview_response(
    *,
    side: str,
    symbol: str,
    anchor_price: float,
    rungs: list[dict[str, Any]],
    atr: float | None,
    anchor_source: str | None,
    anchor_as_of: str | None,
) -> dict[str, Any]:
    """Shared body for sell_ladder_fill_preview / buy_ladder_fill_preview."""
    try:
        parsed_rungs = [
            LadderRung(
                limit_price=float(rung["limit_price"]),
                quantity=(
                    float(rung["quantity"])
                    if rung.get("quantity") is not None
                    else None
                ),
            )
            for rung in rungs
        ]
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "success": False,
            "error": f"invalid rungs payload (need 'limit_price'): {exc!r}",
            "expected": "[{'limit_price': float, 'quantity': float|null}, ...]",
        }
    warnings, fill_safety = evaluate_ladder_fill_safety(
        side=side,
        rungs=parsed_rungs,
        anchor_price=anchor_price,
        anchor_source=anchor_source,
        atr=atr,
    )
    if fill_safety is None:
        return {
            "success": False,
            "error": (
                "nothing to analyze: anchor_price must be > 0 and at least "
                "one rung needs limit_price > 0"
            ),
            "symbol": symbol,
        }
    result: dict[str, Any] = {
        "success": True,
        "symbol": symbol,
        "read_only": True,
        "warnings": warnings,
        "fill_safety": fill_safety,
    }
    if anchor_as_of is not None:
        result["anchor_as_of"] = anchor_as_of
    return result


def _kis_mock_config_error() -> dict[str, Any] | None:
    missing = validate_kis_mock_config()
    if not missing:
        return None
    return {
        "success": False,
        "error": (
            "KIS mock account is disabled or missing required configuration: "
            + ", ".join(missing)
        ),
        "source": "kis",
        "account_mode": "kis_mock",
    }


def register_order_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_order_history",
        description=(
            "[DEPRECATED ROB-447] For KIS equities prefer the typed "
            "kis_live_get_order_history / kis_mock_get_order_history; generic remains "
            "for crypto/US. "
            "Get order history for a symbol. Supports Upbit (crypto) and KIS "
            "(KR/US equities). Pending orders can be queried without a symbol, "
            "but filled/cancelled/all queries require symbol. "
            "status='expired' returns dead day orders (KIS: nothing filled, "
            "nothing left to modify/cancel — EOD expiry/reject, distinct from an "
            "operator cancel which is status='cancelled'). Each order carries "
            "is_live (true only for pending/partial). "
            "Set account_type='paper' to query the virtual paper-trading "
            "account's trade history instead; pass paper_account to target a "
            "named paper account (defaults to 'default'). "
            "Use account_mode={'db_simulated','kis_mock','kis_live'} "
            "(preferred); account_type aliases are deprecated and emit warnings."
        ),
    )
    async def get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled", "expired"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
        account_mode: str | None = None,
        account_type: str | None = None,
        paper_account: str | None = None,
    ):
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if routing.is_db_simulated:
            return apply_account_routing_metadata(
                await _get_paper_order_history(
                    symbol=symbol,
                    status=status,
                    order_id=order_id,
                    market=market,
                    side=side,
                    days=days,
                    limit=limit,
                    paper_account_name=paper_account,
                ),
                routing,
            )
        if routing.is_kis_mock:
            config_error = _kis_mock_config_error()
            if config_error:
                return config_error
        return apply_account_routing_metadata(
            await orders_history.get_order_history_impl(
                symbol=symbol,
                status=status,
                order_id=order_id,
                market=market,
                side=side,
                days=days,
                limit=limit,
                is_mock=routing.is_kis_mock,
            ),
            routing,
        )

    @mcp.tool(
        name="place_order",
        description=(
            "[DEPRECATED ROB-447] For KIS equities prefer the typed "
            "kis_live_place_order / kis_mock_place_order (is_mock-hardpinned, "
            "unambiguous routing); this generic router remains the only surface for "
            "crypto/US for now. "
            "Place buy/sell LIMIT orders for stocks or crypto. "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "Only limit orders are supported via MCP — market orders are not allowed. "
            "`order_type` must be 'limit' and `price` is required. "
            "Always returns dry_run preview unless explicitly set to False. "
            "For buy orders (dry_run=False), thesis and strategy are required "
            "so a trade journal can be created automatically. "
            "For sell orders, active trade journals are auto-closed in FIFO order. "
            "Use exit_reason to record the sell thesis in the journal. "
            "If this order originates from an investment_report item, pass that "
            "item's item_uuid (from investment_report_create / investment_report_get) "
            "as report_item_uuid to create the ROB-473 audit link so /invest can show "
            "rationale → order → fill status; omit when there is no originating report item. "
            "dry_run=True by default for safety. "
            "Set account_type='paper' to route to the virtual paper-trading engine "
            "(no real broker calls, uses PaperTradingService). In paper mode, the "
            "default account is auto-created with 100,000,000 KRW on first use; "
            "pass paper_account to target a named paper account. "
            "Use account_mode={'db_simulated','kis_mock','kis_live'} "
            "(preferred); account_type aliases are deprecated and emit warnings. "
            "Journal features (thesis/strategy/FIFO close) ARE supported in paper mode. "
            "defensive_trim=True enables a sell/limit-only floor bypass path. "
            "ROB-164/ROB-166 defensive_trim requires ALL of: (a) side='sell', "
            "(b) order_type='limit', (c) valid approval_issue_id with approval issue "
            "status=done in Paperclip, and (d) middleware-extracted caller identity "
            "matching Trader agent. "
            "Approval-hash binding (ORDER_APPROVAL_HASH_MODE, default optional): the "
            "dry_run=True preview mints approval_hash (self-contained token over the "
            "normalized order, 5-minute TTL), approval_expires_at, and idempotency_key; "
            "pass that approval_hash back (with the same rung ladder level) so live "
            "send re-derives the canonical order and fail-closes on mismatch/expiry. "
            "off=ignored; optional=verified only when supplied; warn=logs a hash-less "
            "live send; required=mandatory for LIVE sends (mock/is_mock paths exempt)."
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
        account_mode: str | None = None,
        account_type: str | None = None,
        paper_account: str | None = None,
        report_item_uuid: str | None = None,
        approval_hash: str | None = None,
        rung: str | int | None = None,
    ):
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
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
        if routing.is_db_simulated:
            return apply_account_routing_metadata(
                await _place_paper_order(
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
                ),
                routing,
            )
        if routing.is_kis_mock:
            config_error = _kis_mock_config_error()
            if config_error:
                return config_error
        return apply_account_routing_metadata(
            await order_execution._place_order_impl(
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
                is_mock=routing.is_kis_mock,
                report_item_uuid=report_item_uuid,
                approval_hash=approval_hash,
                rung=rung,
            ),
            routing,
        )

    @mcp.tool(
        name="sell_ladder_fill_preview",
        description=(
            "[ROB-477] Read-only fill-safety analysis for a multi-rung SELL "
            "limit ladder. No broker calls, no order mutation. Pass "
            "anchor_price (current price or best bid from get_quote) and the "
            "FULL ladder as rungs=[{'limit_price': 64.0, 'quantity': 2.0}, ...]"
            "; atr optional (widens the near-market threshold to "
            "max(0.3% of anchor, 0.3*ATR)). Returns warnings: "
            "ladder_all_above_market (zero-fill tail risk on reversal - "
            "2026-06-09 incident: 8/8 all-above-market sell ladders filled "
            "nothing) and ladder_missing_near_market_anchor (no rung at or "
            "near the anchor), plus per-rung distance pct / ATR multiples and "
            "a suggested anchor rung. anchor_as_of (optional ISO timestamp "
            "of the anchor quote) is echoed back so a later reviewer can "
            "judge anchor staleness. Run this BEFORE submitting multi-rung "
            "sell ladders via place_order / kis_live_place_order."
        ),
    )
    async def sell_ladder_fill_preview(
        symbol: str,
        anchor_price: float,
        rungs: list[dict[str, Any]],
        atr: float | None = None,
        anchor_source: str | None = None,
        anchor_as_of: str | None = None,
    ):
        return _ladder_fill_preview_response(
            side="sell",
            symbol=symbol,
            anchor_price=anchor_price,
            rungs=rungs,
            atr=atr,
            anchor_source=anchor_source,
            anchor_as_of=anchor_as_of,
        )

    @mcp.tool(
        name="buy_ladder_fill_preview",
        description=(
            "[ROB-507] Read-only fill-safety analysis for a multi-rung BUY "
            "limit ladder (mirror of sell_ladder_fill_preview). No broker "
            "calls, no order mutation. Pass anchor_price (current price or "
            "best ask from get_quote) and the FULL ladder as "
            "rungs=[{'limit_price': 165.5, 'quantity': 10.0}, ...]; atr "
            "optional (widens the near-market threshold to max(0.3% of "
            "anchor, 0.3*ATR)). Returns warnings: ladder_all_below_market "
            "(zero-fill tail risk in a rally — the mirror of the 2026-06-09 "
            "all-above-market sell incident) and "
            "ladder_missing_near_market_anchor (no rung at or near the "
            "anchor), plus per-rung distance pct / ATR multiples and a "
            "suggested anchor rung. anchor_as_of (optional ISO timestamp of "
            "the anchor quote) is echoed back so a later reviewer can judge "
            "anchor staleness (2026-06-10: 5+ stale anchors drifted 1-3% "
            "between analysis and submission). Run this BEFORE submitting "
            "multi-rung buy ladders via place_order / kis_live_place_order. "
            "Note: a buy rung ABOVE the anchor is marketable and place_order "
            "rejects it outright (buy limit > current is a hard error), so "
            "the actionable risk here is the all-below zero-fill tail."
        ),
    )
    async def buy_ladder_fill_preview(
        symbol: str,
        anchor_price: float,
        rungs: list[dict[str, Any]],
        atr: float | None = None,
        anchor_source: str | None = None,
        anchor_as_of: str | None = None,
    ):
        return _ladder_fill_preview_response(
            side="buy",
            symbol=symbol,
            anchor_price=anchor_price,
            rungs=rungs,
            atr=atr,
            anchor_source=anchor_source,
            anchor_as_of=anchor_as_of,
        )

    @mcp.tool(
        name="cancel_order",
        description=(
            "[DEPRECATED ROB-447] For KIS equities prefer the typed "
            "kis_live_cancel_order / kis_mock_cancel_order; generic remains for "
            "crypto/US. "
            "Cancel a pending order. Supports Upbit (crypto) and KIS (KR/US equities). "
            "For KIS US orders, resolves exchange/order details from symbol lookup and order history when possible. "
            "Use account_mode={'kis_live','kis_mock'} to choose KIS routing; "
            "account_type aliases are deprecated and emit warnings. "
            "account_mode='kis_mock' fails closed if KIS_MOCK_ENABLED, "
            "KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, or KIS_MOCK_ACCOUNT_NO "
            "are missing."
        ),
    )
    async def cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
        account_mode: str | None = None,
        account_type: str | None = None,
    ):
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if routing.is_db_simulated:
            return apply_account_routing_metadata(
                {
                    "success": False,
                    "error": "cancel_order is not supported for db_simulated",
                    "order_id": order_id,
                },
                routing,
            )
        if routing.is_kis_mock:
            config_error = _kis_mock_config_error()
            if config_error:
                return apply_account_routing_metadata(config_error, routing)
        return apply_account_routing_metadata(
            await cancel_order_impl(
                order_id=order_id,
                symbol=symbol,
                market=market,
                is_mock=routing.is_kis_mock,
            ),
            routing,
        )

    @mcp.tool(
        name="modify_order",
        description=(
            "[DEPRECATED ROB-447] For KIS equities prefer the typed "
            "kis_live_modify_order / kis_mock_modify_order; generic remains for "
            "crypto/US. "
            "Modify a pending order (price/quantity). "
            "Supports Upbit (crypto) and KIS (KR/US equities). "
            "dry_run=True by default for safety. "
            "Upbit: only limit orders in wait state. "
            "KIS: uses API modify endpoint. "
            "Use account_mode={'kis_live','kis_mock'} to choose KIS routing; "
            "account_type aliases are deprecated and emit warnings. "
            "account_mode='kis_mock' fails closed if KIS_MOCK_ENABLED, "
            "KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, or KIS_MOCK_ACCOUNT_NO "
            "are missing."
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
        account_mode: str | None = None,
        account_type: str | None = None,
    ):
        del reason
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if routing.is_db_simulated:
            return apply_account_routing_metadata(
                {
                    "success": False,
                    "error": "modify_order is not supported for db_simulated",
                    "order_id": order_id,
                    "symbol": symbol,
                },
                routing,
            )
        if routing.is_kis_mock:
            config_error = _kis_mock_config_error()
            if config_error:
                return apply_account_routing_metadata(config_error, routing)
        return apply_account_routing_metadata(
            await modify_order_impl(
                order_id=order_id,
                symbol=symbol,
                market=market,
                new_price=new_price,
                new_quantity=new_quantity,
                dry_run=dry_run,
                is_mock=routing.is_kis_mock,
            ),
            routing,
        )

    @mcp.tool(
        name="kis_mock_reconciliation_run",
        description=(
            "Manually trigger reconciliation of open KIS mock orders against "
            "KIS mock holdings (read-only, is_mock=True). Detects fills via "
            "holdings deltas and updates ledger lifecycle states. "
            "dry_run=True by default for safety. Applying transitions requires "
            "BOTH dry_run=False AND confirm=True. "
            "Fails closed if KIS mock config is missing."
        ),
    )
    async def kis_mock_reconciliation_run(
        dry_run: bool = True,
        confirm: bool = False,
        limit: int = 100,
    ):
        config_error = _kis_mock_config_error()
        if config_error:
            return config_error
        if not dry_run and not confirm:
            return {
                "success": False,
                "account_mode": "kis_mock",
                "dry_run": dry_run,
                "error": "confirm=True is required when dry_run=False",
            }
        return await kis_mock_ledger.kis_mock_reconciliation_run_impl(
            dry_run=dry_run, limit=limit
        )


__all__ = ["ORDER_TOOL_NAMES", "register_order_tools"]
