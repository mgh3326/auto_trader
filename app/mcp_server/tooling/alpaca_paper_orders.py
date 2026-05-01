"""Guarded Alpaca paper submit/cancel MCP tools (ROB-73).

Adapter-specific paper-only side-effect tools.  Both default to a
no-broker-call state and require an explicit ``confirm=True`` flag to
invoke ``AlpacaPaperBrokerService.submit_order`` / ``cancel_order``.

These tools are NOT generic.  They never route through ``place_order`` /
``cancel_order`` / ``modify_order``.  There is no parameter that can
switch the underlying service to the live endpoint.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.mcp_server.tooling.alpaca_paper_preview import PreviewOrderInput
from app.services.brokers.alpaca.schemas import OrderRequest
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

if TYPE_CHECKING:
    from fastmcp import FastMCP


ALPACA_PAPER_MUTATING_TOOL_NAMES: set[str] = {
    "alpaca_paper_submit_order",
    "alpaca_paper_cancel_order",
}

SUBMIT_MAX_QTY: Decimal = Decimal("5")
SUBMIT_MAX_NOTIONAL_USD: Decimal = Decimal("1000")
ORDER_ID_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ORDER_ID_RESERVED_VALUES = frozenset({"all", "order", "orders", "bulk", "cancel"})

ServiceFactory = Callable[[], AlpacaPaperBrokerService]


def _default_service_factory() -> AlpacaPaperBrokerService:
    return AlpacaPaperBrokerService()


_service_factory: ServiceFactory = _default_service_factory


def set_alpaca_paper_orders_service_factory(factory: ServiceFactory) -> None:
    global _service_factory
    _service_factory = factory


def reset_alpaca_paper_orders_service_factory() -> None:
    global _service_factory
    _service_factory = _default_service_factory


def _model_to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list | tuple):
        return [_model_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {k: _model_to_jsonable(v) for k, v in value.items()}
    return value


def _canonical_payload(validated: PreviewOrderInput) -> dict[str, Any]:
    return {
        "symbol": validated.symbol,
        "side": validated.side,
        "type": validated.type,
        "time_in_force": validated.time_in_force,
        "qty": str(validated.qty) if validated.qty is not None else None,
        "notional": str(validated.notional) if validated.notional is not None else None,
        "limit_price": str(validated.limit_price)
        if validated.limit_price is not None
        else None,
        "asset_class": validated.asset_class,
    }


def _derive_client_order_id(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()[:16]
    return f"rob73-{digest}"


def _validate_exact_order_id(order_id: str) -> str:
    """Return a safe single-order id path segment or fail closed.

    Alpaca cancel uses ``DELETE /v2/orders/{order_id}``. Keep the id as a
    single opaque path segment so values cannot normalize into bulk endpoints
    such as ``/v2/orders`` or add query/fragment/filter semantics.
    """
    stripped = (order_id or "").strip()
    if not stripped:
        raise ValueError("order_id is required")
    if stripped.lower() in ORDER_ID_RESERVED_VALUES:
        raise ValueError("order_id must be an exact Alpaca paper order id")
    if not ORDER_ID_SAFE_SEGMENT_RE.fullmatch(stripped):
        raise ValueError("order_id must be a safe single path segment")
    return stripped


async def alpaca_paper_submit_order(
    symbol: str,
    side: str,
    type: str,  # noqa: A002
    qty: Decimal | None = None,
    notional: Decimal | None = None,
    time_in_force: str = "day",
    limit_price: Decimal | None = None,
    client_order_id: str | None = None,
    asset_class: str = "us_equity",
    confirm: bool = False,
) -> dict[str, Any]:
    """Submit a single Alpaca PAPER order (us_equity only).

    Defaults to ``confirm=False`` which performs no broker call.
    """
    validated = PreviewOrderInput(
        symbol=symbol,
        side=side,
        type=type,
        qty=qty,
        notional=notional,
        time_in_force=time_in_force,
        limit_price=limit_price,
        stop_price=None,
        client_order_id=client_order_id,
        asset_class=asset_class,
    )

    if validated.qty is not None and validated.qty > SUBMIT_MAX_QTY:
        raise ValueError(f"qty {validated.qty} exceeds submit cap ({SUBMIT_MAX_QTY})")
    if validated.notional is not None and validated.notional > SUBMIT_MAX_NOTIONAL_USD:
        raise ValueError(
            f"notional {validated.notional} exceeds submit cap ({SUBMIT_MAX_NOTIONAL_USD})"
        )
    if (
        validated.qty is not None
        and validated.limit_price is not None
        and validated.qty * validated.limit_price > SUBMIT_MAX_NOTIONAL_USD
    ):
        raise ValueError(
            f"estimated_cost {validated.qty * validated.limit_price} "
            f"exceeds submit cap ({SUBMIT_MAX_NOTIONAL_USD})"
        )

    canonical = _canonical_payload(validated)
    coid = validated.client_order_id or _derive_client_order_id(canonical)

    if confirm is not True:
        return {
            "success": True,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper",
            "submitted": False,
            "blocked_reason": "confirmation_required",
            "order_request": canonical,
            "client_order_id": coid,
        }

    request = OrderRequest(
        symbol=validated.symbol,
        side=validated.side,
        type=validated.type,
        qty=validated.qty,
        notional=validated.notional,
        time_in_force=validated.time_in_force,
        limit_price=validated.limit_price,
        stop_price=None,
        client_order_id=coid,
    )
    order = await _service_factory().submit_order(request)
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "submitted": True,
        "order": _model_to_jsonable(order),
        "client_order_id": coid,
    }


async def alpaca_paper_cancel_order(
    order_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Cancel exactly one Alpaca PAPER order by id."""
    stripped = _validate_exact_order_id(order_id)

    if confirm is not True:
        return {
            "success": True,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper",
            "cancelled": False,
            "blocked_reason": "confirmation_required",
            "target_order_id": stripped,
        }

    service = _service_factory()
    await service.cancel_order(stripped)

    order_payload: Any = None
    read_back_status = "ok"
    try:
        order = await service.get_order(stripped)
        order_payload = _model_to_jsonable(order)
    except Exception:  # noqa: BLE001 — read-back is best-effort
        read_back_status = "unavailable"

    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "cancelled": True,
        "cancelled_order_id": stripped,
        "order": order_payload,
        "read_back_status": read_back_status,
    }


def register_alpaca_paper_orders_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="alpaca_paper_submit_order",
        description=(
            "Submit a single Alpaca PAPER us_equity order. "
            "Defaults to confirm=False which validates and returns the request "
            "WITHOUT calling the broker. Use confirm=True to actually submit. "
            "Paper endpoint only; live endpoint cannot be selected. "
            "Strict caps: qty<=5, notional<=$1000, qty*limit_price<=$1000."
        ),
    )(alpaca_paper_submit_order)
    _ = mcp.tool(
        name="alpaca_paper_cancel_order",
        description=(
            "Cancel exactly ONE Alpaca PAPER order by order_id. "
            "Defaults to confirm=False which returns the target order_id WITHOUT "
            "calling the broker. Use confirm=True to actually cancel. "
            "No bulk/all/by-symbol/by-status options. Paper endpoint only."
        ),
    )(alpaca_paper_cancel_order)


__all__ = [
    "ALPACA_PAPER_MUTATING_TOOL_NAMES",
    "SUBMIT_MAX_NOTIONAL_USD",
    "SUBMIT_MAX_QTY",
    "ORDER_ID_SAFE_SEGMENT_RE",
    "alpaca_paper_cancel_order",
    "alpaca_paper_submit_order",
    "register_alpaca_paper_orders_tools",
    "reset_alpaca_paper_orders_service_factory",
    "set_alpaca_paper_orders_service_factory",
]
