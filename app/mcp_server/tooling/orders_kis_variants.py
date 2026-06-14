"""Typed KIS order tool variants: kis_live_* and kis_mock_*.

Each variant is a thin wrapper that:
- Hard-pins is_mock (live=False, mock=True).
- Validates any supplied account_mode/account_type matches the pinned mode.
- For mock variants: fails closed via _mock_config_error() before delegating.
- Delegates to existing order implementation functions.
- Wraps response in apply_account_routing_metadata for a consistent envelope.

The original ambiguous place_order/cancel_order/modify_order/get_order_history
tools in orders_registration.py are unchanged; these are additive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from app.core.config import validate_kis_mock_config
from app.mcp_server.tooling import order_execution, orders_history
from app.mcp_server.tooling.account_modes import (
    ACCOUNT_MODE_KIS_LIVE,
    ACCOUNT_MODE_KIS_MOCK,
    AccountRouting,
    apply_account_routing_metadata,
)
from app.mcp_server.tooling.orders_modify_cancel import (
    cancel_order_impl,
    modify_order_impl,
)
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.warnings_guard import (
    WarningsGuardResult,
    check_warnings_guard,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

KIS_LIVE_ORDER_TOOL_NAMES: set[str] = {
    "kis_live_place_order",
    "kis_live_cancel_order",
    "kis_live_modify_order",
    "kis_live_get_order_history",
    "kis_live_reconcile_orders",
}

KIS_MOCK_ORDER_TOOL_NAMES: set[str] = {
    "kis_mock_place_order",
    "kis_mock_cancel_order",
    "kis_mock_modify_order",
    "kis_mock_get_order_history",
}

# US/overseas + crypto live reconcile (ROB-407 generic ledger). Registered
# separately from the KIS KR live variants so the crypto profile can expose
# order reconcile without pulling in the KIS KR live order surface.
LIVE_RECONCILE_TOOL_NAMES: set[str] = {
    "live_reconcile_orders",
}


# ---------------------------------------------------------------------------
# Shared guard/delegation helpers
# ---------------------------------------------------------------------------


def _pinned_routing(account_mode: str) -> AccountRouting:
    return AccountRouting(account_mode=account_mode)


def _is_mock_mode(pinned_mode: str) -> bool:
    return pinned_mode == ACCOUNT_MODE_KIS_MOCK


def _mock_config_error() -> dict[str, Any] | None:
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
        "account_mode": ACCOUNT_MODE_KIS_MOCK,
    }


def _check_mode_arg(
    tool_name: str,
    pinned_mode: str,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any] | None:
    """Return a structured rejection if account_mode or account_type mismatches pinned_mode."""
    for param_name, value in (
        ("account_mode", account_mode),
        ("account_type", account_type),
    ):
        if value is None:
            continue
        normalized = str(value).strip().lower()
        if normalized and normalized != pinned_mode:
            return {
                "success": False,
                "error": (
                    f"{tool_name} does not accept {param_name}='{value}'; "
                    f"this tool is pinned to account_mode='{pinned_mode}'"
                ),
                "source": "mcp",
                "account_mode": pinned_mode,
            }
    return None


def _warning_payload(result: WarningsGuardResult) -> list[dict[str, str | None]]:
    return [
        {
            "warning_type": warning.warning_type,
            "exchange": warning.exchange,
            "start_date": warning.start_date,
            "end_date": warning.end_date,
        }
        for warning in result.warnings
    ]


async def _check_toss_warnings_for_kis_buy(symbol: str) -> WarningsGuardResult:
    client = None
    try:
        client = TossReadClient.from_settings()
        # ROB-550: market=None lets the guard auto-detect KR by the 6-digit
        # symbol pattern, so a US KIS buy (e.g. AAPL) skips the Toss warnings
        # fetch instead of issuing a wasted lookup.
        return await check_warnings_guard(client, symbol, market=None)
    except Exception as exc:
        logger.warning(
            "Failed to check Toss warnings for KIS live order symbol=%s; proceeding fail-open: %s",
            symbol,
            exc,
            exc_info=True,
        )
        return WarningsGuardResult(
            ok=True,
            warnings=[],
            error_message=f"Warnings check failed: {exc} (fail-open)",
        )
    finally:
        if client is not None:
            await client.aclose()


def _prepare_variant_call(
    tool_name: str,
    pinned_mode: str,
    account_mode: str | None,
    account_type: str | None,
) -> tuple[AccountRouting, dict[str, Any] | None]:
    routing = _pinned_routing(pinned_mode)
    rejection = _check_mode_arg(tool_name, pinned_mode, account_mode, account_type)
    if rejection:
        return routing, rejection
    if _is_mock_mode(pinned_mode):
        config_error = _mock_config_error()
        if config_error:
            return routing, apply_account_routing_metadata(config_error, routing)
    return routing, None


def _limit_order_error(tool_name: str, symbol: str, order_type: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"{tool_name} only supports limit orders.",
        "source": "mcp",
        "symbol": symbol,
        "order_type": order_type,
    }


# ROB-463: NXT venue, TIF/order-validity, and 예약주문 are requested capabilities
# but require operator confirmation of the exact KIS wire codes
# (EXCG_ID_DVSN_CD='NXT', ORD_COND_DVSN_CD, RSV_ORD_TIME) before any live order
# can carry them. Until then these knobs are surfaced (so callers get an explicit,
# actionable response instead of silent non-support) but fail closed — no live
# order is placed, even in dry_run. Day orders auto-route via SOR (NXT-eligible) /
# KRX exactly as before. See docs/superpowers/specs for the gated questions.
_SUPPORTED_VENUES = {None, "auto"}
_SUPPORTED_ORDER_VALIDITIES = {None, "day"}


def _venue_tif_gate(
    tool_name: str,
    symbol: str,
    *,
    venue: str | None,
    order_validity: str | None,
    reserved_time: str | None,
) -> dict[str, Any] | None:
    """Return a fail-closed error payload for not-yet-enabled venue/TIF knobs.

    None means the request uses only the supported (auto-route, day) behaviour
    and may proceed unchanged.
    """
    norm_venue = (venue or "").strip().lower() or None
    norm_validity = (order_validity or "").strip().lower() or None
    norm_reserved = (reserved_time or "").strip() or None

    blocked: str | None = None
    if norm_venue not in _SUPPORTED_VENUES:
        blocked = f"venue={venue!r} (explicit KRX/NXT/unified routing)"
    elif norm_validity not in _SUPPORTED_ORDER_VALIDITIES:
        blocked = f"order_validity={order_validity!r} (TIF / 예약주문 / gtc)"
    elif norm_reserved is not None:
        blocked = f"reserved_time={reserved_time!r} (예약주문 / scheduled order)"

    if blocked is None:
        return None

    return {
        "success": False,
        "error": "venue_tif_pending_operator_confirmation",
        "source": "mcp",
        "tool": tool_name,
        "symbol": symbol,
        "blocked": blocked,
        "linear": "ROB-463",
        "reason": (
            f"{blocked} is not yet enabled for KIS live orders. NXT venue, "
            "order validity/TIF, and 예약주문 require operator confirmation of "
            "the exact KIS wire codes (EXCG_ID_DVSN_CD='NXT', ORD_COND_DVSN_CD, "
            "RSV_ORD_TIME) before a live order can carry them — see ROB-463. "
            "Until then orders auto-route via SOR (NXT-eligible) / KRX as day "
            "orders; leave venue/order_validity/reserved_time unset (or "
            "venue='auto', order_validity='day')."
        ),
    }


async def _place_order_variant(
    *,
    tool_name: str,
    pinned_mode: str,
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit"],
    quantity: float | None,
    price: float | None,
    amount: float | None,
    dry_run: bool,
    reason: str,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    defensive_trim: bool,
    approval_issue_id: str | None,
    account_mode: str | None,
    account_type: str | None,
    report_item_uuid: str | None = None,
) -> dict[str, Any]:  # NOSONAR - mirrors the public MCP order contract.
    routing, early_response = _prepare_variant_call(
        tool_name, pinned_mode, account_mode, account_type
    )
    if early_response:
        return early_response
    if str(order_type).lower().strip() != "limit":
        return _limit_order_error(tool_name, symbol, order_type)

    warning_result: WarningsGuardResult | None = None
    is_live_buy = pinned_mode == ACCOUNT_MODE_KIS_LIVE and str(side).lower() == "buy"
    if is_live_buy:
        warning_result = await _check_toss_warnings_for_kis_buy(symbol)
        if not dry_run and not warning_result.ok:
            return {
                "success": False,
                "source": "kis",
                "account_mode": ACCOUNT_MODE_KIS_LIVE,
                "dry_run": dry_run,
                "mutation_sent": False,
                "error": warning_result.error_message,
                "warnings": _warning_payload(warning_result),
            }

    result = apply_account_routing_metadata(
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
            is_mock=_is_mock_mode(pinned_mode),
            report_item_uuid=report_item_uuid,
        ),
        routing,
    )
    if warning_result is not None:
        result["warnings"] = _warning_payload(warning_result)
        if warning_result.error_message:
            result["warnings_check_message"] = warning_result.error_message
    return result


async def _cancel_order_variant(
    *,
    tool_name: str,
    pinned_mode: str,
    order_id: str,
    symbol: str | None,
    market: str | None,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    routing, early_response = _prepare_variant_call(
        tool_name, pinned_mode, account_mode, account_type
    )
    if early_response:
        return early_response
    return apply_account_routing_metadata(
        await cancel_order_impl(
            order_id=order_id,
            symbol=symbol,
            market=market,
            is_mock=_is_mock_mode(pinned_mode),
        ),
        routing,
    )


async def _modify_order_variant(
    *,
    tool_name: str,
    pinned_mode: str,
    order_id: str,
    symbol: str,
    market: str | None,
    new_price: float | None,
    new_quantity: float | None,
    dry_run: bool,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    routing, early_response = _prepare_variant_call(
        tool_name, pinned_mode, account_mode, account_type
    )
    if early_response:
        return early_response
    return apply_account_routing_metadata(
        await modify_order_impl(
            order_id=order_id,
            symbol=symbol,
            market=market,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
            is_mock=_is_mock_mode(pinned_mode),
        ),
        routing,
    )


async def _get_order_history_variant(
    *,
    tool_name: str,
    pinned_mode: str,
    symbol: str | None,
    status: Literal["all", "pending", "filled", "cancelled"],
    order_id: str | None,
    market: str | None,
    side: str | None,
    days: int | None,
    limit: int | None,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    routing, early_response = _prepare_variant_call(
        tool_name, pinned_mode, account_mode, account_type
    )
    if early_response:
        return early_response
    return apply_account_routing_metadata(
        await orders_history.get_order_history_impl(
            symbol=symbol,
            status=status,
            order_id=order_id,
            market=market,
            side=side,
            days=days,
            limit=limit,
            is_mock=_is_mock_mode(pinned_mode),
        ),
        routing,
    )


async def _reconcile_orders_variant(
    *,
    symbol: str | None,
    order_id: str | None,
    dry_run: bool,
    limit: int,
    account_mode: str | None,
    account_type: str | None,
) -> dict[str, Any]:
    routing, early_response = _prepare_variant_call(
        "kis_live_reconcile_orders", ACCOUNT_MODE_KIS_LIVE, account_mode, account_type
    )
    if early_response:
        return early_response
    from app.mcp_server.tooling.kis_live_ledger import (
        kis_live_reconcile_orders_impl,
    )

    return apply_account_routing_metadata(
        await kis_live_reconcile_orders_impl(
            symbol=symbol, order_id=order_id, dry_run=dry_run, limit=limit
        ),
        routing,
    )


# ---------------------------------------------------------------------------
# Live variants (is_mock=False hard-pinned)
# ---------------------------------------------------------------------------


def register_kis_live_order_tools(mcp: FastMCP) -> None:
    """Register kis_live_* typed order tools (is_mock=False hard-pinned)."""
    _PINNED = ACCOUNT_MODE_KIS_LIVE

    @mcp.tool(
        name="kis_live_place_order",
        description=(
            "Place a LIMIT buy/sell order on KIS live (real-money) account. "
            "is_mock is hard-pinned to False. "
            "dry_run=True by default for safety. "
            "For buy orders (dry_run=False), thesis and strategy are required. "
            "Normal weight-management trims do NOT need defensive_trim — leave "
            "it False. defensive_trim=True only bypasses the sell-side price "
            "floor and requires side='sell', order_type='limit', and an "
            "approval_issue_id (e.g. 'ROB-164'); approval_issue_id is mandatory "
            "whenever defensive_trim=True, including dry_run (ROB-164 audit gate). "
            "Orders auto-route via SOR (NXT-eligible) / KRX as day orders. "
            "venue (krx|nxt|unified), order_validity (day|예약|gtc), and "
            "reserved_time are accepted but NOT yet enabled — NXT/TIF/예약주문 "
            "require operator confirmation of the exact KIS wire codes "
            "(ROB-463) and currently fail closed with an explicit error (no live "
            "order, even in dry_run); leave them unset for normal day orders. "
            "report item에서 비롯된 주문이면 investment_report_get의 item_uuid를 report_item_uuid로 넘겨 감사 링크(ROB-473). "
            "Fills are NOT recorded at send time; run "
            "kis_live_reconcile_orders (or enable the operator-gated "
            "kis_live.reconcile_periodic task, ROB-475) to book "
            "fill/journal/realized_pnl. reconcile is the LOCAL bookkeeping "
            "layer; the live-account truth is get_holdings / "
            "get_available_capital. "
            "For multi-rung limit ladders, run sell_ladder_fill_preview "
            "(sells, ROB-477) or buy_ladder_fill_preview (buys, ROB-507) "
            "first to check zero-fill risk. "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_live_place_order(  # NOSONAR - public MCP order schema mirrors legacy tool.
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
        venue: str | None = None,
        order_validity: str | None = None,
        reserved_time: str | None = None,
        account_mode: str | None = None,
        account_type: str | None = None,
        report_item_uuid: str | None = None,
    ) -> dict[str, Any]:
        gate = _venue_tif_gate(
            "kis_live_place_order",
            symbol,
            venue=venue,
            order_validity=order_validity,
            reserved_time=reserved_time,
        )
        if gate is not None:
            return gate
        return await _place_order_variant(
            tool_name="kis_live_place_order",
            pinned_mode=_PINNED,
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
            account_mode=account_mode,
            account_type=account_type,
            report_item_uuid=report_item_uuid,
        )

    @mcp.tool(
        name="kis_live_cancel_order",
        description=(
            "Cancel a pending order on KIS live (real-money) account. "
            "is_mock is hard-pinned to False. "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_live_cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _cancel_order_variant(
            tool_name="kis_live_cancel_order",
            pinned_mode=_PINNED,
            order_id=order_id,
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_live_modify_order",
        description=(
            "Modify a pending order (price/quantity) on KIS live (real-money) account. "
            "is_mock is hard-pinned to False. dry_run=True by default for safety. "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_live_modify_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        new_price: float | None = None,
        new_quantity: float | None = None,
        dry_run: bool = True,
        reason: str = "",
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        del reason
        return await _modify_order_variant(
            tool_name="kis_live_modify_order",
            pinned_mode=_PINNED,
            order_id=order_id,
            symbol=symbol,
            market=market,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_live_get_order_history",
        description=(
            "Get order history on KIS live (real-money) account. "
            "is_mock is hard-pinned to False. "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_live_get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _get_order_history_variant(
            tool_name="kis_live_get_order_history",
            pinned_mode=_PINNED,
            symbol=symbol,
            status=status,
            order_id=order_id,
            market=market,
            side=side,
            days=days,
            limit=limit,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_live_reconcile_orders",
        description=(
            "Reconcile accepted/pending KIS live (real-money) KR orders against "
            "order-id-keyed broker fill evidence (inquire_daily_order_domestic). "
            "Books fills/journals/realized_pnl ONLY from confirmed fills "
            "(delta-idempotent). Missing evidence is fail-closed: rows are left "
            "open with requires_manual_review instead of being marked cancelled. "
            "Stale unfilled day orders are resolved to 'expired' only after "
            "NXT close (20:00 KST) AND broker evidence (rjct_qty == ord_qty); "
            "cancel-confirm rows resolve to 'cancelled'. Evidence is queried "
            "from each order's send date through today (90-day cap), so "
            "next-day reconciles still book prior-day fills. "
            "dry_run=True by default for safety. KR domestic only. "
            "realized_pnl_pct (alias journal_pnl_pct, labeled "
            "realized_pnl_basis='journal_entry') is the per-lot / journal-entry "
            "(FIFO oldest-first) basis, NOT the account-average; "
            "place_order preview / get_holdings / get_available_capital remain "
            "the account-average (pchs_avg_pric) truth. "
            "This is the LOCAL bookkeeping layer (trade/journal/"
            "realized_pnl); the live-account truth is get_holdings / "
            "get_available_capital. An operator-gated periodic auto-"
            "reconcile task exists (kis_live.reconcile_periodic, ROB-475)."
        ),
    )
    async def kis_live_reconcile_orders(
        symbol: str | None = None,
        order_id: str | None = None,
        dry_run: bool = True,
        limit: int = 100,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _reconcile_orders_variant(
            symbol=symbol,
            order_id=order_id,
            dry_run=dry_run,
            limit=limit,
            account_mode=account_mode,
            account_type=account_type,
        )


# ---------------------------------------------------------------------------
# US/overseas + crypto live reconcile (broker-generic ledger, ROB-407)
# ---------------------------------------------------------------------------


def register_live_reconcile_tools(mcp: FastMCP) -> None:
    """Register the US/overseas + crypto live reconcile tool."""

    @mcp.tool(
        name="live_reconcile_orders",
        description=(
            "Reconcile accepted/pending US/overseas + crypto live (real-money) orders "
            "against broker fill evidence (overseas daily-order / Upbit order-state). "
            "Books fills/journals/realized_pnl ONLY from confirmed fills (delta-idempotent); "
            "marks unfilled/cancelled without journal side-effects. dry_run=True by default. "
            "realized_pnl_pct (alias journal_pnl_pct, labeled "
            "realized_pnl_basis='journal_entry') is the per-lot / journal-entry "
            "(FIFO oldest-first) basis, NOT the account-average; get_holdings / "
            "get_available_capital remain the account-average truth. "
            "KR domestic uses kis_live_reconcile_orders instead."
        ),
    )
    async def live_reconcile_orders(
        market: str | None = None,
        broker: str | None = None,
        symbol: str | None = None,
        order_id: str | None = None,
        dry_run: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl

        return await live_reconcile_orders_impl(
            market=market,
            broker=broker,
            symbol=symbol,
            order_id=order_id,
            dry_run=dry_run,
            limit=limit,
        )


# ---------------------------------------------------------------------------
# Mock variants (is_mock=True hard-pinned)
# ---------------------------------------------------------------------------


def register_kis_mock_order_tools(mcp: FastMCP) -> None:
    """Register kis_mock_* typed order tools (is_mock=True hard-pinned)."""
    _PINNED = ACCOUNT_MODE_KIS_MOCK

    @mcp.tool(
        name="kis_mock_place_order",
        description=(
            "Place a LIMIT buy/sell order on KIS official mock (paper) account. "
            "is_mock is hard-pinned to True. Fails closed if KIS mock config "
            "(KIS_MOCK_ENABLED, KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, "
            "KIS_MOCK_ACCOUNT_NO) is missing. "
            "dry_run=True by default for safety. "
            "account_mode='kis_mock' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_mock_place_order(  # NOSONAR - public MCP order schema mirrors legacy tool.
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
        report_item_uuid: str | None = None,
    ) -> dict[str, Any]:
        return await _place_order_variant(
            tool_name="kis_mock_place_order",
            pinned_mode=_PINNED,
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
            account_mode=account_mode,
            account_type=account_type,
            report_item_uuid=report_item_uuid,
        )

    @mcp.tool(
        name="kis_mock_cancel_order",
        description=(
            "Cancel a pending order on KIS official mock (paper) account. "
            "is_mock is hard-pinned to True. Fails closed if KIS mock config is missing. "
            "account_mode='kis_mock' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_mock_cancel_order(
        order_id: str,
        symbol: str | None = None,
        market: str | None = None,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _cancel_order_variant(
            tool_name="kis_mock_cancel_order",
            pinned_mode=_PINNED,
            order_id=order_id,
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_mock_modify_order",
        description=(
            "Modify a pending order (price/quantity) on KIS official mock (paper) account. "
            "is_mock is hard-pinned to True. Fails closed if KIS mock config is missing. "
            "dry_run=True by default for safety. "
            "account_mode='kis_mock' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_mock_modify_order(
        order_id: str,
        symbol: str,
        market: str | None = None,
        new_price: float | None = None,
        new_quantity: float | None = None,
        dry_run: bool = True,
        reason: str = "",
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        del reason
        return await _modify_order_variant(
            tool_name="kis_mock_modify_order",
            pinned_mode=_PINNED,
            order_id=order_id,
            symbol=symbol,
            market=market,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
            account_mode=account_mode,
            account_type=account_type,
        )

    @mcp.tool(
        name="kis_mock_get_order_history",
        description=(
            "Get order history on KIS official mock (paper) account. "
            "is_mock is hard-pinned to True. Fails closed if KIS mock config is missing. "
            "Note: some KR order history endpoints (e.g. TTTC8036R) are unsupported "
            "in KIS mock and return mock_unsupported-tagged errors. "
            "account_mode='kis_mock' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_mock_get_order_history(
        symbol: str | None = None,
        status: Literal["all", "pending", "filled", "cancelled"] = "all",
        order_id: str | None = None,
        market: str | None = None,
        side: str | None = None,
        days: int | None = None,
        limit: int | None = 50,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        return await _get_order_history_variant(
            tool_name="kis_mock_get_order_history",
            pinned_mode=_PINNED,
            symbol=symbol,
            status=status,
            order_id=order_id,
            market=market,
            side=side,
            days=days,
            limit=limit,
            account_mode=account_mode,
            account_type=account_type,
        )


__all__ = [
    "KIS_LIVE_ORDER_TOOL_NAMES",
    "KIS_MOCK_ORDER_TOOL_NAMES",
    "LIVE_RECONCILE_TOOL_NAMES",
    "register_kis_live_order_tools",
    "register_kis_mock_order_tools",
    "register_live_reconcile_tools",
]
