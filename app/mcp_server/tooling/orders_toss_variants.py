# app/mcp_server/tooling/orders_toss_variants.py
"""Toss Securities live MCP order tools.

Every tool is hard-pinned to ``account_mode="toss_live"``. They:
- Validate ``validate_toss_api_config`` before any side effect.
- Default mutation tools to ``dry_run=True`` and require ``confirm=True`` before any POST.
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from app.core.config import settings, validate_toss_api_config
from app.mcp_server.tooling.account_modes import (
    ACCOUNT_MODE_TOSS_LIVE,
    normalize_account_mode,
)
from app.services.brokers.toss import (
    TossApiDisabled,
    TossMissingCredentials,
    TossReadClient,
)
from app.services.brokers.toss.errors import TossApiResponseError

if TYPE_CHECKING:
    from fastmcp import FastMCP

TOSS_LIVE_ORDER_TOOL_NAMES: set[str] = {
    "toss_preview_order",
    "toss_place_order",
    "toss_modify_order",
    "toss_cancel_order",
    "toss_get_order_history",
    "toss_get_positions",
    "toss_get_orderable_cash",
}


def _config_error() -> None:
    if not settings.toss_api_enabled:
        raise TossApiDisabled("Toss API is disabled.")
    missing = validate_toss_api_config()
    if missing:
        raise TossMissingCredentials(
            f"Toss API is missing required configuration: {', '.join(missing)}"
        )


def _check_mode_arg(account_mode: str | None, account_type: str | None) -> None:
    routing = normalize_account_mode(account_mode, account_type)
    if routing.account_mode != ACCOUNT_MODE_TOSS_LIVE:
        raise ValueError(
            f"Invalid account_mode resolving to {routing.account_mode!r}. "
            f"Toss live tools only support account_mode='{ACCOUNT_MODE_TOSS_LIVE}'."
        )


@asynccontextmanager
async def _client_context():
    client = TossReadClient.from_settings()
    try:
        yield client
    finally:
        await client.aclose()


def _infer_market(symbol: str, market: Literal["kr", "us"] | None) -> Literal["kr", "us"]:
    if market is not None:
        val = str(market).strip().lower()
        if val not in ("kr", "us"):
            raise ValueError(f"Invalid market: {market!r}. Toss supports 'kr' or 'us'.")
        return cast(Literal["kr", "us"], val)
    clean_sym = str(symbol).strip()
    if re.match(r"^\d{6}$", clean_sym):
        return "kr"
    return "us"


def _decimal_string(value: str | int | float | None, name: str) -> Decimal:
    if value is None:
        raise ValueError(f"{name} is required")
    if isinstance(value, float):
        raise TypeError(f"{name} must be str or int, not float")
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Invalid decimal value for {name}: {value!r}") from exc


def _stringify_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return f"{normalized:f}"


def _new_client_order_id() -> str:
    return uuid.uuid4().hex


def _estimate_krw_notional(
    market: Literal["kr", "us"],
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
) -> Decimal | None:
    if market != "kr":
        return None
    if price is not None and quantity is not None:
        return price * quantity
    if order_amount is not None:
        return order_amount
    return None


def _high_value_error(
    market: Literal["kr", "us"],
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
    confirm_high_value_order: bool,
    base: dict[str, Any],
) -> dict[str, Any] | None:
    notional = _estimate_krw_notional(market, quantity, price, order_amount)
    if notional is not None and notional >= Decimal("100000000"):
        if not confirm_high_value_order:
            return {
                "success": False,
                **base,
                "error": (
                    f"High-value KR order (notional={notional} KRW >= 100M KRW) "
                    "requires confirm_high_value_order=True."
                ),
            }
    return None


async def _find_holding(client: TossReadClient, symbol: str) -> Any | None:
    res = await client.holdings(symbol=symbol)
    if res and hasattr(res, "items") and res.items:
        for item in res.items:
            if item.symbol == symbol:
                return item
    return None


async def _latest_price(client: TossReadClient, symbol: str) -> Decimal:
    res = await client.prices([symbol])
    if res:
        for p in res:
            if p.symbol == symbol:
                return p.last_price
    raise ValueError(f"Could not resolve latest price for symbol: {symbol}")


async def _sell_loss_guard(
    client: TossReadClient,
    symbol: str,
    order_type: Literal["limit", "market"],
    price: Decimal | None,
    base: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        holding = await _find_holding(client, symbol)
    except Exception as exc:
        return {
            "success": False,
            **base,
            "error": f"Failed to retrieve holdings for sell loss guard: {exc}",
        }

    if not holding:
        return {
            "success": False,
            **base,
            "error": f"No holding found for symbol {symbol} to validate sell cost basis (fail closed).",
        }

    avg = holding.average_purchase_price
    if avg <= Decimal("0"):
        return {
            "success": False,
            **base,
            "error": f"Invalid holding average purchase price for symbol {symbol}: {avg} (fail closed).",
        }

    floor = avg * Decimal("1.01")

    if order_type == "limit":
        if price is None:
            return {
                "success": False,
                **base,
                "error": "Limit sell order requires price for cost basis validation.",
            }
        if price < floor:
            return {
                "success": False,
                **base,
                "error": f"Limit sell price {price} is below average purchase price floor ({floor}) (sell floor is avg_purchase_price * 1.01).",
            }
    else:  # market
        try:
            curr_price = await _latest_price(client, symbol)
        except Exception as exc:
            return {
                "success": False,
                **base,
                "error": f"Failed to retrieve current price for market sell cost basis validation (fail closed): {exc}",
            }
        if curr_price < floor:
            return {
                "success": False,
                **base,
                "error": f"Market sell proxy price {curr_price} is below average purchase price floor ({floor}) (sell floor is avg_purchase_price * 1.01).",
            }

    return None


async def _opposite_pending_error(
    client: TossReadClient,
    symbol: str,
    side: Literal["buy", "sell"],
    base: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        page = await client.list_orders(status="OPEN", symbol=symbol)
    except Exception as exc:
        return {
            "success": False,
            **base,
            "error": f"Failed to check pending orders: {exc}",
        }
    opp_side = "SELL" if side == "buy" else "BUY"
    for order in page.orders:
        if order.symbol == symbol and order.side.upper() == opp_side:
            return {
                "success": False,
                **base,
                "error": f"An opposite pending order exists for symbol {symbol} ({opp_side}).",
            }
    return None


def _toss_error_response(exc: Exception, base: dict[str, Any]) -> dict[str, Any]:
    if isinstance(exc, TossApiResponseError):
        return {
            "success": False,
            **base,
            "error": str(exc),
            "status_code": exc.status_code,
            "code": exc.envelope.code,
            "request_id": exc.envelope.request_id,
            "message": exc.envelope.message,
            "data": exc.envelope.data,
        }
    return {
        "success": False,
        **base,
        "error": f"{type(exc).__name__}: {exc}",
    }


async def toss_preview_order(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)

    mkt = _infer_market(symbol, market)
    quantity_dec = _decimal_string(quantity, "quantity") if quantity is not None else None
    price_dec = _decimal_string(price, "price") if price is not None else None
    order_amount_dec = _decimal_string(order_amount, "order_amount") if order_amount is not None else None

    payload: dict[str, Any] = {
        "clientOrderId": _new_client_order_id(),
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "timeInForce": time_in_force,
    }
    if quantity_dec is not None:
        payload["quantity"] = _stringify_decimal(quantity_dec)
    if price_dec is not None:
        payload["price"] = _stringify_decimal(price_dec)
    if order_amount_dec is not None:
        payload["orderAmount"] = _stringify_decimal(order_amount_dec)

    return {
        "success": True,
        "preview": True,
        "market": mkt,
        "payload_preview": payload,
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
    }


async def toss_place_order(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)

    mkt = _infer_market(symbol, market)
    quantity_dec = _decimal_string(quantity, "quantity") if quantity is not None else None
    price_dec = _decimal_string(price, "price") if price is not None else None
    order_amount_dec = _decimal_string(order_amount, "order_amount") if order_amount is not None else None

    payload: dict[str, Any] = {
        "clientOrderId": _new_client_order_id(),
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "timeInForce": time_in_force,
    }
    if quantity_dec is not None:
        payload["quantity"] = _stringify_decimal(quantity_dec)
    if price_dec is not None:
        payload["price"] = _stringify_decimal(price_dec)
    if order_amount_dec is not None:
        payload["orderAmount"] = _stringify_decimal(order_amount_dec)
    if confirm_high_value_order:
        payload["confirmHighValueOrder"] = True

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "dry_run": dry_run,
        "mutation_sent": not dry_run,
    }

    if dry_run:
        return {
            "success": True,
            **base_response,
            "payload_preview": payload,
        }

    if not confirm:
        return {
            "success": False,
            **base_response,
            "error": "toss_place_order requires confirm=True when dry_run=False.",
        }

    # Guard: high-value KR order
    if (guard := _high_value_error(mkt, quantity_dec, price_dec, order_amount_dec, confirm_high_value_order, base_response)) is not None:
        return guard

    async def execute_order(client: TossReadClient):
        # Guard: opposite pending order check
        if (opp_guard := await _opposite_pending_error(client, symbol, side, base_response)) is not None:
            return opp_guard

        # Guard: sell loss check
        if side == "sell":
            if (sell_guard := await _sell_loss_guard(client, symbol, order_type, price_dec, base_response)) is not None:
                return sell_guard

        try:
            res = await client.place_order(payload)
            return {
                "success": True,
                **base_response,
                "order_id": res.order_id,
                "client_order_id": res.client_order_id,
            }
        except Exception as exc:
            return _toss_error_response(exc, base_response)

    async with _client_context() as client:
        return await execute_order(client)


async def toss_modify_order(
    order_id: str,
    new_price: str | int | None = None,
    new_quantity: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_cancel_order(
    order_id: str,
    dry_run: bool = True,
    confirm: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_get_order_history(
    status: Literal["open", "closed"] = "closed",
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_get_positions(
    symbol: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


async def toss_get_orderable_cash(
    currency: Literal["KRW", "USD"] = "KRW",
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    _config_error()
    _check_mode_arg(account_mode, account_type)
    return {"success": True}


def register_toss_live_order_tools(mcp: FastMCP) -> None:
    mcp.tool(name="toss_preview_order", description="Preview a live order on Toss Securities.")(toss_preview_order)
    mcp.tool(name="toss_place_order", description="Place a live order on Toss Securities.")(toss_place_order)
    mcp.tool(name="toss_modify_order", description="Modify a pending live order on Toss Securities.")(toss_modify_order)
    mcp.tool(name="toss_cancel_order", description="Cancel a pending live order on Toss Securities.")(toss_cancel_order)
    mcp.tool(name="toss_get_order_history", description="Retrieve live order history from Toss Securities.")(toss_get_order_history)
    mcp.tool(name="toss_get_positions", description="Retrieve current holding positions from Toss Securities.")(toss_get_positions)
    mcp.tool(name="toss_get_orderable_cash", description="Retrieve available cash/buying power from Toss Securities.")(toss_get_orderable_cash)
