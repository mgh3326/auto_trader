# app/mcp_server/tooling/orders_kiwoom_variants.py
"""Kiwoom mock-only MCP tools.

Every tool is hard-pinned to ``account_mode="kiwoom_mock"``. They:
- Validate ``validate_kiwoom_mock_config`` before any side effect.
- Reject anything except KR equity (``market="kr"``).
- Reject ``NXT``/``SOR`` exchanges.
- Reject unsafe order ids (path separators, query fragments, whitespace,
  commas, newlines).
- Default order-like tools to ``dry_run=True`` and never call the broker
  unless ``dry_run=False`` AND ``confirm=True`` are both supplied.

Mirrors the structure of ``orders_kis_variants.py``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal, cast

from app.core.config import validate_kiwoom_mock_config
from app.mcp_server.tooling.orders_kiwoom_shared import (
    derive_broker_success as _derive_broker_success,
)
from app.mcp_server.tooling.orders_kiwoom_shared import (
    finalize_broker_response as _finalize_broker_response,
)
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.client import KiwoomMockClient, KiwoomPreDispatchError
from app.services.brokers.kiwoom.domestic_account import KiwoomDomesticAccountClient
from app.services.brokers.kiwoom.domestic_orders import KiwoomDomesticOrderClient
from app.services.brokers.kiwoom.normalization import (
    KiwoomMockEvidenceError,
    build_mock_provenance,
    normalize_deposit,
    normalize_orderable_cash,
    normalize_orders,
    normalize_positions,
    redact_broker_response,
    validate_mock_response_provenance,
)
from app.services.brokers.kiwoom.order_preflight import (
    PreflightResult,
    run_order_preflight,
)
from app.services.brokers.kiwoom.validation import normalize_krx_symbol

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ("_derive_broker_success",)

ACCOUNT_MODE_KIWOOM_MOCK = "kiwoom_mock"

KIWOOM_MOCK_TOOL_NAMES: set[str] = {
    "kiwoom_mock_preview_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_modify_order",
    "kiwoom_mock_get_order_history",
    "kiwoom_mock_get_positions",
    "kiwoom_mock_get_orderable_cash",
}

_SAFE_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _mock_config_error() -> dict[str, Any] | None:
    missing = validate_kiwoom_mock_config()
    if not missing:
        return None
    return {
        "success": False,
        "error": (
            "Kiwoom mock account is disabled or missing required configuration: "
            + ", ".join(missing)
        ),
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


def _market_error(market: str | None) -> dict[str, Any] | None:
    if market is None:
        return None
    if str(market).strip().lower() != "kr":
        return {
            "success": False,
            "error": "kiwoom_mock tools only support market='kr' (KR equity).",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    return None


def _exchange_error(exchange: str | None) -> dict[str, Any] | None:
    if exchange is None:
        return None
    value = str(exchange).strip().upper()
    if (
        value in constants.MOCK_REJECTED_EXCHANGES
        or value != constants.MOCK_EXCHANGE_KRX
    ):
        return {
            "success": False,
            "error": f"kiwoom_mock supports KRX only; rejected exchange={exchange!r}.",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    return None


def _order_id_error(order_id: str) -> dict[str, Any] | None:
    candidate = (order_id or "").strip()
    if not candidate or not _SAFE_ORDER_ID_RE.fullmatch(candidate):
        return {
            "success": False,
            "error": f"Unsafe order id rejected by kiwoom_mock: {order_id!r}",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    return None


def _symbol_error(symbol: str) -> dict[str, Any] | None:
    try:
        normalize_krx_symbol(symbol)
    except ValueError as exc:
        return {
            "success": False,
            "error": str(exc),
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    return None


def _positive_amount_error(
    name: str, value: float | int | None
) -> dict[str, Any] | None:
    """Reject zero/negative quantities and prices before any broker call."""

    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": f"kiwoom_mock requires {name} to be numeric; got {value!r}",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    if not numeric > 0:
        return {
            "success": False,
            "error": f"kiwoom_mock requires {name} > 0; got {value!r}",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        }
    return None


# ---------------------------------------------------------------------------
# Shared broker-response shaping (ROB-319).

_MUTATION_PASSTHROUGH_KEYS = (
    "return_code",
    "return_msg",
    "continuation",
    "ord_no",
    "order_no",
)


def _stable_read_failure(
    *,
    result_key: Literal["positions", "orders", "cash"],
    result_value: list[Any] | None,
    api_id: str,
    error: str,
    error_detail: str | None = None,
    broker_response: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "success": False,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        result_key: result_value,
        "provenance": build_mock_provenance(api_id),
        "error": error,
        **(extra or {}),
    }
    if error_detail:
        response["error_detail"] = error_detail
    if broker_response is not None:
        redacted_broker_response = redact_broker_response(broker_response)
        response["broker_response"] = redacted_broker_response
        for key in _MUTATION_PASSTHROUGH_KEYS:
            if key in redacted_broker_response:
                response[key] = redacted_broker_response[key]
    return response


def _finalize_normalized_read_response(
    base: dict[str, Any],
    broker_response: dict[str, Any],
    *,
    api_id: str,
    result_key: Literal["positions", "orders"],
) -> dict[str, Any]:
    response = _finalize_broker_response(base, broker_response)
    response["provenance"] = build_mock_provenance(api_id)
    response[result_key] = []
    if not response["success"]:
        response["error"] = "kiwoom_mock_broker_error"
        return response
    try:
        validate_mock_response_provenance(broker_response)
        response[result_key] = (
            normalize_positions(broker_response)
            if result_key == "positions"
            else normalize_orders(broker_response)
        )
    except KiwoomMockEvidenceError as exc:
        response["success"] = False
        response["error"] = exc.code
        response["error_detail"] = str(exc)
    return response


# ---------------------------------------------------------------------------
# Implementation seams (overridable via monkeypatch in tests).


async def _fetch_kr_quote_for_preflight(symbol: str) -> tuple[int | None, str]:
    try:
        from app.mcp_server.tooling.market_data_quotes import _get_quote_impl

        quote = await _get_quote_impl(symbol, "kr")
        raw_price = quote.get("price")
        freshness = str(quote.get("price_freshness") or "unavailable")
        if raw_price is not None:
            return int(raw_price), freshness
        return None, freshness
    except Exception:
        return None, "unavailable"


def _new_kiwoom_mock_client() -> KiwoomMockClient:
    """Request-scoped KiwoomMockClient factory.

    Single chokepoint for client construction so tests can count/inject.
    Each place/preview flow builds exactly one client and reuses it across
    preflight + POST to share one auth client + one cold token.
    """
    return KiwoomMockClient.from_app_settings()


async def _run_preflight_for_kiwoom_mock(
    symbol: str,
    side: str,
    quantity: int,
    price: int,
    *,
    account_client: Any | None = None,
) -> PreflightResult:
    quote_price, quote_freshness = await _fetch_kr_quote_for_preflight(symbol)
    if account_client is None:
        account_client = KiwoomDomesticAccountClient(cast(Any, _new_kiwoom_mock_client()))
    return await run_order_preflight(
        account_client=account_client,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        quote_price=quote_price,
        quote_freshness=quote_freshness,
    )


def _preflight_to_response(
    result: PreflightResult,
    *,
    symbol: str,
    side: str,
    quantity: int,
    price: int,
    preview: bool,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "success": result.ok,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
    }
    if preview:
        response["preview"] = True
    if not result.ok:
        response["error"] = result.error_code
        response["error_detail"] = result.error_detail
    response.update(result.to_response_extras())
    return response


def _not_submitted_response(
    base: dict[str, Any], exc: KiwoomPreDispatchError
) -> dict[str, Any]:
    """Pre-dispatch failure: request provably never sent. No reconcile needed.

    Reads ONLY the structured fields on exc — never str(exc) or exc.__cause__
    (the redaction guarantee). token/secret/header/account/body/raw msg are
    never surfaced.
    """
    return {
        **base,
        "success": False,
        "error": f"kiwoom_mock_place_order failed: {exc.cause_type}",
        "status": "not_submitted",
        "dispatch_started": False,
        "stage": exc.stage,
        "api_id": exc.api_id,
        "cause_type": exc.cause_type,
        "reconcile_required": False,
    }


def _dispatch_unknown_response(base: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """Post-dispatch / unclassifiable failure: request MAY have been sent.

    Conservative default (Oracle Q6): cannot prove the request wasn't
    transmitted (send may have succeeded before response/parsing failed), so
    reconciliation is REQUIRED. Only KiwoomPreDispatchError earns
    reconcile_required=False.
    """
    return {
        **base,
        "success": False,
        "error": f"kiwoom_mock_place_order failed: {type(exc).__name__}",
        "status": "acceptance_uncertain",
        "reconcile_required": True,
        "retry_allowed": False,
    }


async def _kiwoom_mock_place_order_impl(**kwargs: Any) -> dict[str, Any]:
    symbol = str(kwargs.get("symbol") or "").strip()
    side = kwargs.get("side")
    quantity_value = kwargs.get("quantity")
    price_value = kwargs.get("price")
    if quantity_value is None or price_value is None:
        raise ValueError("kiwoom_mock_place_order requires quantity and price")
    quantity = int(quantity_value)
    price = int(price_value)
    dry_run = bool(kwargs.get("dry_run", True))
    exchange = kwargs.get("exchange") or constants.MOCK_EXCHANGE_KRX

    base_response = {
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "dry_run": dry_run,
        "side": side,
        "symbol": symbol,
        "quantity": quantity,
        "price": price,
        "exchange": str(exchange).strip().upper(),
    }
    if side not in {"buy", "sell"}:
        return {
            "success": False,
            **base_response,
            "error": f"kiwoom_mock_place_order supports side='buy' or 'sell'; got {side!r}.",
        }

    # ONE request-scoped client shared across preflight + POST (ROB-893 v2).
    client = _new_kiwoom_mock_client()
    account_client = KiwoomDomesticAccountClient(cast(Any, client))
    order_client = KiwoomDomesticOrderClient(cast(Any, client))

    try:
        preflight = await _run_preflight_for_kiwoom_mock(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            account_client=account_client,
        )
    except Exception as exc:  # noqa: BLE001 - preflight must fail closed
        return {
            "success": False,
            **base_response,
            "error": f"kiwoom_mock_place_order preflight failed: {type(exc).__name__}",
        }
    if not preflight.ok:
        response = _preflight_to_response(
            preflight,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            preview=dry_run,
        )
        response["dry_run"] = dry_run
        response["exchange"] = str(exchange).strip().upper()
        return response

    if dry_run:
        response = _preflight_to_response(
            preflight,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            preview=True,
        )
        response["dry_run"] = True
        response["exchange"] = str(exchange).strip().upper()
        return response

    # Confirmed: the single preflight above IS the mutation-boundary check,
    # run immediately before POST on the SAME client/auth/token. Attach its
    # evidence as the authoritative snapshot.
    base_response.update(preflight.to_response_extras())

    try:
        if side == "buy":
            broker_response = await order_client.place_buy_order(
                symbol=symbol,
                quantity=quantity,
                price=price,
                exchange=exchange,
            )
        else:
            broker_response = await order_client.place_sell_order(
                symbol=symbol,
                quantity=quantity,
                price=price,
                exchange=exchange,
            )
    except KiwoomPreDispatchError as exc:
        return _not_submitted_response(base_response, exc)
    except Exception as exc:  # noqa: BLE001 - post-dispatch: may have been sent
        return _dispatch_unknown_response(base_response, exc)

    return _finalize_broker_response(base_response, broker_response)


async def _kiwoom_mock_preview_impl(**kwargs: Any) -> dict[str, Any]:
    symbol = str(kwargs.get("symbol") or "").strip()
    side = kwargs.get("side")
    quantity = int(kwargs.get("quantity"))
    price = int(kwargs.get("price"))

    preflight = await _run_preflight_for_kiwoom_mock(
        symbol=symbol, side=side, quantity=quantity, price=price
    )
    return _preflight_to_response(
        preflight,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        preview=True,
    )


async def _kiwoom_mock_cancel_impl(**kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "dry_run": kwargs.get("dry_run", True),
        "order_id": kwargs.get("order_id"),
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


async def _kiwoom_mock_cancel_confirmed_impl(**kwargs: Any) -> dict[str, Any]:
    order_id = str(kwargs.get("order_id") or "").strip()
    symbol = str(kwargs.get("symbol") or "").strip()
    cancel_quantity = int(kwargs["cancel_quantity"])
    exchange = kwargs.get("exchange") or constants.MOCK_EXCHANGE_KRX
    base = {
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "dry_run": False,
        "order_id": order_id,
        "symbol": symbol,
        "cancel_quantity": cancel_quantity,
    }
    try:
        client = KiwoomMockClient.from_app_settings()
        order_client = KiwoomDomesticOrderClient(cast(Any, client))
        broker_response = await order_client.cancel_order(
            original_order_no=order_id,
            symbol=symbol,
            cancel_quantity=cancel_quantity,
            exchange=exchange,
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return {
            "success": False,
            **base,
            "error": f"kiwoom_mock_cancel_order failed: {type(exc).__name__}: {exc}",
        }
    return _finalize_broker_response(base, broker_response)


async def _kiwoom_mock_modify_impl(**kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "dry_run": kwargs.get("dry_run", True),
        "order_id": kwargs.get("order_id"),
        "new_price": kwargs.get("new_price"),
        "new_quantity": kwargs.get("new_quantity"),
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
    }


async def _kiwoom_mock_modify_confirmed_impl(**kwargs: Any) -> dict[str, Any]:
    order_id = str(kwargs.get("order_id") or "").strip()
    symbol = str(kwargs.get("symbol") or "").strip()
    new_price = int(kwargs["new_price"])
    new_quantity = int(kwargs["new_quantity"])
    exchange = kwargs.get("exchange") or constants.MOCK_EXCHANGE_KRX
    base = {
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "dry_run": False,
        "order_id": order_id,
        "symbol": symbol,
        "new_price": new_price,
        "new_quantity": new_quantity,
    }
    try:
        client = KiwoomMockClient.from_app_settings()
        order_client = KiwoomDomesticOrderClient(cast(Any, client))
        broker_response = await order_client.modify_order(
            original_order_no=order_id,
            symbol=symbol,
            new_quantity=new_quantity,
            new_price=new_price,
            exchange=exchange,
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return {
            "success": False,
            **base,
            "error": f"kiwoom_mock_modify_order failed: {type(exc).__name__}: {exc}",
        }
    return _finalize_broker_response(base, broker_response)


async def _kiwoom_mock_order_history_impl(**kwargs: Any) -> dict[str, Any]:
    cont_yn = kwargs.get("cont_yn")
    next_key = kwargs.get("next_key")
    try:
        client = KiwoomMockClient.from_app_settings()
        account_client = KiwoomDomesticAccountClient(cast(Any, client))
        broker_response = await account_client.get_order_status(
            cont_yn=cont_yn, next_key=next_key
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return _stable_read_failure(
            result_key="orders",
            result_value=[],
            api_id=constants.ACCOUNT_ORDER_STATUS_API_ID,
            error="kiwoom_mock_transport_error",
            error_detail=(
                f"kiwoom_mock_get_order_history transport failed: {type(exc).__name__}"
            ),
        )
    return _finalize_normalized_read_response(
        {"source": "kiwoom", "account_mode": ACCOUNT_MODE_KIWOOM_MOCK},
        broker_response,
        api_id=constants.ACCOUNT_ORDER_STATUS_API_ID,
        result_key="orders",
    )


async def _kiwoom_mock_positions_impl(**kwargs: Any) -> dict[str, Any]:
    cont_yn = kwargs.get("cont_yn")
    next_key = kwargs.get("next_key")
    try:
        client = KiwoomMockClient.from_app_settings()
        account_client = KiwoomDomesticAccountClient(cast(Any, client))
        broker_response = await account_client.get_balance(
            cont_yn=cont_yn, next_key=next_key
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return _stable_read_failure(
            result_key="positions",
            result_value=[],
            api_id=constants.ACCOUNT_BALANCE_API_ID,
            error="kiwoom_mock_transport_error",
            error_detail=(
                f"kiwoom_mock_get_positions transport failed: {type(exc).__name__}"
            ),
        )
    return _finalize_normalized_read_response(
        {"source": "kiwoom", "account_mode": ACCOUNT_MODE_KIWOOM_MOCK},
        broker_response,
        api_id=constants.ACCOUNT_BALANCE_API_ID,
        result_key="positions",
    )


async def _kiwoom_mock_orderable_cash_impl(**kwargs: Any) -> dict[str, Any]:
    symbol_raw = kwargs.get("symbol")
    symbol = None if symbol_raw is None else normalize_krx_symbol(symbol_raw)
    side = kwargs.get("side")
    price = kwargs.get("price")
    cont_yn = kwargs.get("cont_yn")
    next_key = kwargs.get("next_key")

    if symbol is not None:
        base_source = "orderable_amount"
        api_id = constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID
        normalizer = normalize_orderable_cash
    else:
        base_source = "deposit"
        api_id = constants.ACCOUNT_DEPOSIT_API_ID
        normalizer = normalize_deposit

    extra: dict[str, Any] = {
        "cash_source": f"{base_source}_unavailable",
    }
    if symbol is not None:
        extra["symbol"] = symbol

    try:
        client = KiwoomMockClient.from_app_settings()
        account_client = KiwoomDomesticAccountClient(cast(Any, client))
        if symbol is not None:
            broker_response = await account_client.get_orderable_amount(
                symbol=symbol,
                side=side,
                price=price,
                cont_yn=cont_yn,
                next_key=next_key,
            )
        else:
            broker_response = await account_client.get_deposit(
                cont_yn=cont_yn, next_key=next_key
            )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return _stable_read_failure(
            result_key="cash",
            result_value=None,
            api_id=api_id,
            error="kiwoom_mock_transport_error",
            error_detail=(
                f"kiwoom_mock_get_orderable_cash transport failed: {type(exc).__name__}"
            ),
            extra=extra,
        )

    try:
        validate_mock_response_provenance(broker_response)
    except KiwoomMockEvidenceError as exc:
        return _stable_read_failure(
            result_key="cash",
            result_value=None,
            api_id=api_id,
            error=exc.code,
            error_detail=str(exc),
            broker_response=broker_response,
            extra=extra,
        )

    response = _finalize_broker_response(
        {"source": "kiwoom", "account_mode": ACCOUNT_MODE_KIWOOM_MOCK}, broker_response
    )
    response["provenance"] = build_mock_provenance(api_id)
    response["cash"] = None
    if not response["success"]:
        response["error"] = "kiwoom_mock_broker_error"
        response.update(extra)
        return response

    try:
        cash = normalizer(broker_response)
    except KiwoomMockEvidenceError as exc:
        return _stable_read_failure(
            result_key="cash",
            result_value=None,
            api_id=api_id,
            error=exc.code,
            error_detail=str(exc),
            broker_response=broker_response,
            extra=extra,
        )
    response["cash"] = cash
    response["cash_source"] = base_source
    if symbol is not None:
        response["symbol"] = symbol
    return response


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="kiwoom_mock_preview_order",
        description="Preview a KRX-only Kiwoom mock order without sending.",
    )
    async def kiwoom_mock_preview_order(  # noqa: D401
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: int,
        market: str | None = "kr",
        exchange: str | None = "KRX",
    ) -> dict[str, Any]:
        for guard in (
            _mock_config_error(),
            _symbol_error(symbol),
            _market_error(market),
            _exchange_error(exchange),
            _positive_amount_error("quantity", quantity),
            _positive_amount_error("price", price),
        ):
            if guard:
                return guard
        symbol = normalize_krx_symbol(symbol)
        return await _kiwoom_mock_preview_impl(
            symbol=symbol, side=side, quantity=quantity, price=price
        )

    @mcp.tool(
        name="kiwoom_mock_place_order",
        description="Place a KRX-only Kiwoom mock order. dry_run defaults to True.",
    )
    async def kiwoom_mock_place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: int,
        market: str | None = "kr",
        exchange: str | None = "KRX",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        for guard in (
            _mock_config_error(),
            _symbol_error(symbol),
            _market_error(market),
            _exchange_error(exchange),
            _positive_amount_error("quantity", quantity),
            _positive_amount_error("price", price),
        ):
            if guard:
                return guard
        if not dry_run and not confirm:
            return {
                "success": False,
                "error": "kiwoom_mock_place_order requires confirm=True when dry_run=False.",
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            }
        symbol = normalize_krx_symbol(symbol)
        return await _kiwoom_mock_place_order_impl(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            exchange=exchange,
            dry_run=dry_run,
        )

    @mcp.tool(
        name="kiwoom_mock_cancel_order",
        description="Cancel a Kiwoom mock order by id. dry_run defaults to True.",
    )
    async def kiwoom_mock_cancel_order(
        order_id: str,
        symbol: str | None = None,
        cancel_quantity: int | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return guard
        if (guard := _order_id_error(order_id)) is not None:
            return guard
        if symbol is not None and (guard := _symbol_error(symbol)) is not None:
            return guard
        if (
            guard := _positive_amount_error("cancel_quantity", cancel_quantity)
        ) is not None:
            return guard
        canonical_symbol = None if symbol is None else normalize_krx_symbol(symbol)
        if not dry_run:
            if not confirm:
                return {
                    "success": False,
                    "error": "kiwoom_mock_cancel_order requires confirm=True when dry_run=False.",
                    "source": "kiwoom",
                    "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
                }
            if canonical_symbol is None or cancel_quantity is None:
                return {
                    "success": False,
                    "error": (
                        "kiwoom_mock_cancel_order confirmed execution requires symbol "
                        "and cancel_quantity."
                    ),
                    "source": "kiwoom",
                    "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
                }
            return await _kiwoom_mock_cancel_confirmed_impl(
                order_id=order_id,
                symbol=canonical_symbol,
                cancel_quantity=cancel_quantity,
            )
        return await _kiwoom_mock_cancel_impl(
            order_id=order_id,
            symbol=canonical_symbol,
            cancel_quantity=cancel_quantity,
            dry_run=dry_run,
        )

    @mcp.tool(
        name="kiwoom_mock_modify_order",
        description="Modify a Kiwoom mock order. dry_run defaults to True.",
    )
    async def kiwoom_mock_modify_order(
        order_id: str,
        symbol: str,
        new_price: int | None = None,
        new_quantity: int | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return guard
        if (guard := _order_id_error(order_id)) is not None:
            return guard
        if (guard := _symbol_error(symbol)) is not None:
            return guard
        for guard in (
            _positive_amount_error("new_quantity", new_quantity),
            _positive_amount_error("new_price", new_price),
        ):
            if guard:
                return guard
        symbol = normalize_krx_symbol(symbol)
        if not dry_run:
            if not confirm:
                return {
                    "success": False,
                    "error": "kiwoom_mock_modify_order requires confirm=True when dry_run=False.",
                    "source": "kiwoom",
                    "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
                }
            if new_price is None or new_quantity is None:
                return {
                    "success": False,
                    "error": (
                        "kiwoom_mock_modify_order confirmed execution requires both "
                        "new_price and new_quantity."
                    ),
                    "source": "kiwoom",
                    "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
                }
            return await _kiwoom_mock_modify_confirmed_impl(
                order_id=order_id,
                symbol=symbol,
                new_price=new_price,
                new_quantity=new_quantity,
            )
        return await _kiwoom_mock_modify_impl(
            order_id=order_id,
            symbol=symbol,
            new_price=new_price,
            new_quantity=new_quantity,
            dry_run=dry_run,
        )

    @mcp.tool(
        name="kiwoom_mock_get_order_history",
        description="Read Kiwoom mock order/fill history (read-only).",
    )
    async def kiwoom_mock_get_order_history(
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return _stable_read_failure(
                result_key="orders",
                result_value=[],
                api_id=constants.ACCOUNT_ORDER_STATUS_API_ID,
                error="kiwoom_mock_config_invalid",
                error_detail=str(guard["error"]),
            )
        return await _kiwoom_mock_order_history_impl(cont_yn=cont_yn, next_key=next_key)

    @mcp.tool(
        name="kiwoom_mock_get_positions",
        description="Read Kiwoom mock positions/balance (read-only).",
    )
    async def kiwoom_mock_get_positions() -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            return _stable_read_failure(
                result_key="positions",
                result_value=[],
                api_id=constants.ACCOUNT_BALANCE_API_ID,
                error="kiwoom_mock_config_invalid",
                error_detail=str(guard["error"]),
            )
        return await _kiwoom_mock_positions_impl()

    @mcp.tool(
        name="kiwoom_mock_get_orderable_cash",
        description="Read Kiwoom mock orderable cash (read-only).",
    )
    async def kiwoom_mock_get_orderable_cash(
        symbol: str | None = None,
        side: Literal["buy", "sell"] | None = None,
        price: int | None = None,
    ) -> dict[str, Any]:
        if (guard := _mock_config_error()) is not None:
            if symbol is not None:
                api_id = constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID
                cash_source = "orderable_amount_unavailable"
            else:
                api_id = constants.ACCOUNT_DEPOSIT_API_ID
                cash_source = "deposit_unavailable"
            return _stable_read_failure(
                result_key="cash",
                result_value=None,
                api_id=api_id,
                error="kiwoom_mock_config_invalid",
                error_detail=str(guard["error"]),
                extra={
                    "cash_source": cash_source,
                    **({"symbol": symbol} if symbol is not None else {}),
                },
            )
        if symbol is not None:
            try:
                symbol = normalize_krx_symbol(symbol)
            except ValueError as exc:
                return _stable_read_failure(
                    result_key="cash",
                    result_value=None,
                    api_id=constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID,
                    error="kiwoom_mock_symbol_invalid",
                    error_detail=str(exc),
                    extra={
                        "cash_source": "orderable_amount_unavailable",
                        "symbol": symbol,
                    },
                )
        return await _kiwoom_mock_orderable_cash_impl(
            symbol=symbol, side=side, price=price
        )
