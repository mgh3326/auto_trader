# app/mcp_server/tooling/orders_toss_variants.py
"""Toss Securities live MCP order tools.

Every tool is hard-pinned to ``account_mode="toss_live"``. They:
- Validate ``validate_toss_api_config`` before any side effect.
- Default mutation tools to ``dry_run=True`` and require ``confirm=True`` before any POST.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from app.core.config import settings, validate_toss_api_config
from app.mcp_server.tooling.account_modes import (
    ACCOUNT_MODE_TOSS_LIVE,
    normalize_account_mode,
)
from app.mcp_server.tooling.toss_live_ledger import (
    record_toss_place_order,
    record_toss_replacement_order,
)
from app.services.brokers.toss import TossReadClient
from app.services.brokers.toss.dto import TossWarningInfo
from app.services.brokers.toss.errors import TossApiResponseError
from app.services.brokers.toss.warnings_guard import check_warnings_guard

logger = logging.getLogger(__name__)

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
    "toss_reconcile_orders",
}


def _config_error() -> dict[str, Any] | None:
    missing = validate_toss_api_config()
    if missing:
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": (
                "Toss live account is disabled or missing required configuration: "
                + ", ".join(missing)
            ),
        }
    return None


def _check_mode_arg(
    account_mode: str | None, account_type: str | None
) -> dict[str, Any] | None:
    if account_mode is None and account_type is None:
        return None
    try:
        routing = normalize_account_mode(account_mode, account_type)
    except ValueError as exc:
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": str(exc),
        }
    if routing.account_mode != ACCOUNT_MODE_TOSS_LIVE:
        return {
            "success": False,
            "source": "toss",
            "account_mode": ACCOUNT_MODE_TOSS_LIVE,
            "error": (
                f"Invalid account_mode resolving to {routing.account_mode!r}. "
                f"Toss live tools only support account_mode='{ACCOUNT_MODE_TOSS_LIVE}'."
            ),
        }
    return None


def _entry_guard(
    account_mode: str | None, account_type: str | None
) -> dict[str, Any] | None:
    return _config_error() or _check_mode_arg(account_mode, account_type)


@asynccontextmanager
async def _client_context():
    client = TossReadClient.from_settings()
    try:
        yield client
    finally:
        await client.aclose()


def _warning_payload(
    warnings: Iterable[TossWarningInfo],
) -> list[dict[str, str | None]]:
    return [
        {
            "warning_type": w.warning_type,
            "exchange": w.exchange,
            "start_date": w.start_date,
            "end_date": w.end_date,
        }
        for w in warnings
    ]


def _infer_market(
    symbol: str, market: Literal["kr", "us"] | None
) -> Literal["kr", "us"]:
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


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _stringify_decimal(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_json_safe(inner) for inner in value]
    if isinstance(value, tuple):
        return [_json_safe(inner) for inner in value]
    return value


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


def _high_value_uncheckable(
    market: Literal["kr", "us"],
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
) -> bool:
    """True when a KR order's KRW notional cannot be estimated locally (e.g. a
    market order has no price), so the local 1억 confirm gate cannot evaluate it.
    The broker still enforces ``confirm-high-value-required`` server-side."""
    return (
        market == "kr"
        and _estimate_krw_notional(market, quantity, price, order_amount) is None
    )


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
    if notional is None and _high_value_uncheckable(
        market, quantity, price, order_amount
    ):
        logger.warning(
            "Toss KR order high-value (1억+) local gate could not be evaluated "
            "(no estimable KRW notional, e.g. market order); relying on broker-side "
            "confirm-high-value-required enforcement. confirm_high_value_order=%s",
            confirm_high_value_order,
        )
    return None


def _live_mutation_disabled_error(
    operation: str,
    base: dict[str, Any],
) -> dict[str, Any] | None:
    if bool(getattr(settings, "toss_live_order_mutations_enabled", False)):
        return None
    return {
        "success": False,
        **base,
        "error": (
            f"{operation} requires TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true. "
            "Keep live Toss mutations disabled until the accepted-order ledger "
            "and operator live-smoke hold are cleared."
        ),
    }


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
    cursor: str | None = None
    seen_cursors: set[str] = set()
    try:
        while True:
            page = await client.list_orders(status="OPEN", symbol=symbol, cursor=cursor)
            opp_side = "SELL" if side == "buy" else "BUY"
            for order in page.orders:
                if order.symbol == symbol and order.side.upper() == opp_side:
                    return {
                        "success": False,
                        **base,
                        "error": f"An opposite pending order exists for symbol {symbol} ({opp_side}).",
                    }
            if not page.has_next:
                return None
            next_cursor = page.next_cursor
            if not next_cursor or next_cursor in seen_cursors:
                return {
                    "success": False,
                    **base,
                    "error": (
                        "Failed to check all pending orders: Toss pagination cursor "
                        "was missing or repeated (fail closed)."
                    ),
                }
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    except Exception as exc:
        return {
            "success": False,
            **base,
            "error": f"Failed to check pending orders: {exc}",
        }


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


_SAFE_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _order_id_error(order_id: str, base: dict[str, Any]) -> dict[str, Any] | None:
    candidate = (order_id or "").strip()
    if not candidate or not _SAFE_ORDER_ID_RE.fullmatch(candidate):
        return {
            "success": False,
            **base,
            "error": f"Unsafe order id rejected: {order_id!r}",
        }
    return None


_SAFE_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")


def _client_order_id_error(
    client_order_id: str | None, base: dict[str, Any]
) -> dict[str, Any] | None:
    if client_order_id is None:
        return None
    candidate = client_order_id.strip()
    if (
        not candidate
        or candidate != client_order_id
        or not _SAFE_CLIENT_ORDER_ID_RE.fullmatch(candidate)
    ):
        return {
            "success": False,
            **base,
            "error": f"Unsafe client order id rejected: {client_order_id!r}",
        }
    return None


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
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    mkt = _infer_market(symbol, market)
    quantity_dec = (
        _decimal_string(quantity, "quantity") if quantity is not None else None
    )
    price_dec = _decimal_string(price, "price") if price is not None else None
    order_amount_dec = (
        _decimal_string(order_amount, "order_amount")
        if order_amount is not None
        else None
    )

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

    warnings_list = []
    warnings_check_msg = None
    try:
        async with _client_context() as client:
            guard_res = await check_warnings_guard(
                client, symbol, market=mkt, side=side
            )
            warnings_list = [
                {
                    "warning_type": w.warning_type,
                    "exchange": w.exchange,
                    "start_date": w.start_date,
                    "end_date": w.end_date,
                }
                for w in guard_res.warnings
            ]
            if guard_res.error_message:
                warnings_check_msg = guard_res.error_message
    except Exception as exc:
        logger.error("Failed to check warnings in preview: %s", exc, exc_info=True)
        warnings_check_msg = f"Failed to check warnings: {exc}"

    return {
        "success": True,
        "preview": True,
        "market": mkt,
        "payload_preview": payload,
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "warnings": warnings_list,
        "warnings_check_message": warnings_check_msg,
    }


async def _toss_place_order_impl(
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
    reason: str | None = None,
    exit_reason: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: str | int | None = None,
    stop_loss: str | int | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
    report_item_uuid: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
    client_order_id_override: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    mkt = _infer_market(symbol, market)
    quantity_dec = (
        _decimal_string(quantity, "quantity") if quantity is not None else None
    )
    price_dec = _decimal_string(price, "price") if price is not None else None
    order_amount_dec = (
        _decimal_string(order_amount, "order_amount")
        if order_amount is not None
        else None
    )
    target_price_dec = (
        _decimal_string(target_price, "target_price")
        if target_price is not None
        else None
    )
    stop_loss_dec = (
        _decimal_string(stop_loss, "stop_loss") if stop_loss is not None else None
    )

    payload: dict[str, Any] = {
        "clientOrderId": client_order_id_override or _new_client_order_id(),
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
        "mutation_sent": False,
        # ROB-545 Major — carry the clientOrderId on every response (incl. error
        # paths) so a failed/timed-out order can be retried with the *same*
        # idempotency key instead of minting a new one.
        "client_order_id": payload["clientOrderId"],
    }

    if (
        id_guard := _client_order_id_error(client_order_id_override, base_response)
    ) is not None:
        return id_guard

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
    if (
        guard := _high_value_error(
            mkt,
            quantity_dec,
            price_dec,
            order_amount_dec,
            confirm_high_value_order,
            base_response,
        )
    ) is not None:
        return guard

    if (
        mutation_gate := _live_mutation_disabled_error(
            "toss_place_order", base_response
        )
    ) is not None:
        return mutation_gate

    async def execute_order(client: TossReadClient):
        # Guard: Warnings check
        guard_res = await check_warnings_guard(client, symbol, market=mkt, side=side)
        guard_warnings = _warning_payload(guard_res.warnings)
        if not guard_res.ok:
            return {
                "success": False,
                **base_response,
                "error": guard_res.error_message,
                "warnings": guard_warnings,
            }

        # Guard: opposite pending order check
        if (
            opp_guard := await _opposite_pending_error(
                client, symbol, side, base_response
            )
        ) is not None:
            return opp_guard

        # Guard: sell loss check
        if side == "sell":
            if (
                sell_guard := await _sell_loss_guard(
                    client, symbol, order_type, price_dec, base_response
                )
            ) is not None:
                return sell_guard

        res = None
        try:
            res = await client.place_order(payload)
            raw_response = {
                "orderId": res.order_id,
                "clientOrderId": res.client_order_id,
                "payload": _json_safe(payload),
            }
            ledger = await record_toss_place_order(
                market=mkt,
                symbol=symbol,
                side=side,
                order_type=order_type,
                time_in_force=time_in_force,
                quantity=quantity_dec,
                price=price_dec,
                order_amount=order_amount_dec,
                currency=("KRW" if mkt == "kr" else "USD"),
                client_order_id=res.client_order_id or str(payload["clientOrderId"]),
                broker_order_id=res.order_id,
                raw_response=raw_response,
                reason=reason,
                exit_reason=exit_reason,
                thesis=thesis,
                strategy=strategy,
                target_price=target_price_dec,
                stop_loss=stop_loss_dec,
                min_hold_days=min_hold_days,
                notes=notes,
                indicators_snapshot=indicators_snapshot,
                report_item_uuid=report_item_uuid,
            )
            return {
                "success": True,
                **base_response,
                "mutation_sent": True,
                "order_id": res.order_id,
                "client_order_id": res.client_order_id,
                **ledger,
                "warnings": guard_warnings,
                "warnings_check_message": guard_res.error_message,
                "message": (
                    "Toss live order accepted and recorded accepted-only; "
                    "run toss_reconcile_orders to book confirmed fills."
                ),
            }
        except Exception as exc:
            err = _toss_error_response(exc, {**base_response, "mutation_sent": True})
            # ROB-545 Major/B2 — if the POST already reached Toss (res set) the
            # broker order id must survive a ledger-write failure or idempotency
            # anomaly, so reconcile/cancel can still find the live order.
            if res is not None:
                err["order_id"] = res.order_id
                if res.client_order_id is not None:
                    err["client_order_id"] = res.client_order_id
            return err

    async with _client_context() as client:
        return await execute_order(client)


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
    reason: str | None = None,
    exit_reason: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: str | int | None = None,
    stop_loss: str | int | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
    report_item_uuid: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    return await _toss_place_order_impl(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        order_amount=order_amount,
        market=market,
        time_in_force=time_in_force,
        dry_run=dry_run,
        confirm=confirm,
        confirm_high_value_order=confirm_high_value_order,
        reason=reason,
        exit_reason=exit_reason,
        thesis=thesis,
        strategy=strategy,
        target_price=target_price,
        stop_loss=stop_loss,
        min_hold_days=min_hold_days,
        notes=notes,
        indicators_snapshot=indicators_snapshot,
        report_item_uuid=report_item_uuid,
        account_mode=account_mode,
        account_type=account_type,
        client_order_id_override=None,
    )


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
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "dry_run": dry_run,
        "mutation_sent": False,
    }

    if (id_guard := _order_id_error(order_id, base_response)) is not None:
        return id_guard

    if not dry_run and not confirm:
        return {
            "success": False,
            **base_response,
            "error": "toss_modify_order requires confirm=True when dry_run=False.",
        }

    async def execute_modify(client: TossReadClient):
        try:
            orig_order = await client.get_order(order_id)
        except Exception as exc:
            return _toss_error_response(exc, base_response)

        symbol = orig_order.symbol
        side = orig_order.side.lower()
        orig_order_type = orig_order.order_type.lower()

        mkt = _infer_market(symbol, market)
        new_price_dec = (
            _decimal_string(new_price, "new_price") if new_price is not None else None
        )
        new_quantity_dec = (
            _decimal_string(new_quantity, "new_quantity")
            if new_quantity is not None
            else None
        )

        if mkt == "kr":
            if new_price_dec is None or new_quantity_dec is None:
                return {
                    "success": False,
                    **base_response,
                    "error": "Toss KR order modify requires both new_price and new_quantity.",
                }
        else:  # us
            if new_quantity_dec is not None:
                return {
                    "success": False,
                    **base_response,
                    "error": "Toss US order modify rejects new_quantity; only price modification is supported.",
                }
            if new_price_dec is None:
                return {
                    "success": False,
                    **base_response,
                    "error": "Toss US order modify requires new_price.",
                }

        payload: dict[str, Any] = {
            "orderType": orig_order_type.upper(),
        }
        if new_price_dec is not None:
            payload["price"] = _stringify_decimal(new_price_dec)
        if new_quantity_dec is not None:
            payload["quantity"] = _stringify_decimal(new_quantity_dec)
        if confirm_high_value_order:
            payload["confirmHighValueOrder"] = True

        if side == "sell" and orig_order_type == "limit":
            if (
                sell_guard := await _sell_loss_guard(
                    client, symbol, "limit", new_price_dec, base_response
                )
            ) is not None:
                return sell_guard

        if (
            high_value_guard := _high_value_error(
                mkt,
                new_quantity_dec,
                new_price_dec,
                None,
                confirm_high_value_order,
                base_response,
            )
        ) is not None:
            return high_value_guard

        if not dry_run:
            mutation_gate = _live_mutation_disabled_error(
                "toss_modify_order", base_response
            )
            if mutation_gate is not None:
                return mutation_gate

        if dry_run:
            return {
                "success": True,
                **base_response,
                "original_order_id": order_id,
                "payload_preview": payload,
            }

        res = None
        try:
            res = await client.modify_order(order_id, payload)
            ledger = await record_toss_replacement_order(
                operation_kind="modify",
                market=mkt,
                symbol=symbol,
                side=side,
                order_type=orig_order_type,
                time_in_force=orig_order.time_in_force,
                quantity=new_quantity_dec or orig_order.quantity,
                price=new_price_dec or orig_order.price,
                order_amount=orig_order.order_amount,
                currency=orig_order.currency,
                original_order_id=order_id,
                replacement_order_id=res.order_id,
                raw_response={
                    "operation": "modify",
                    "originalOrderId": order_id,
                    "replacementOrderId": res.order_id,
                    "payload": _json_safe(payload),
                },
            )
            return {
                "success": True,
                **base_response,
                "mutation_sent": True,
                "original_order_id": order_id,
                "replacement_order_id": res.order_id,
                "operation_semantics": "Toss modify returns a newly issued orderId; it is not the original order id.",
                **ledger,
            }
        except Exception as exc:
            err = _toss_error_response(exc, {**base_response, "mutation_sent": True})
            # ROB-545 Major — keep the order ids on the error path so the live
            # order (and any issued replacement) can be reconciled/cancelled.
            err.setdefault("original_order_id", order_id)
            if res is not None:
                err["replacement_order_id"] = res.order_id
                err.setdefault("order_id", res.order_id)
            return err

    async with _client_context() as client:
        return await execute_modify(client)


async def toss_cancel_order(
    order_id: str,
    dry_run: bool = True,
    confirm: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "dry_run": dry_run,
        "mutation_sent": False,
    }

    if (id_guard := _order_id_error(order_id, base_response)) is not None:
        return id_guard

    if dry_run:
        return {
            "success": True,
            **base_response,
            "original_order_id": order_id,
        }

    if not confirm:
        return {
            "success": False,
            **base_response,
            "error": "toss_cancel_order requires confirm=True when dry_run=False.",
        }

    if (
        mutation_gate := _live_mutation_disabled_error(
            "toss_cancel_order", base_response
        )
    ) is not None:
        return mutation_gate

    res = None
    try:
        async with _client_context() as client:
            try:
                orig_order = await client.get_order(order_id)
            except Exception as exc:
                return _toss_error_response(
                    exc, {**base_response, "original_order_id": order_id}
                )
            mkt = _infer_market(orig_order.symbol, None)
            res = await client.cancel_order(order_id)
            ledger = await record_toss_replacement_order(
                operation_kind="cancel",
                market=mkt,
                symbol=orig_order.symbol,
                side=str(orig_order.side).lower(),
                order_type=str(orig_order.order_type).lower(),
                time_in_force=orig_order.time_in_force,
                quantity=orig_order.quantity,
                price=orig_order.price,
                order_amount=orig_order.order_amount,
                currency=orig_order.currency,
                original_order_id=order_id,
                replacement_order_id=res.order_id,
                raw_response={
                    "operation": "cancel",
                    "originalOrderId": order_id,
                    "replacementOrderId": res.order_id,
                },
            )
            return {
                "success": True,
                **base_response,
                "mutation_sent": True,
                "original_order_id": order_id,
                "replacement_order_id": res.order_id,
                **ledger,
                "operation_semantics": "Toss cancel returns a newly issued orderId; it is not the original order id.",
            }
    except Exception as exc:
        err = _toss_error_response(exc, {**base_response, "mutation_sent": True})
        # ROB-545 Major — keep the order ids on the error path so the live order
        # (and any issued cancel replacement) can be reconciled/cancelled.
        err.setdefault("original_order_id", order_id)
        if res is not None:
            err["replacement_order_id"] = res.order_id
            err.setdefault("order_id", res.order_id)
        return err


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
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
    }

    toss_status = "CLOSED" if status == "closed" else "OPEN"

    try:
        async with _client_context() as client:
            page = await client.list_orders(
                status=toss_status,
                symbol=symbol,
                from_date=from_date,
                to_date=to_date,
                cursor=cursor,
                limit=limit,
            )
            orders_list = []
            for o in page.orders:
                orders_list.append(
                    {
                        "order_id": o.order_id,
                        "symbol": o.symbol,
                        "side": o.side,
                        "order_type": o.order_type,
                        "time_in_force": o.time_in_force,
                        "status": o.status,
                        "price": _stringify_decimal(o.price)
                        if o.price is not None
                        else None,
                        "quantity": _stringify_decimal(o.quantity)
                        if o.quantity is not None
                        else None,
                        "order_amount": _stringify_decimal(o.order_amount)
                        if o.order_amount is not None
                        else None,
                        "currency": o.currency,
                        "ordered_at": _json_safe(o.ordered_at),
                        "canceled_at": _json_safe(o.canceled_at),
                        "execution": _json_safe(o.execution),
                    }
                )
            return {
                "success": True,
                **base_response,
                "orders": orders_list,
                "next_cursor": page.next_cursor,
                "has_next": page.has_next,
            }
    except Exception as exc:
        return _toss_error_response(exc, base_response)


async def toss_get_positions(
    symbol: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
    }

    try:
        async with _client_context() as client:
            res = await client.holdings(symbol=symbol)
            items_list = []
            for item in res.items:
                items_list.append(
                    {
                        "symbol": item.symbol,
                        "name": item.name,
                        "market_country": item.market_country,
                        "currency": item.currency,
                        "quantity": _stringify_decimal(item.quantity)
                        if item.quantity is not None
                        else None,
                        "last_price": _stringify_decimal(item.last_price)
                        if item.last_price is not None
                        else None,
                        "average_purchase_price": _stringify_decimal(
                            item.average_purchase_price
                        )
                        if item.average_purchase_price is not None
                        else None,
                        "market_value": _json_safe(item.market_value),
                        "profit_loss": _json_safe(item.profit_loss),
                        "daily_profit_loss": _json_safe(item.daily_profit_loss),
                        "cost": _json_safe(item.cost),
                    }
                )
            return {
                "success": True,
                **base_response,
                "items": items_list,
                "overview": _json_safe(res.raw_overview),
            }
    except Exception as exc:
        return _toss_error_response(exc, base_response)


async def toss_get_orderable_cash(
    currency: Literal["KRW", "USD"] = "KRW",
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard

    base_response = {
        "source": "toss",
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
    }

    try:
        async with _client_context() as client:
            res = await client.buying_power(currency=currency)
            return {
                "success": True,
                **base_response,
                "cash_buying_power": _stringify_decimal(res.cash_buying_power),
                "currency": res.currency,
            }
    except Exception as exc:
        return _toss_error_response(exc, base_response)


async def toss_reconcile_orders(
    symbol: str | None = None,
    order_id: str | None = None,
    market: Literal["kr", "us"] | None = None,
    dry_run: bool = True,
    limit: int = 100,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
    if (guard := _entry_guard(account_mode, account_type)) is not None:
        return guard
    from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl

    return await toss_reconcile_orders_impl(
        symbol=symbol,
        order_id=order_id,
        market=market,
        dry_run=dry_run,
        limit=limit,
    )


def register_toss_live_order_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="toss_preview_order",
        description=(
            "Preview a Toss Securities live KR/US order without sending. "
            "Hard-pinned to account_mode='toss_live'; matching account_mode is "
            "optional and any other value is rejected. market='kr'|'us' is "
            "accepted or inferred from symbol. This is read-only but still "
            "requires TOSS_API_ENABLED and Toss credentials."
        ),
    )(toss_preview_order)
    mcp.tool(
        name="toss_place_order",
        description=(
            "Place a Toss Securities live KR/US order. Hard-pinned to "
            "account_mode='toss_live'; market='kr'|'us' is accepted or inferred. "
            "dry_run=True by default and sends no mutation. Real submission "
            "requires dry_run=False, confirm=True, and "
            "TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true. 100M KRW+ computable KR "
            "orders require operator-supplied confirm_high_value_order=True; it "
            "is never inferred. Before POST the tool blocks opposite pending "
            "orders across all paginated OPEN pages and applies the live sell "
            "loss guard (sell price/current market proxy must be >= "
            "avg_purchase_price*1.01). Supports optional metadata (note, "
            "reason, strategy, signal) for ledger recording."
        ),
    )(toss_place_order)
    mcp.tool(
        name="toss_modify_order",
        description=(
            "Modify a pending Toss Securities live order. Hard-pinned to "
            "account_mode='toss_live'. dry_run=True by default; real submission "
            "requires dry_run=False, confirm=True, and "
            "TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true. KR modify requires both "
            "new_price and new_quantity. US modify requires new_price and "
            "rejects new_quantity. Sell reprices are blocked below "
            "avg_purchase_price*1.01. Toss returns a newly issued replacement "
            "orderId for successful modify."
        ),
    )(toss_modify_order)
    mcp.tool(
        name="toss_cancel_order",
        description=(
            "Cancel a pending Toss Securities live order. Hard-pinned to "
            "account_mode='toss_live'. dry_run=True by default; real submission "
            "requires dry_run=False, confirm=True, and "
            "TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true. Toss returns a newly issued "
            "replacement orderId for successful cancel."
        ),
    )(toss_cancel_order)
    mcp.tool(
        name="toss_get_order_history",
        description=(
            "Retrieve Toss Securities live order history for account_mode='toss_live'. "
            "Supports status='open'|'closed'; closed history supports cursor/limit "
            "pagination."
        ),
    )(toss_get_order_history)
    mcp.tool(
        name="toss_get_positions",
        description=(
            "Retrieve Toss Securities live KR/US positions for "
            "account_mode='toss_live'. Read-only and default-disabled by Toss "
            "API config."
        ),
    )(toss_get_positions)
    mcp.tool(
        name="toss_get_orderable_cash",
        description=(
            "Retrieve Toss Securities live orderable cash/buying power for "
            "account_mode='toss_live' in currency='KRW'|'USD'. Read-only and "
            "default-disabled by Toss API config."
        ),
    )(toss_get_orderable_cash)
    mcp.tool(
        name="toss_reconcile_orders",
        description=(
            "Reconcile Toss Securities live KR/US orders from the local "
            "review.toss_live_order_ledger against single-order broker evidence "
            "from GET /orders/{orderId}. Books fill/journal/realized_pnl only "
            "from confirmed execution evidence and is delta-idempotent. "
            "dry_run=True by default."
        ),
    )(toss_reconcile_orders)
