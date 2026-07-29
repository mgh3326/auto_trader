"""ROB-1164 dedicated, least-privilege crypto directional-lab MCP surface."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.analysis_readonly_registration import (
    ANALYSIS_READONLY_TOOL_NAMES,
    _AllowlistedMCP,
    _register_persistence_tools,
)
from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.forecast_registration import register_forecast_tools
from app.mcp_server.tooling.fundamentals_registration import register_fundamentals_tools
from app.mcp_server.tooling.market_data_registration import register_market_data_tools
from app.mcp_server.tooling.operating_briefing_registration import (
    register_operating_briefing_tools,
)
from app.mcp_server.tooling.route_request_registration import (
    register_route_request_tools,
)
from app.mcp_server.tooling.trading_policy_registration import (
    register_trading_policy_tools,
)
from app.services.paper_limit_order_service import PaperLimitOrderService
from app.services.paper_trading_service import PaperTradingService

if TYPE_CHECKING:
    from fastmcp import FastMCP

_F = TypeVar("_F", bound=Callable[..., Any])
DIRECTIONAL_LAB_CRYPTO_STRATEGY = "directional-lab"

# The analysis profile's generic holding/account-routing tools are deliberately
# excluded: they could select an arbitrary paper account. This profile exposes
# only identity-bound paper reads below.
DIRECTIONAL_LAB_CRYPTO_RESEARCH_TOOL_NAMES = ANALYSIS_READONLY_TOOL_NAMES - {
    "get_holdings",
    "toss_get_positions",
    "suggest_order_account",
    "get_intraday_investor_flow",
    "analysis_bundle_get",
}
DIRECTIONAL_LAB_CRYPTO_PAPER_TOOL_NAMES = {
    "list_paper_accounts",
    "directional_lab_crypto_get_holdings",
    "paper_place_limit_order",
    "paper_reconcile_orders",
    "paper_list_pending_orders",
    "paper_cancel_pending_order",
}
DIRECTIONAL_LAB_CRYPTO_TOOL_NAMES = (
    DIRECTIONAL_LAB_CRYPTO_RESEARCH_TOOL_NAMES | DIRECTIONAL_LAB_CRYPTO_PAPER_TOOL_NAMES
)


def _identity_error(error: str) -> dict[str, Any]:
    return {"success": False, "error": error, "fail_closed": True}


def validate_directional_lab_crypto_account_identity(
    account: Any,
    *,
    account_id: int,
    account_name: str,
    strategy_name: str,
) -> str | None:
    """Return a typed fail-closed reason unless all account identity fields match."""
    if strategy_name != DIRECTIONAL_LAB_CRYPTO_STRATEGY:
        return "strategy_name_mismatch"
    if not account_name.strip() or getattr(account, "id", None) != account_id:
        return "account_id_mismatch"
    if getattr(account, "name", None) != account_name:
        return "account_name_mismatch"
    if getattr(account, "strategy_name", None) != strategy_name:
        return "account_strategy_mismatch"
    if not bool(getattr(account, "is_active", False)):
        return "account_inactive"
    return None


async def _resolve_account(
    db: Any, *, account_id: int, account_name: str, strategy_name: str
) -> tuple[Any | None, dict[str, Any] | None]:
    service = PaperTradingService(db)
    account = await service.get_account_by_name(account_name)
    if account is None:
        return None, _identity_error("account_not_found")
    error = validate_directional_lab_crypto_account_identity(
        account,
        account_id=account_id,
        account_name=account_name,
        strategy_name=strategy_name,
    )
    if error:
        return None, _identity_error(error)
    return account, None


def register_directional_lab_crypto_tools(mcp: FastMCP) -> None:
    """Register only the directional-lab crypto contract surface."""
    filtered = cast(
        "FastMCP", _AllowlistedMCP(mcp, DIRECTIONAL_LAB_CRYPTO_RESEARCH_TOOL_NAMES)
    )
    register_operating_briefing_tools(filtered)
    register_trading_policy_tools(filtered)
    register_route_request_tools(filtered)
    register_market_data_tools(filtered)
    register_fundamentals_tools(filtered)
    register_analysis_tools(filtered)
    register_forecast_tools(filtered)
    _register_persistence_tools(mcp)

    @mcp.tool(
        name="list_paper_accounts",
        description="List active directional-lab paper accounts only.",
    )
    async def list_paper_accounts(strategy_name: str) -> dict[str, Any]:
        if strategy_name != DIRECTIONAL_LAB_CRYPTO_STRATEGY:
            return _identity_error("strategy_name_mismatch")
        async with AsyncSessionLocal() as db:
            accounts = await PaperTradingService(db).list_accounts(
                is_active=True, strategy_name=DIRECTIONAL_LAB_CRYPTO_STRATEGY
            )
        return {
            "success": True,
            "accounts": [
                {
                    "id": account.id,
                    "name": account.name,
                    "strategy_name": account.strategy_name,
                }
                for account in accounts
            ],
        }

    @mcp.tool(
        name="directional_lab_crypto_get_holdings",
        description="Read holdings for one identity-verified directional-lab paper account.",
    )
    async def directional_lab_crypto_get_holdings(
        account_id: int, account_name: str, strategy_name: str
    ) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            account, failure = await _resolve_account(
                db,
                account_id=account_id,
                account_name=account_name,
                strategy_name=strategy_name,
            )
            if failure:
                return failure
            positions = await PaperTradingService(db).get_positions(
                account_id=account.id, market="crypto"
            )
        return {"success": True, "account_id": account_id, "positions": positions}

    @mcp.tool(
        name="paper_place_limit_order",
        description="Identity-verified paper limit order; dry_run defaults true and commit requires confirm=true.",
    )
    async def paper_place_limit_order(
        account_id: int,
        account_name: str,
        strategy_name: str,
        symbol: str,
        side: str,
        limit_price: float,
        quantity: float | None = None,
        amount_krw: float | None = None,
        thesis: str | None = None,
        target_price: float | None = None,
        stop_loss: float | None = None,
        probability: float | None = None,
        review_date: str | None = None,
        artifact_uuid: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not dry_run and not confirm:
            return _identity_error("confirm_required")
        async with AsyncSessionLocal() as db:
            _, failure = await _resolve_account(
                db,
                account_id=account_id,
                account_name=account_name,
                strategy_name=strategy_name,
            )
            if failure:
                return failure
            if dry_run:
                return {"success": True, "dry_run": True, "account_id": account_id}
            return await PaperLimitOrderService(db).place_limit_order(
                account_id=account_id,
                symbol=symbol,
                side=side,
                limit_price=Decimal(str(limit_price)),
                quantity=Decimal(str(quantity)) if quantity is not None else None,
                amount=Decimal(str(amount_krw)) if amount_krw is not None else None,
                thesis=thesis,
                strategy=DIRECTIONAL_LAB_CRYPTO_STRATEGY,
                target_price=Decimal(str(target_price))
                if target_price is not None
                else None,
                stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
                probability=probability,
                review_date=review_date,
                artifact_uuid=artifact_uuid,
            )

    async def _with_limit_service(
        account_id: int,
        account_name: str,
        strategy_name: str,
        action: str,
        order_id: int | None = None,
        status: str | None = "pending",
    ) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            _, failure = await _resolve_account(
                db,
                account_id=account_id,
                account_name=account_name,
                strategy_name=strategy_name,
            )
            if failure:
                return failure
            service = PaperLimitOrderService(db)
            if action == "reconcile":
                return await service.reconcile_pending_orders(account_id=account_id)
            if action == "cancel":
                return await service.cancel_pending_order(
                    account_id=account_id, order_id=cast(int, order_id)
                )
            pending = await service.list_pending_orders(
                account_id=account_id, status=status
            )
            return {
                "success": True,
                "account_id": account_id,
                "status": status,
                "pending": pending,
                "count": len(pending),
            }

    @mcp.tool(
        name="paper_reconcile_orders",
        description="Reconcile pending orders for one identity-verified lab account.",
    )
    async def paper_reconcile_orders(
        account_id: int, account_name: str, strategy_name: str
    ) -> dict[str, Any]:
        return await _with_limit_service(
            account_id, account_name, strategy_name, "reconcile"
        )

    @mcp.tool(
        name="paper_list_pending_orders",
        description="List pending orders for one identity-verified lab account.",
    )
    async def paper_list_pending_orders(
        account_id: int,
        account_name: str,
        strategy_name: str,
        status: str | None = "pending",
    ) -> dict[str, Any]:
        return await _with_limit_service(
            account_id, account_name, strategy_name, "list", status=status
        )

    @mcp.tool(
        name="paper_cancel_pending_order",
        description="Cancel one pending order for one identity-verified lab account.",
    )
    async def paper_cancel_pending_order(
        account_id: int, account_name: str, strategy_name: str, order_id: int
    ) -> dict[str, Any]:
        return await _with_limit_service(
            account_id, account_name, strategy_name, "cancel", order_id=order_id
        )


__all__ = [
    "DIRECTIONAL_LAB_CRYPTO_PAPER_TOOL_NAMES",
    "DIRECTIONAL_LAB_CRYPTO_RESEARCH_TOOL_NAMES",
    "DIRECTIONAL_LAB_CRYPTO_TOOL_NAMES",
    "DIRECTIONAL_LAB_CRYPTO_STRATEGY",
    "register_directional_lab_crypto_tools",
    "validate_directional_lab_crypto_account_identity",
]
