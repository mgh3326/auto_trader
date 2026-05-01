"""Side-effect-free Alpaca paper MCP order preview/validation tool (ROB-70).

alpaca_paper_preview_order is a pure validator + echo. It does NOT call
POST /v2/orders. There is no alpaca_paper_submit_order / place_order /
cancel_order / modify_order / replace_order tool.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, field_validator, model_validator

from app.services.brokers.alpaca.exceptions import (
    AlpacaPaperConfigurationError,
    AlpacaPaperEndpointError,
    AlpacaPaperRequestError,
)
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

if TYPE_CHECKING:
    from fastmcp import FastMCP

ALPACA_PAPER_PREVIEW_TOOL_NAMES: set[str] = {"alpaca_paper_preview_order"}

# Referenced by tests to confirm these methods are never called on the preview path
_FORBIDDEN_SERVICE_METHODS = ("submit_order", "cancel_order")

ServiceFactory = Callable[[], AlpacaPaperBrokerService]


def _default_preview_service_factory() -> AlpacaPaperBrokerService:
    """Build the guarded Alpaca paper service using app settings."""
    return AlpacaPaperBrokerService()


_preview_service_factory: ServiceFactory = _default_preview_service_factory


def set_alpaca_paper_preview_service_factory(factory: ServiceFactory) -> None:
    """Override the preview service factory for tests."""
    global _preview_service_factory
    _preview_service_factory = factory


def reset_alpaca_paper_preview_service_factory() -> None:
    """Restore the default preview service factory after tests."""
    global _preview_service_factory
    _preview_service_factory = _default_preview_service_factory


class PreviewOrderInput(BaseModel):
    """Strict input validation model for alpaca_paper_preview_order."""

    symbol: str
    side: str
    type: str  # noqa: A003
    qty: Decimal | None = None
    notional: Decimal | None = None
    time_in_force: str = "day"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str | None = None
    asset_class: str = "us_equity"

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("symbol must not be blank")
        if len(stripped) > 10:
            raise ValueError("symbol must be 1-10 characters")
        return stripped.upper()

    @field_validator("side")
    @classmethod
    def validate_side(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        return normalized

    @field_validator("type")
    @classmethod
    def validate_order_type(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"market", "limit"}:
            raise ValueError("order type must be 'market' or 'limit'")
        return normalized

    @field_validator("qty")
    @classmethod
    def validate_qty(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        if not v.is_finite():
            raise ValueError("qty must be a finite number")
        if v <= 0:
            raise ValueError("qty must be > 0")
        if v > Decimal("1000000"):
            raise ValueError("qty exceeds maximum allowed value")
        return v

    @field_validator("notional")
    @classmethod
    def validate_notional(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        if not v.is_finite():
            raise ValueError("notional must be a finite number")
        if v <= 0:
            raise ValueError("notional must be > 0")
        if v > Decimal("10000000"):
            raise ValueError("notional exceeds maximum allowed value")
        return v

    @field_validator("stop_price")
    @classmethod
    def validate_stop_price(cls, v: Decimal | None) -> Decimal | None:
        if v is not None:
            raise ValueError("stop_price not supported in preview")
        return None

    @field_validator("time_in_force")
    @classmethod
    def validate_tif(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"day", "gtc", "ioc", "fok"}:
            raise ValueError("time_in_force must be one of: day, gtc, ioc, fok")
        return normalized

    @field_validator("client_order_id")
    @classmethod
    def validate_client_order_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("client_order_id must not be blank")
        if len(stripped) > 48:
            raise ValueError("client_order_id must be <= 48 characters")
        return stripped

    @field_validator("asset_class")
    @classmethod
    def validate_asset_class(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized != "us_equity":
            raise ValueError(
                f"asset_class '{v}' not supported in preview (us_equity only)"
            )
        return normalized

    @field_validator("limit_price")
    @classmethod
    def validate_limit_price_positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("limit_price must be > 0")
        return v

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> PreviewOrderInput:
        has_qty = self.qty is not None
        has_notional = self.notional is not None

        if has_qty and has_notional:
            raise ValueError("exactly one of qty or notional is required")
        if not has_qty and not has_notional:
            raise ValueError("exactly one of qty or notional is required")

        # notional + limit type is rejected — Alpaca only supports notional for market orders
        if has_notional and self.type == "limit":
            raise ValueError("notional is not supported for limit orders")

        if self.type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")

        if self.type == "market" and self.limit_price is not None:
            raise ValueError("limit_price is not allowed for market orders")

        return self


async def alpaca_paper_preview_order(
    symbol: str,
    side: str,
    type: str,  # noqa: A002
    qty: Decimal | None = None,
    notional: Decimal | None = None,
    time_in_force: str = "day",
    limit_price: Decimal | None = None,
    stop_price: Decimal | None = None,
    client_order_id: str | None = None,
    asset_class: str = "us_equity",
) -> dict[str, Any]:
    """Preview and validate an Alpaca paper US equity order without submitting it.

    Pure validator + echo — preview only, no side effects, does not submit.
    Does NOT call POST /v2/orders. There is no alpaca_paper_submit_order,
    place_order, cancel_order, modify_order, or replace_order tool.
    """
    validated = PreviewOrderInput(
        symbol=symbol,
        side=side,
        type=type,
        qty=qty,
        notional=notional,
        time_in_force=time_in_force,
        limit_price=limit_price,
        stop_price=stop_price,
        client_order_id=client_order_id,
        asset_class=asset_class,
    )

    order_request: dict[str, Any] = {
        "symbol": validated.symbol,
        "side": validated.side,
        "type": validated.type,
        "time_in_force": validated.time_in_force,
        "qty": str(validated.qty) if validated.qty is not None else None,
        "notional": str(validated.notional) if validated.notional is not None else None,
        "limit_price": str(validated.limit_price)
        if validated.limit_price is not None
        else None,
        "stop_price": None,
        "client_order_id": validated.client_order_id,
        "asset_class": validated.asset_class,
    }

    warnings: list[str] = []
    account_context: dict[str, Any] | None = None
    estimated_cost: str | None = None
    would_exceed_buying_power: bool | None = None

    try:
        service = _preview_service_factory()
        cash = await service.get_cash()
        account_context = {
            "cash": str(cash.cash),
            "buying_power": str(cash.buying_power),
        }
    except AlpacaPaperEndpointError:
        raise  # fail closed — live endpoint is never allowed
    except (AlpacaPaperConfigurationError, AlpacaPaperRequestError):
        warnings.append("context_unavailable")

    if (
        validated.qty is not None
        and validated.limit_price is not None
        and account_context is not None
    ):
        cost = validated.qty * validated.limit_price
        estimated_cost = str(cost)
        would_exceed_buying_power = cost > Decimal(account_context["buying_power"])

    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper",
        "preview": True,
        "submitted": False,
        "order_request": order_request,
        "estimated_cost": estimated_cost,
        "account_context": account_context,
        "would_exceed_buying_power": would_exceed_buying_power,
        "warnings": warnings,
    }


def register_alpaca_paper_preview_tools(mcp: FastMCP) -> None:
    """Register Alpaca paper order preview MCP tool."""
    _ = mcp.tool(
        name="alpaca_paper_preview_order",
        description=(
            "Preview and validate an Alpaca paper US equity order without submitting it. "
            "Pure validator + echo — preview only, no side effects, does not submit. "
            "Does NOT call POST /v2/orders. "
            "There is no alpaca_paper_submit_order / place_order / cancel_order / "
            "modify_order / replace_order tool."
        ),
    )(alpaca_paper_preview_order)


__all__ = [
    "ALPACA_PAPER_PREVIEW_TOOL_NAMES",
    "_FORBIDDEN_SERVICE_METHODS",
    "PreviewOrderInput",
    "alpaca_paper_preview_order",
    "register_alpaca_paper_preview_tools",
    "reset_alpaca_paper_preview_service_factory",
    "set_alpaca_paper_preview_service_factory",
]
