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

if TYPE_CHECKING:
    from fastmcp import FastMCP

KIS_LIVE_ORDER_TOOL_NAMES: set[str] = {
    "kis_live_place_order",
    "kis_live_cancel_order",
    "kis_live_modify_order",
    "kis_live_get_order_history",
}

KIS_MOCK_ORDER_TOOL_NAMES: set[str] = {
    "kis_mock_place_order",
    "kis_mock_cancel_order",
    "kis_mock_modify_order",
    "kis_mock_get_order_history",
}


def _pinned_routing(account_mode: str) -> AccountRouting:
    return AccountRouting(account_mode=account_mode)


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
            "Safety limit: max 20 orders/day. "
            "account_mode='kis_live' is accepted but redundant; "
            "any other account_mode value is rejected."
        ),
    )
    async def kis_live_place_order(
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
    ) -> dict[str, Any]:
        rejection = _check_mode_arg(
            "kis_live_place_order", _PINNED, account_mode, account_type
        )
        if rejection:
            return rejection
        if str(order_type).lower().strip() != "limit":
            return {
                "success": False,
                "error": "kis_live_place_order only supports limit orders.",
                "source": "mcp",
                "symbol": symbol,
                "order_type": order_type,
            }
        routing = _pinned_routing(_PINNED)
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
                is_mock=False,
            ),
            routing,
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
        rejection = _check_mode_arg(
            "kis_live_cancel_order", _PINNED, account_mode, account_type
        )
        if rejection:
            return rejection
        routing = _pinned_routing(_PINNED)
        return apply_account_routing_metadata(
            await cancel_order_impl(
                order_id=order_id,
                symbol=symbol,
                market=market,
                is_mock=False,
            ),
            routing,
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
        rejection = _check_mode_arg(
            "kis_live_modify_order", _PINNED, account_mode, account_type
        )
        if rejection:
            return rejection
        routing = _pinned_routing(_PINNED)
        return apply_account_routing_metadata(
            await modify_order_impl(
                order_id=order_id,
                symbol=symbol,
                market=market,
                new_price=new_price,
                new_quantity=new_quantity,
                dry_run=dry_run,
                is_mock=False,
            ),
            routing,
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
        rejection = _check_mode_arg(
            "kis_live_get_order_history", _PINNED, account_mode, account_type
        )
        if rejection:
            return rejection
        routing = _pinned_routing(_PINNED)
        return apply_account_routing_metadata(
            await orders_history.get_order_history_impl(
                symbol=symbol,
                status=status,
                order_id=order_id,
                market=market,
                side=side,
                days=days,
                limit=limit,
                is_mock=False,
            ),
            routing,
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
    async def kis_mock_place_order(
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
    ) -> dict[str, Any]:
        rejection = _check_mode_arg(
            "kis_mock_place_order", _PINNED, account_mode, account_type
        )
        if rejection:
            return rejection
        config_error = _mock_config_error()
        if config_error:
            return apply_account_routing_metadata(
                config_error, _pinned_routing(_PINNED)
            )
        if str(order_type).lower().strip() != "limit":
            return {
                "success": False,
                "error": "kis_mock_place_order only supports limit orders.",
                "source": "mcp",
                "symbol": symbol,
                "order_type": order_type,
            }
        routing = _pinned_routing(_PINNED)
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
                is_mock=True,
            ),
            routing,
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
        rejection = _check_mode_arg(
            "kis_mock_cancel_order", _PINNED, account_mode, account_type
        )
        if rejection:
            return rejection
        config_error = _mock_config_error()
        if config_error:
            return apply_account_routing_metadata(
                config_error, _pinned_routing(_PINNED)
            )
        routing = _pinned_routing(_PINNED)
        return apply_account_routing_metadata(
            await cancel_order_impl(
                order_id=order_id,
                symbol=symbol,
                market=market,
                is_mock=True,
            ),
            routing,
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
        rejection = _check_mode_arg(
            "kis_mock_modify_order", _PINNED, account_mode, account_type
        )
        if rejection:
            return rejection
        config_error = _mock_config_error()
        if config_error:
            return apply_account_routing_metadata(
                config_error, _pinned_routing(_PINNED)
            )
        routing = _pinned_routing(_PINNED)
        return apply_account_routing_metadata(
            await modify_order_impl(
                order_id=order_id,
                symbol=symbol,
                market=market,
                new_price=new_price,
                new_quantity=new_quantity,
                dry_run=dry_run,
                is_mock=True,
            ),
            routing,
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
        rejection = _check_mode_arg(
            "kis_mock_get_order_history", _PINNED, account_mode, account_type
        )
        if rejection:
            return rejection
        config_error = _mock_config_error()
        if config_error:
            return apply_account_routing_metadata(
                config_error, _pinned_routing(_PINNED)
            )
        routing = _pinned_routing(_PINNED)
        return apply_account_routing_metadata(
            await orders_history.get_order_history_impl(
                symbol=symbol,
                status=status,
                order_id=order_id,
                market=market,
                side=side,
                days=days,
                limit=limit,
                is_mock=True,
            ),
            routing,
        )


__all__ = [
    "KIS_LIVE_ORDER_TOOL_NAMES",
    "KIS_MOCK_ORDER_TOOL_NAMES",
    "register_kis_live_order_tools",
    "register_kis_mock_order_tools",
]
