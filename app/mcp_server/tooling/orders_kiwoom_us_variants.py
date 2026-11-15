"""Evidence-gated Kiwoom mock US account and order MCP tools (ROB-867)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from app.mcp_server.tooling.orders_kiwoom_shared import (
    finalize_broker_response,
    finalize_place_broker_response,
)
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.client import KiwoomPreDispatchError
from app.services.brokers.kiwoom.normalization import (
    KiwoomMockEvidenceError,
    build_mock_provenance,
    validate_mock_response_provenance,
)
from app.services.brokers.kiwoom.us_account import (
    KiwoomUsAccountClient,
    extract_usd_deposit,
)
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient
from app.services.brokers.kiwoom.us_orders import (
    KiwoomUsOrderClient,
    KiwoomUsOrderRejected,
    build_us_place_order_body,
    validate_us_order_id,
)
from app.services.kiwoom_mock_place import (
    KiwoomMockUsPlaceDeps,
    UsPlacePreflight,
    new_signal_recorder,
    new_us_place_recorder,
    place_kiwoom_mock_us_order,
)
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

if TYPE_CHECKING:
    from fastmcp import FastMCP

ACCOUNT_MODE_KIWOOM_MOCK_US = "kiwoom_mock_us"
SUPPORTED_MCP_TRDE_TYPES = ("00", "03")
_US_ALLOWED_PROVENANCE_MODES = frozenset({ACCOUNT_MODE_KIWOOM_MOCK_US})

KIWOOM_MOCK_US_READ_TOOL_NAMES = {
    "kiwoom_mock_us_get_order_history",
    "kiwoom_mock_us_get_positions",
    "kiwoom_mock_us_get_orderable_cash",
}
KIWOOM_MOCK_US_MUTATION_TOOL_NAMES = {
    "kiwoom_mock_us_preview_order",
    "kiwoom_mock_us_place_order",
    "kiwoom_mock_us_modify_order",
    "kiwoom_mock_us_cancel_order",
}
KIWOOM_MOCK_US_TOOL_NAMES = (
    KIWOOM_MOCK_US_READ_TOOL_NAMES | KIWOOM_MOCK_US_MUTATION_TOOL_NAMES
)


class _TrustedValidationError(ValueError):
    """Locally generated, operator-actionable validation failure."""


def _mock_us_config_error() -> dict[str, Any] | None:
    # One definition of the gate text (the proposals adapter shows the same).
    # Imported here, not bound at module import: the gate must read whatever the
    # defining module currently holds, so there is no second copy of its text.
    from app.services.kiwoom_mock_place import us_config_error

    return us_config_error()


# ---------------------------------------------------------------------------
# Place-path seams (hk 4754). The sequence itself lives in
# ``app.services.kiwoom_mock_place`` so the order_proposals submit adapter runs
# the same attribution → reservation → POST → ledger boundary. These names are
# the monkeypatch points tests already use (client, exchange lookup) plus the
# recorder/clock seams the KR tool has.
# ---------------------------------------------------------------------------


def _new_us_place_recorder() -> Any:
    return new_us_place_recorder()


def _new_signal_recorder() -> Any:
    return new_signal_recorder()


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _us_preflight(
    symbol: str,
    side: str,
    quantity: int,
    price: object | None,
    trde_tp: str,
) -> UsPlacePreflight:
    """Resolve this module's exchange lookup and validate the order shape."""
    try:
        stex_tp = await _resolve_stex(symbol)
        build_us_place_order_body(
            side=side,
            symbol=symbol,
            stex_tp=stex_tp,
            quantity=quantity,
            trde_tp=trde_tp,
            price=price,
        )
    except Exception as exc:  # noqa: BLE001 - a shape refusal, never a send
        return UsPlacePreflight(
            ok=False,
            error_code="place_order_validation_failed",
            error_detail=f"kiwoom_mock_us_place_order failed: {type(exc).__name__}",
        )
    return UsPlacePreflight(ok=True, stex_tp=stex_tp)


def _us_finalize_for(side: str):
    api_id = (
        constants.US_ORDER_BUY_API_ID
        if str(side).strip().lower() == "buy"
        else constants.US_ORDER_SELL_API_ID
    )

    def finalize(
        base: dict[str, Any], broker_response: dict[str, Any]
    ) -> dict[str, Any]:
        return _finalize_us(base, broker_response, api_id=api_id, tracked_order=True)

    return finalize


def _trade_type_error(trde_tp: str) -> dict[str, Any] | None:
    if trde_tp in SUPPORTED_MCP_TRDE_TYPES:
        return None
    return {
        "success": False,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        "error_code": "unsupported_trde_tp",
        "rejected_trde_tp": trde_tp,
        "supported_trde_tp": list(SUPPORTED_MCP_TRDE_TYPES),
        "error": f"kiwoom_mock_us does not expose trde_tp={trde_tp!r}.",
    }


def _price_error(trde_tp: str, price: float | None) -> dict[str, Any] | None:
    if trde_tp == "00" and (price is None or not math.isfinite(price) or price <= 0):
        return {
            "success": False,
            "error": "trde_tp='00' requires price > 0.",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        }
    if trde_tp == "03" and price is not None:
        return {
            "success": False,
            "error": "trde_tp='03' must omit price.",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        }
    return None


def _basic_order_error(market: str, quantity: int) -> dict[str, Any] | None:
    if market.strip().lower() == "us" and quantity > 0:
        return None
    return {
        "success": False,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        "error": "kiwoom_mock_us requires market='us' and quantity > 0.",
    }


async def _resolve_stex(symbol: str) -> str:
    exchange = str(await get_us_exchange_by_symbol(symbol)).strip().upper()
    try:
        return constants.US_EXCHANGE_TO_STEX[exchange]
    except KeyError as exc:
        raise _TrustedValidationError(
            f"Kiwoom US mock rejects unsupported exchange={exchange!r}"
        ) from exc


def _exception_response(operation: str, exc: Exception) -> dict[str, Any]:
    return {
        "success": False,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        # Exception text is provider-controlled and can contain request details
        # that key-based broker-response redaction cannot inspect.
        "error": f"kiwoom_mock_us_{operation} failed: {type(exc).__name__}",
    }


def _validation_response(operation: str, exc: Exception) -> dict[str, Any]:
    return {
        "success": False,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        "error": str(exc),
        "error_code": f"{operation}_validation_failed",
    }


def _not_submitted_response(operation: str, exc: Exception) -> dict[str, Any]:
    return {
        **_exception_response(operation, exc),
        "status": "not_submitted",
        "reconcile_required": False,
        "retry_allowed": False,
    }


def _finalize_us(
    base: dict[str, Any],
    broker_response: dict[str, Any],
    *,
    api_id: str,
    tracked_order: bool = False,
) -> dict[str, Any]:
    """Anti-spoof provenance check + broker-evidence success + mock stamp."""

    try:
        validate_mock_response_provenance(
            broker_response, allowed_account_modes=_US_ALLOWED_PROVENANCE_MODES
        )
    except KiwoomMockEvidenceError as exc:
        response: dict[str, Any] = {
            "success": False,
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            "error_code": exc.code,
            "error": str(exc),
        }
        if tracked_order:
            response.update(
                {
                    "status": "acceptance_uncertain",
                    "reconcile_required": True,
                    "retry_allowed": False,
                }
            )
        return response
    finalizer = (
        finalize_place_broker_response if tracked_order else finalize_broker_response
    )
    response = finalizer(base, broker_response)
    response["provenance"] = build_mock_provenance(
        api_id, account_mode=ACCOUNT_MODE_KIWOOM_MOCK_US
    )
    return response


def register(mcp: FastMCP) -> None:
    # All seven tools registered in one MCP process share the transport's
    # concurrency-safe OAuth cache.  Creating a client per pagination/poll call
    # would request a fresh token each time and could strand cleanup mid-flight.
    shared_client: KiwoomMockUsClient | None = None

    def get_client() -> KiwoomMockUsClient:
        nonlocal shared_client
        if shared_client is None:
            shared_client = KiwoomMockUsClient.from_app_settings()
        return shared_client

    @mcp.tool(
        name="kiwoom_mock_us_preview_order",
        description="Preview a Kiwoom US mock order; MCP supports trde_tp 00/03.",
    )
    async def preview(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: float | None = None,
        trde_tp: str = "00",
        market: str = "us",
    ) -> dict[str, Any]:
        for guard in (
            _mock_us_config_error(),
            _trade_type_error(trde_tp),
            _price_error(trde_tp, price),
            _basic_order_error(market, quantity),
        ):
            if guard:
                return guard
        try:
            stex_tp = await _resolve_stex(symbol)
            body = build_us_place_order_body(
                side=side,
                symbol=symbol,
                stex_tp=stex_tp,
                quantity=quantity,
                trde_tp=trde_tp,
                price=price,
            )
        except (KiwoomUsOrderRejected, _TrustedValidationError) as exc:
            return _validation_response("preview_order", exc)
        except Exception as exc:  # noqa: BLE001 - stable MCP error envelope
            return _exception_response("preview_order", exc)
        requested_notional = (
            Decimal(str(price)) * quantity if price is not None else None
        )
        return {
            "success": True,
            "preview": True,
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            "symbol": symbol.strip().upper(),
            "side": side,
            "quantity": quantity,
            "price": price,
            "requested_notional": requested_notional,
            "trde_tp": trde_tp,
            "stex_tp": stex_tp,
            "request_body": body,
        }

    @mcp.tool(
        name="kiwoom_mock_us_place_order",
        description=(
            "Place a Kiwoom US mock order; dry_run defaults to true. A confirmed "
            "order requires a non-blank strategy: the attribution is committed "
            "to the signal ledger before the send reservation."
        ),
    )
    async def place(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: float | None = None,
        trde_tp: str = "00",
        market: str = "us",
        strategy: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        rung: str | int | None = None,
    ) -> dict[str, Any]:
        preview_result = await preview(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            trde_tp=trde_tp,
            market=market,
        )
        if not preview_result.get("success") or dry_run:
            return {**preview_result, "dry_run": dry_run}
        if not confirm:
            return {
                "success": False,
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
                "dry_run": False,
                "error": (
                    "kiwoom_mock_us_place_order requires confirm=True "
                    "when dry_run=False."
                ),
            }
        # hk 4754: the confirmed sequence (attribution signal → preflight →
        # reservation → POST → ledger row → outcome mark) is the shared
        # service-layer pipeline, so the order_proposals submit adapter places
        # through the same boundary. This tool keeps the seams (client,
        # exchange lookup, recorders, clock) and the response envelope.
        deps = KiwoomMockUsPlaceDeps(
            new_client=get_client,
            order_client_cls=KiwoomUsOrderClient,
            preflight=_us_preflight,
            place_recorder=_new_us_place_recorder,
            signal_recorder=_new_signal_recorder,
            utc_now=_utc_now,
            finalize=_us_finalize_for(side),
        )
        return await place_kiwoom_mock_us_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            trde_tp=trde_tp,
            market=market,
            dry_run=False,
            strategy=strategy,
            rung=rung,
            deps=deps,
        )

    @mcp.tool(
        name="kiwoom_mock_us_modify_order",
        description="Modify a Kiwoom US mock order price; dry_run defaults to true.",
    )
    async def modify(
        order_id: str,
        symbol: str,
        new_price: float,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            validate_us_order_id(order_id)
        except KiwoomUsOrderRejected as exc:
            return _validation_response("modify_order", exc)
        if not math.isfinite(new_price) or new_price <= 0:
            return _validation_response(
                "modify_order", ValueError("new_price must be a finite value > 0")
            )
        try:
            stex_tp = await _resolve_stex(symbol)
        except _TrustedValidationError as exc:
            return _validation_response("modify_order", exc)
        except Exception as exc:  # noqa: BLE001 - stable MCP error envelope
            return _exception_response("modify_order", exc)
        base = {
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            "dry_run": dry_run,
            "order_id": order_id,
            "symbol": symbol.strip().upper(),
            "new_price": new_price,
            "stex_tp": stex_tp,
        }
        if dry_run:
            return {"success": True, **base}
        if not confirm:
            return {
                "success": False,
                **base,
                "error": (
                    "kiwoom_mock_us_modify_order requires confirm=True "
                    "when dry_run=False."
                ),
            }
        try:
            client = get_client()
            orders = KiwoomUsOrderClient(cast(Any, client))
        except Exception as exc:  # noqa: BLE001 - no broker dispatch occurred
            return _not_submitted_response("modify_order", exc)
        try:
            raw = await orders.modify_order(
                original_order_no=order_id,
                symbol=symbol,
                stex_tp=stex_tp,
                new_price=new_price,
            )
        except KiwoomPreDispatchError as exc:
            return _not_submitted_response("modify_order", exc)
        except Exception as exc:  # noqa: BLE001 - stable MCP error envelope
            return {
                **_exception_response("modify_order", exc),
                "status": "acceptance_uncertain",
                "reconcile_required": True,
                "retry_allowed": False,
            }
        return _finalize_us(
            base,
            raw,
            api_id=constants.US_ORDER_MODIFY_API_ID,
            tracked_order=True,
        )

    @mcp.tool(
        name="kiwoom_mock_us_cancel_order",
        description="Cancel a Kiwoom US mock order; dry_run defaults to true.",
    )
    async def cancel(
        order_id: str,
        symbol: str,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            validate_us_order_id(order_id)
            stex_tp = await _resolve_stex(symbol)
        except (KiwoomUsOrderRejected, _TrustedValidationError) as exc:
            return _validation_response("cancel_order", exc)
        except Exception as exc:  # noqa: BLE001 - stable MCP error envelope
            return _exception_response("cancel_order", exc)
        base = {
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            "dry_run": dry_run,
            "order_id": order_id,
            "symbol": symbol.strip().upper(),
            "stex_tp": stex_tp,
        }
        if dry_run:
            return {"success": True, **base}
        if not confirm:
            return {
                "success": False,
                **base,
                "error": (
                    "kiwoom_mock_us_cancel_order requires confirm=True "
                    "when dry_run=False."
                ),
            }
        try:
            client = get_client()
            raw = await KiwoomUsOrderClient(cast(Any, client)).cancel_order(
                original_order_no=order_id,
                symbol=symbol,
                stex_tp=stex_tp,
            )
        except Exception as exc:  # noqa: BLE001 - stable MCP error envelope
            return _exception_response("cancel_order", exc)
        return _finalize_us(base, raw, api_id=constants.US_ORDER_CANCEL_API_ID)

    @mcp.tool(
        name="kiwoom_mock_us_get_order_history",
        description="Read Kiwoom US mock open or today's orders.",
    )
    async def history(
        scope: Literal["open", "today"] = "open",
        symbol: str | None = None,
        side_code: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        if scope not in {"open", "today"}:
            return _validation_response(
                "get_order_history", ValueError("scope must be 'open' or 'today'")
            )
        try:
            stex_tp = await _resolve_stex(symbol) if symbol else None
            client = get_client()
            account = KiwoomUsAccountClient(cast(Any, client))
            method = (
                account.get_open_orders if scope == "open" else account.get_today_orders
            )
            raw = await method(
                side_code=side_code,
                stex_tp=stex_tp,
                symbol=symbol,
                cont_yn=cont_yn,
                next_key=next_key,
            )
        except Exception as exc:  # noqa: BLE001 - stable MCP error envelope
            return _exception_response("get_order_history", exc)
        return _finalize_us(
            {
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
                "scope": scope,
            },
            raw,
            api_id=(
                constants.US_ACCOUNT_OPEN_ORDERS_API_ID
                if scope == "open"
                else constants.US_ACCOUNT_TODAY_ORDERS_API_ID
            ),
        )

    @mcp.tool(
        name="kiwoom_mock_us_get_positions",
        description="Read Kiwoom US mock positions.",
    )
    async def positions(
        symbol: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            stex_tp = await _resolve_stex(symbol) if symbol else None
            client = get_client()
            raw = await KiwoomUsAccountClient(cast(Any, client)).get_positions(
                stex_tp=stex_tp,
                symbol=symbol,
                cont_yn=cont_yn,
                next_key=next_key,
            )
        except Exception as exc:  # noqa: BLE001 - stable MCP error envelope
            return _exception_response("get_positions", exc)
        return _finalize_us(
            {
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            },
            raw,
            api_id=constants.US_ACCOUNT_POSITIONS_API_ID,
        )

    @mcp.tool(
        name="kiwoom_mock_us_get_orderable_cash",
        description="Read USD deposit evidence; not broker orderable quantity.",
    )
    async def cash() -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            client = get_client()
            raw = await KiwoomUsAccountClient(cast(Any, client)).get_us_deposit_detail()
        except Exception as exc:  # noqa: BLE001 - stable MCP error envelope
            return _exception_response("get_orderable_cash", exc)
        result = _finalize_us(
            {
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            },
            raw,
            api_id=constants.US_ACCOUNT_DEPOSIT_DETAIL_API_ID,
        )
        if result.get("error_code") == "kiwoom_mock_provenance_conflict":
            return result
        value = extract_usd_deposit(raw)
        result.update(
            {
                "cash": value,
                "currency": "USD",
                "cash_source": (
                    "ust21160.d0_usd_fx_entr"
                    if value is not None
                    else "ust21160.d0_usd_fx_entr_unparsed"
                ),
                "cash_semantics": "deposit_not_broker_orderable",
                "orderable_quantity_supported": False,
                "warning": (
                    "Kiwoom mock rejects ust31490; cash is USD deposit "
                    "evidence, not per-symbol broker orderable cash."
                ),
            }
        )
        return result
