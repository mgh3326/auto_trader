"""Read-only Alpaca paper MCP tooling.

ROB-69 intentionally exposes only inspection/smoke helpers for the Alpaca paper
broker.  Do not add submit/cancel/replace handlers here; paper order mutation is
out of scope until a later issue defines the account/profile/strategy model.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

if TYPE_CHECKING:
    from fastmcp import FastMCP


ALPACA_PAPER_READONLY_TOOL_NAMES: set[str] = {
    "alpaca_paper_get_account",
    "alpaca_paper_get_cash",
    "alpaca_paper_list_positions",
    "alpaca_paper_list_orders",
    "alpaca_paper_get_order",
    "alpaca_paper_list_assets",
    "alpaca_paper_list_fills",
    # ROB-84/ROB-90/ROB-92/ROB-93 ledger read and anomaly preflight tools
    "alpaca_paper_ledger_list_recent",
    "alpaca_paper_ledger_get",
    "alpaca_paper_ledger_get_by_correlation",
    "alpaca_paper_roundtrip_report",
    "alpaca_paper_execution_preflight_check",
}

ServiceFactory = Callable[[], AlpacaPaperBrokerService]


def _default_service_factory() -> AlpacaPaperBrokerService:
    """Build the guarded Alpaca paper service using app settings."""

    return AlpacaPaperBrokerService()


_service_factory: ServiceFactory = _default_service_factory


def set_alpaca_paper_service_factory(factory: ServiceFactory) -> None:
    """Override the service factory for tests."""

    global _service_factory
    _service_factory = factory


def reset_alpaca_paper_service_factory() -> None:
    """Restore the default service factory after tests."""

    global _service_factory
    _service_factory = _default_service_factory


def _model_to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [_model_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_model_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _model_to_jsonable(item) for key, item in value.items()}
    return value


def _parse_optional_datetime(value: str | None, *, field_name: str) -> datetime | None:
    if value is None or value == "":
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc


def _normalize_optional_limit(limit: int | None, *, max_limit: int = 500) -> int | None:
    if limit is None:
        return None
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, max_limit)


async def alpaca_paper_get_account() -> dict[str, Any]:
    """Return the Alpaca paper account snapshot using the paper-only service."""

    account = await _service_factory().get_account()
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "account": _model_to_jsonable(account),
    }


async def alpaca_paper_get_cash() -> dict[str, Any]:
    """Return paper cash and buying power."""

    cash = await _service_factory().get_cash()
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "cash": _model_to_jsonable(cash),
    }


async def alpaca_paper_list_positions() -> dict[str, Any]:
    """List current Alpaca paper positions."""

    positions = await _service_factory().list_positions()
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "count": len(positions),
        "positions": _model_to_jsonable(positions),
    }


async def alpaca_paper_list_orders(
    status: str | None = "open",
    limit: int | None = 50,
) -> dict[str, Any]:
    """List Alpaca paper orders without mutating broker state."""

    normalized_limit = _normalize_optional_limit(limit)
    orders = await _service_factory().list_orders(status=status, limit=normalized_limit)
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "status": status,
        "limit": normalized_limit,
        "count": len(orders),
        "orders": _model_to_jsonable(orders),
    }


async def alpaca_paper_get_order(order_id: str) -> dict[str, Any]:
    """Fetch one Alpaca paper order by id without modifying it."""

    if not order_id.strip():
        raise ValueError("order_id is required")
    order = await _service_factory().get_order(order_id.strip())
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "order": _model_to_jsonable(order),
    }


async def alpaca_paper_list_assets(
    status: str | None = "active",
    asset_class: str | None = "us_equity",
) -> dict[str, Any]:
    """List Alpaca paper-visible assets with optional status/class filters."""

    assets = await _service_factory().list_assets(
        status=status, asset_class=asset_class
    )
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "status": status,
        "asset_class": asset_class,
        "count": len(assets),
        "assets": _model_to_jsonable(assets),
    }


async def alpaca_paper_list_fills(
    after: str | None = None,
    until: str | None = None,
    limit: int | None = 50,
) -> dict[str, Any]:
    """List Alpaca paper fill activities without placing/cancelling orders."""

    parsed_after = _parse_optional_datetime(after, field_name="after")
    parsed_until = _parse_optional_datetime(until, field_name="until")
    normalized_limit = _normalize_optional_limit(limit)
    fills = await _service_factory().list_fills(
        after=parsed_after,
        until=parsed_until,
        limit=normalized_limit,
    )
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "after": after,
        "until": until,
        "limit": normalized_limit,
        "count": len(fills),
        "fills": _model_to_jsonable(fills),
    }


def register_alpaca_paper_tools(mcp: FastMCP) -> None:
    """Register Alpaca paper read-only MCP tools."""

    _ = mcp.tool(
        name="alpaca_paper_get_account",
        description="Read-only Alpaca paper account snapshot. Uses paper endpoint guard; no live endpoint or order mutation.",
    )(alpaca_paper_get_account)
    _ = mcp.tool(
        name="alpaca_paper_get_cash",
        description="Read-only Alpaca paper cash and buying power. Uses paper endpoint guard.",
    )(alpaca_paper_get_cash)
    _ = mcp.tool(
        name="alpaca_paper_list_positions",
        description="Read-only list of Alpaca paper positions.",
    )(alpaca_paper_list_positions)
    _ = mcp.tool(
        name="alpaca_paper_list_orders",
        description="Read-only list of Alpaca paper orders by status and limit; does not submit/cancel/replace.",
    )(alpaca_paper_list_orders)
    _ = mcp.tool(
        name="alpaca_paper_get_order",
        description="Read-only fetch of one Alpaca paper order by id; does not modify broker state.",
    )(alpaca_paper_get_order)
    _ = mcp.tool(
        name="alpaca_paper_list_assets",
        description="Read-only list of Alpaca assets with status and asset_class filters.",
    )(alpaca_paper_list_assets)
    _ = mcp.tool(
        name="alpaca_paper_list_fills",
        description="Read-only list of Alpaca paper fill activities with optional ISO datetime window and limit.",
    )(alpaca_paper_list_fills)


__all__ = [
    "ALPACA_PAPER_READONLY_TOOL_NAMES",
    "alpaca_paper_get_account",
    "alpaca_paper_get_cash",
    "alpaca_paper_list_assets",
    "alpaca_paper_list_fills",
    "alpaca_paper_get_order",
    "alpaca_paper_list_orders",
    "alpaca_paper_list_positions",
    "register_alpaca_paper_tools",
    "reset_alpaca_paper_service_factory",
    "set_alpaca_paper_service_factory",
]
