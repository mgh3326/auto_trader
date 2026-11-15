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

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from app.core.config import validate_kiwoom_mock_config
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.orders_kiwoom_shared import (
    _is_explicit_broker_rejection,
)
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
    normalize_order_detail,
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
from app.services.kiwoom_mock_attribution import (
    KiwoomMockAttribution,
    KiwoomMockSignalRecorder,
    MissingAttribution,
)
from app.services.kiwoom_mock_attribution import (
    resolve_attribution as resolve_kiwoom_attribution,
)
from app.services.kiwoom_mock_order_ledger_service import (
    DuplicateOrderIntent,
    KiwoomMockPlaceRecorder,
    SendReservation,
    build_client_order_id,
    kr_trading_day,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ("_derive_broker_success",)

logger = logging.getLogger(__name__)

ACCOUNT_MODE_KIWOOM_MOCK = "kiwoom_mock"

# ROB-904 — kt00010 (주문인출가능금액) is unsupported by mockapi.kiwoom.com
# (return_code=20, RC7006; ROB-891 4-variant probe confirmed this). Cash
# evidence — with or without a symbol — always comes from kt00001
# (예수금상세현황) ord_alow_amt now. The symbol-path cash_source is spelled out
# explicitly so callers can tell the figure is account-level, not order-scoped.
_CASH_SOURCE_DEPOSIT = "deposit"
_CASH_SOURCE_DEPOSIT_FALLBACK = "deposit_fallback_kt00010_unsupported"

KIWOOM_MOCK_TOOL_NAMES: set[str] = {
    "kiwoom_mock_preview_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_modify_order",
    "kiwoom_mock_get_order_history",
    "kiwoom_mock_get_order_detail",
    "kiwoom_mock_get_positions",
    "kiwoom_mock_get_orderable_cash",
}

# ROB-1155 — kt00007 read scope -> official qry_tp. The official values are
# "1:주문순, 2:역순, 3:미체결, 4:체결내역만"; these names keep the MCP surface
# self-describing without leaking Kiwoom enum digits into prompts.
_ORDER_DETAIL_SCOPES: dict[str, str] = {
    "all": constants.ACCOUNT_ORDER_DETAIL_QRY_TP_ORDER_SEQUENCE,
    "all_reverse": constants.ACCOUNT_ORDER_DETAIL_QRY_TP_REVERSE,
    "unfilled": constants.ACCOUNT_ORDER_DETAIL_QRY_TP_UNFILLED,
    "filled": constants.ACCOUNT_ORDER_DETAIL_QRY_TP_FILLED,
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


def _finalize_order_detail_read_response(
    base: dict[str, Any],
    broker_response: dict[str, Any],
    *,
    order_id: str | None,
) -> dict[str, Any]:
    """kt00007-only sibling of :func:`_finalize_normalized_read_response`.

    ROB-1155 — a separate finalizer, not a widening of the shared one. The shared
    finalizer pins ``result_key`` to ``positions``/``orders`` and hardcodes which
    normalizer to call; injecting a callable there would put the already-shipped
    kt00009 / kt00018 read paths on the diff. This duplicates only the
    provenance + error-envelope shape and leaves those paths byte-identical.

    ``order_id`` filtering is local and exact. ``fr_ord_no`` narrows the broker
    query to "this order number and later" (official semantics), so the broker
    response is a superset; without the local filter a single-order lookup would
    silently report unrelated later orders. ``matched_order_count`` is reported
    so a caller can tell "not found" from "found".
    """

    response = _finalize_broker_response(base, broker_response)
    response["provenance"] = build_mock_provenance(
        constants.ACCOUNT_ORDER_DETAIL_API_ID
    )
    response["orders"] = []
    if not response["success"]:
        response["error"] = "kiwoom_mock_broker_error"
        return response
    try:
        validate_mock_response_provenance(broker_response)
        orders = normalize_order_detail(broker_response)
    except KiwoomMockEvidenceError as exc:
        response["success"] = False
        response["error"] = exc.code
        response["error_detail"] = str(exc)
        return response

    response["broker_order_count"] = len(orders)
    if order_id is not None:
        orders = [row for row in orders if row["order_id"] == order_id]
    response["orders"] = orders
    response["matched_order_count"] = len(orders)
    # CP6 evidence: which venue the broker actually recorded each row on, as
    # opposed to the venue we requested. Both are echoed so a mismatch is visible
    # without re-reading the raw broker_response.
    response["response_venues"] = sorted(
        {row["venue"] for row in orders if row["venue"]}
    )
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
        account_client = KiwoomDomesticAccountClient(
            cast(Any, _new_kiwoom_mock_client())
        )
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


def _new_place_recorder() -> KiwoomMockPlaceRecorder:
    """Single chokepoint for the place-path persistence (tests inject a fake)."""
    return KiwoomMockPlaceRecorder(AsyncSessionLocal)


def _new_signal_recorder() -> KiwoomMockSignalRecorder:
    """Single chokepoint for pre-submit attribution (tests inject a fake)."""
    return KiwoomMockSignalRecorder(AsyncSessionLocal)


def _kst_date(trading_day: str) -> str:
    """``YYYYMMDD`` -> ``YYYY-MM-DD`` without a second clock read."""
    return f"{trading_day[:4]}-{trading_day[4:6]}-{trading_day[6:]}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


_SAFE_BROKER_ORDER_ID_MAX_LEN = 32


def _broker_order_id_from(broker_response: dict[str, Any]) -> str | None:
    """The single acknowledged order number, or ``None`` when not provable.

    Both ``ord_no`` and ``order_no`` are read; a missing, non-string, unsafe or
    conflicting value is *not* an acknowledgement.
    """
    found: set[str] = set()
    for key in ("ord_no", "order_no"):
        if key not in broker_response:
            continue
        raw = broker_response.get(key)
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str):
            return None
        candidate = raw.strip()
        if (
            not candidate
            or len(candidate) > _SAFE_BROKER_ORDER_ID_MAX_LEN
            or not _SAFE_ORDER_ID_RE.fullmatch(candidate)
        ):
            return None
        found.add(candidate)
    return next(iter(found)) if len(found) == 1 else None


def _classify_place_response(
    broker_response: dict[str, Any],
) -> tuple[Literal["accepted", "rejected", "uncertain"], str | None]:
    """Three-way outcome of a place answer (design §2.2 item 2).

    return_code == 0 with an order number -> accepted; return_code == 0 without
    one -> uncertain; an explicit non-zero return_code -> rejected. A missing or
    unparseable return_code proves neither, so it is uncertain.
    """
    if _derive_broker_success(broker_response):
        order_id = _broker_order_id_from(broker_response)
        return ("accepted", order_id) if order_id else ("uncertain", None)
    if _is_explicit_broker_rejection(broker_response):
        return "rejected", None
    return "uncertain", None


def _response_code_text(broker_response: dict[str, Any]) -> str | None:
    value = broker_response.get("return_code")
    return None if value is None else str(value)[:64]


def _response_message_text(broker_response: dict[str, Any]) -> str | None:
    value = broker_response.get("return_msg")
    return None if value is None else str(value)[:500]


async def _duplicate_intent_response(
    base: dict[str, Any], client_order_id: str, recorder: KiwoomMockPlaceRecorder
) -> dict[str, Any]:
    """Same key already reserved: refused before any broker call (I2).

    The earlier dispatch may have an unknown outcome (timeout, or a ledger write
    that failed after the ACK), so reconciliation is required unless the ledger
    proves a terminal broker rejection. A failed lookup is not a proof.
    """
    existing_status: str | None = None
    try:
        existing_status = await recorder.existing_status(client_order_id)
    except Exception:  # noqa: BLE001 - an unreadable ledger proves nothing
        logger.warning(
            "kiwoom_mock duplicate-intent ledger lookup failed: key=%s", client_order_id
        )
    return {
        **base,
        "success": False,
        "error": "kiwoom_mock_duplicate_order_intent",
        "status": "not_submitted",
        "dispatch_started": False,
        "client_order_id": client_order_id,
        "existing_ledger_status": existing_status,
        "reconcile_required": existing_status != "rejected",
        "retry_allowed": False,
    }


def _intent_unavailable_response(
    base: dict[str, Any], client_order_id: str, exc: Exception
) -> dict[str, Any]:
    """Reservation could not be committed: fail closed, nothing was sent."""
    return {
        **base,
        "success": False,
        "error": f"kiwoom_mock_send_intent_unavailable: {type(exc).__name__}",
        "status": "not_submitted",
        "dispatch_started": False,
        "client_order_id": client_order_id,
        "reconcile_required": False,
    }


def _attribution_required_response(
    base: dict[str, Any], exc: MissingAttribution
) -> dict[str, Any]:
    """No strategy: refused before the reservation and before any broker call.

    The error text names the reason instead of echoing the exception message,
    which is worded for the kis_mock lane.
    """
    return {
        **base,
        "success": False,
        "error": (
            "kiwoom_mock order is unattributed; refusing to send "
            f"(missing={list(exc.missing)})"
        ),
        "error_code": getattr(exc, "error_code", "attribution_required"),
        "missing_attribution": list(exc.missing),
        "status": "not_submitted",
        "dispatch_started": False,
        "reconcile_required": False,
    }


def _signal_record_unavailable_response(
    base: dict[str, Any], exc: Exception
) -> dict[str, Any]:
    """Attribution could not be committed: fail closed, nothing was sent."""
    return {
        **base,
        "success": False,
        "error": f"kiwoom_mock_signal_record_unavailable: {type(exc).__name__}",
        "error_code": "signal_record_unavailable",
        "status": "not_submitted",
        "dispatch_started": False,
        "reconcile_required": False,
    }


async def _mark_kiwoom_signal_outcome(
    recorder: KiwoomMockSignalRecorder,
    *,
    attribution: KiwoomMockAttribution,
    outcome: Literal["accepted", "rejected", "uncertain"],
    broker_order_id: str | None,
    ledger_id: Any,
) -> None:
    """Close the signal row out after the broker answered; never raises.

    Best-effort by design: the attribution itself is already durable, so a
    failure here degrades the outcome label, never the ownership of the order.
    """
    try:
        await recorder.mark_outcome(
            correlation_id=attribution.correlation_id,
            outcome_state="submitted" if outcome == "accepted" else "failed",
            suppressed_reason=None if outcome == "accepted" else str(outcome),
            detail_patch={
                "order_no": broker_order_id,
                "status": outcome,
                "ledger_id": ledger_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 - must not mask the broker answer
        logger.warning(
            "kiwoom_mock signal outcome update failed correlation_id=%s: %s",
            attribution.correlation_id,
            type(exc).__name__,
        )


async def _mark_kiwoom_signal_not_sent(
    signal_recorder: KiwoomMockSignalRecorder | None,
    attribution: KiwoomMockAttribution | None,
    *,
    reason: str,
    detail_patch: dict[str, Any],
) -> None:
    """Close the signal out as not sent for a provably unsent refusal.

    Only refusals that never handed an order to the broker reach this function;
    a dispatch that may have been transmitted keeps today's labelling. The write
    is a compare-and-set on ``outcome_state='recorded'`` (see
    ``mark_signal_not_sent``), so replaying a correlation id cannot relabel an
    order an earlier attempt already closed.

    Best-effort by design: the attribution is already durable, so a failure
    here degrades the outcome label. It must never change the refusal the
    caller receives.
    """
    if signal_recorder is None or attribution is None:
        return
    try:
        await signal_recorder.mark_not_sent(
            correlation_id=attribution.correlation_id,
            reason=reason,
            detail_patch=detail_patch,
        )
    except Exception as exc:  # noqa: BLE001 - must not mask the refusal
        logger.warning(
            "kiwoom_mock signal not-sent update failed correlation_id=%s reason=%s: %s",
            attribution.correlation_id,
            reason,
            type(exc).__name__,
        )


async def _release_after_pre_dispatch_error(
    recorder: KiwoomMockPlaceRecorder, reservation: SendReservation
) -> bool:
    try:
        await recorder.release(reservation)
    except Exception:  # noqa: BLE001 - a kept reservation only blocks a retry
        logger.warning(
            "kiwoom_mock reservation release failed after pre-dispatch error: key=%s",
            reservation.idempotency_key,
        )
        return False
    return True


async def _record_place_outcome(
    recorder: KiwoomMockPlaceRecorder,
    *,
    outcome: Literal["accepted", "rejected", "uncertain"],
    broker_response: dict[str, Any] | None,
    broker_order_id: str | None,
    ledger_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Write the ledger row AFTER the broker answered; never raises.

    A recording failure must not change what the broker did: the caller keeps
    the broker result and the reservation, and nothing is resent. The failure
    is reported as ``ledger_recording_error``.
    """
    redacted = (
        redact_broker_response(broker_response) if broker_response is not None else None
    )
    try:
        if outcome == "uncertain":
            receipt = await recorder.record_uncertain(
                **ledger_kwargs,
                broker_order_id=broker_order_id,
                response_code=_response_code_text(broker_response or {}),
                response_message=_response_message_text(broker_response or {}),
                raw_response=redacted,
            )
        else:
            receipt = await recorder.record_send(
                status=outcome,
                **ledger_kwargs,
                broker_order_id=broker_order_id,
                response_code=_response_code_text(broker_response or {}),
                response_message=_response_message_text(broker_response or {}),
                raw_response=redacted,
            )
    except Exception as exc:  # noqa: BLE001 - recording must not mask the ACK
        logger.error(
            "kiwoom_mock ledger recording failed after broker answer: key=%s outcome=%s error=%s",
            ledger_kwargs.get("client_order_id"),
            outcome,
            type(exc).__name__,
        )
        return {
            "ledger_recorded": False,
            "ledger_recording_error": type(exc).__name__,
            "reconcile_required": True,
            "retry_allowed": False,
        }
    return {
        "ledger_recorded": True,
        "ledger_id": receipt.ledger_id,
        "ledger_status": receipt.status,
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

    # hk 4723 — pre-submit attribution gate (AGENTS.md hard rule 7 equivalent).
    # Deliberately the FIRST step of the confirmed path: an unattributed mock
    # order must not reach the broker at all, not even the preflight read.
    # Resolution is pure and the signal row is committed before the reservation
    # and the POST, so an order that exists at the broker always has a durable
    # owner. ``dry_run`` is unaffected — like kis_mock's preview, it echoes the
    # order without requiring attribution and writes nothing.
    kiwoom_attribution: KiwoomMockAttribution | None = None
    signal_recorder: KiwoomMockSignalRecorder | None = None
    trading_day = kr_trading_day(_utc_now())
    kst_date = _kst_date(trading_day)
    if not dry_run:
        try:
            kiwoom_attribution = resolve_kiwoom_attribution(
                symbol=symbol,
                side=str(side),
                price=price,
                quantity=quantity,
                strategy=kwargs.get("strategy"),
                kst_trade_day=kst_date,
                rung=kwargs.get("rung"),
            )
        except MissingAttribution as attribution_exc:
            logger.warning(
                "kiwoom_mock order blocked, unattributed: symbol=%s side=%s missing=%s",
                symbol,
                side,
                list(attribution_exc.missing),
            )
            return _attribution_required_response(base_response, attribution_exc)

        signal_recorder = _new_signal_recorder()
        try:
            await signal_recorder.record(
                attribution=kiwoom_attribution,
                symbol=symbol,
                decision="order",
                instrument_type="equity_kr",
                side=str(side),
                intended_quantity=quantity,
                intended_price=price,
                detail={"exchange": str(exchange).strip().upper()},
                kst_date=kst_date,
            )
        except Exception as signal_exc:  # noqa: BLE001 — no record, no send
            logger.error(
                "kiwoom_mock signal record failed, refusing to send: "
                "symbol=%s side=%s correlation_id=%s error=%s",
                symbol,
                side,
                kiwoom_attribution.correlation_id,
                type(signal_exc).__name__,
            )
            return _signal_record_unavailable_response(base_response, signal_exc)
        base_response["correlation_id"] = kiwoom_attribution.correlation_id

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
    except KiwoomPreDispatchError as exc:
        await _mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            reason="preflight_refused",
            detail_patch={
                "status": "not_submitted",
                "stage": exc.stage,
                "cause_type": exc.cause_type,
            },
        )
        return _not_submitted_response(base_response, exc)
    except Exception as exc:  # noqa: BLE001 - preflight must fail closed
        await _mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            reason="preflight_refused",
            detail_patch={
                "status": "not_submitted",
                "error_class": type(exc).__name__,
            },
        )
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
        await _mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            reason="preflight_refused",
            detail_patch={
                "status": "not_submitted",
                "error_code": preflight.error_code,
            },
        )
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

    # hk 4712 — the Kiwoom order body has no client idempotency field, so the
    # pre-send reservation is the only double-send defence. It is committed
    # after preflight and immediately before the POST; a duplicate refuses
    # before any broker call.
    exchange_text = str(exchange).strip().upper()
    client_order_id = build_client_order_id(
        symbol=symbol,
        side=str(side),
        quantity=quantity,
        price=price,
        exchange=exchange_text,
        trading_day=trading_day,
        rung=kwargs.get("rung"),
    )
    base_response["client_order_id"] = client_order_id
    recorder = _new_place_recorder()
    try:
        reservation = await recorder.reserve(
            idempotency_key=client_order_id, symbol=symbol, side=str(side)
        )
    except DuplicateOrderIntent:
        response = await _duplicate_intent_response(
            base_response, client_order_id, recorder
        )
        await _mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            reason="duplicate_intent",
            detail_patch={
                "status": "not_submitted",
                "client_order_id": client_order_id,
                "existing_ledger_status": response.get("existing_ledger_status"),
                "reconcile_required": response.get("reconcile_required"),
            },
        )
        return response
    except Exception as exc:  # noqa: BLE001 - cannot prove the guard: do not send
        response = _intent_unavailable_response(base_response, client_order_id, exc)
        await _mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            reason="send_intent_unavailable",
            detail_patch={
                "status": "not_submitted",
                "client_order_id": client_order_id,
                "error_class": type(exc).__name__,
            },
        )
        return response

    ledger_kwargs: dict[str, Any] = {
        "client_order_id": client_order_id,
        "idempotency_key": client_order_id,
        "symbol": symbol,
        "side": str(side),
        "quantity": quantity,
        "price": price,
        "trading_day": trading_day,
    }

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
        # Provably never sent: no ledger row, and the same key may retry.
        released = await _release_after_pre_dispatch_error(recorder, reservation)
        response = _not_submitted_response(base_response, exc)
        response["reservation_released"] = released
        await _mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            reason="pre_dispatch_error",
            detail_patch={
                "status": "not_submitted",
                "client_order_id": client_order_id,
                "stage": exc.stage,
                "cause_type": exc.cause_type,
                "reservation_released": released,
            },
        )
        return response
    except Exception as exc:  # noqa: BLE001 - post-dispatch: may have been sent
        # I3: uncertain row, reservation kept, never resent.
        response = _dispatch_unknown_response(base_response, exc)
        response.update(
            await _record_place_outcome(
                recorder,
                outcome="uncertain",
                broker_response=None,
                broker_order_id=None,
                ledger_kwargs=ledger_kwargs,
            )
        )
        if signal_recorder is not None and kiwoom_attribution is not None:
            await _mark_kiwoom_signal_outcome(
                signal_recorder,
                attribution=kiwoom_attribution,
                outcome="uncertain",
                broker_order_id=None,
                ledger_id=response.get("ledger_id"),
            )
        return response

    outcome, broker_order_id = _classify_place_response(broker_response)
    response = _finalize_broker_response(base_response, broker_response)
    if outcome == "uncertain":
        # return_code 0 without a provable order number (or an unreadable
        # return_code): the order may exist but cannot be tracked, so it is not
        # a confirmed placement and must not be retried.
        response["success"] = False
        response["status"] = "acceptance_uncertain"
        response["reconcile_required"] = True
        response["retry_allowed"] = False
    response.update(
        await _record_place_outcome(
            recorder,
            outcome=outcome,
            broker_response=broker_response,
            broker_order_id=broker_order_id,
            ledger_kwargs=ledger_kwargs,
        )
    )
    if signal_recorder is not None and kiwoom_attribution is not None:
        await _mark_kiwoom_signal_outcome(
            signal_recorder,
            attribution=kiwoom_attribution,
            outcome=outcome,
            broker_order_id=broker_order_id,
            ledger_id=response.get("ledger_id"),
        )
    return response


async def _kiwoom_mock_preview_impl(**kwargs: Any) -> dict[str, Any]:
    symbol = str(kwargs.get("symbol") or "").strip()
    side = kwargs.get("side")
    quantity = int(kwargs.get("quantity"))
    price = int(kwargs.get("price"))

    base_response = {
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "preview": True,
    }
    try:
        preflight = await _run_preflight_for_kiwoom_mock(
            symbol=symbol, side=side, quantity=quantity, price=price
        )
    except KiwoomPreDispatchError as exc:
        return _not_submitted_response(base_response, exc)
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


async def _kiwoom_mock_order_detail_impl(**kwargs: Any) -> dict[str, Any]:
    """kt00007 read-only order-detail lookup (ROB-1155).

    Read-only by construction: builds ONLY a ``KiwoomDomesticAccountClient`` and
    never a ``KiwoomDomesticOrderClient``, so no place/modify/cancel code path is
    reachable from here.
    """

    order_id_raw = kwargs.get("order_id")
    order_id = None if order_id_raw is None else str(order_id_raw).strip()
    scope = str(kwargs.get("scope") or "all")
    venue = str(kwargs.get("venue") or constants.ACCOUNT_READ_VENUE_KRX).strip().upper()
    order_date = kwargs.get("order_date")
    symbol_raw = kwargs.get("symbol")
    symbol = None if symbol_raw is None else normalize_krx_symbol(symbol_raw)
    qry_tp = _ORDER_DETAIL_SCOPES[scope]

    extra: dict[str, Any] = {
        "order_id": order_id,
        "requested_scope": scope,
        "requested_qry_tp": qry_tp,
        "requested_venue": venue,
        "order_date": None if order_date is None else str(order_date).strip(),
        **({"symbol": symbol} if symbol is not None else {}),
    }

    try:
        client = KiwoomMockClient.from_app_settings()
        account_client = KiwoomDomesticAccountClient(cast(Any, client))
        broker_response = await account_client.get_order_detail(
            order_date=order_date,
            qry_tp=qry_tp,
            symbol=symbol,
            from_order_no=order_id,
            dmst_stex_tp=venue,
        )
    except ValueError as exc:
        # Locally raised official-contract validation (venue/qry_tp/date/order
        # number shape). Never dispatched, so it is not a transport failure.
        return _stable_read_failure(
            result_key="orders",
            result_value=[],
            api_id=constants.ACCOUNT_ORDER_DETAIL_API_ID,
            error="kiwoom_mock_request_invalid",
            error_detail=str(exc),
            extra=extra,
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return _stable_read_failure(
            result_key="orders",
            result_value=[],
            api_id=constants.ACCOUNT_ORDER_DETAIL_API_ID,
            error="kiwoom_mock_transport_error",
            error_detail=(
                f"kiwoom_mock_get_order_detail transport failed: {type(exc).__name__}"
            ),
            extra=extra,
        )
    return _finalize_order_detail_read_response(
        {
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            **extra,
        },
        broker_response,
        order_id=order_id,
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
    cont_yn = kwargs.get("cont_yn")
    next_key = kwargs.get("next_key")

    # ROB-904 — kt00010 is mock-unsupported (RC7006), so the symbol path no
    # longer dispatches to it. Both branches call kt00001 (get_deposit); side
    # and price are accepted (kwargs) for call-site backcompat only and do not
    # affect dispatch or the cash value, which is account-level either way.
    base_source = (
        _CASH_SOURCE_DEPOSIT_FALLBACK if symbol is not None else _CASH_SOURCE_DEPOSIT
    )
    api_id = constants.ACCOUNT_DEPOSIT_API_ID

    extra: dict[str, Any] = {
        "cash_source": f"{base_source}_unavailable",
    }
    if symbol is not None:
        extra["symbol"] = symbol

    try:
        client = KiwoomMockClient.from_app_settings()
        account_client = KiwoomDomesticAccountClient(cast(Any, client))
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
        cash = normalize_deposit(broker_response)
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
        description=(
            "Place a KRX-only Kiwoom mock order. dry_run defaults to True. "
            "A confirmed order requires a non-blank strategy: the attribution "
            "is committed to the signal ledger before the send reservation."
        ),
    )
    async def kiwoom_mock_place_order(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: int,
        market: str | None = "kr",
        exchange: str | None = "KRX",
        strategy: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        rung: str | int | None = None,
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
            strategy=strategy,
            dry_run=dry_run,
            rung=rung,
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
        name="kiwoom_mock_get_order_detail",
        description=(
            "Read Kiwoom mock order/fill DETAIL via kt00007 (read-only). Tracks a "
            "single known order number, or scopes to unfilled/filled rows. "
            "venue='NXT' is an observation-only argument; order placement stays "
            "KRX-pinned."
        ),
    )
    async def kiwoom_mock_get_order_detail(
        order_id: str | None = None,
        order_date: str | None = None,
        scope: Literal["all", "all_reverse", "unfilled", "filled"] = "all",
        symbol: str | None = None,
        venue: Literal["KRX", "NXT"] = "KRX",
    ) -> dict[str, Any]:
        api_id = constants.ACCOUNT_ORDER_DETAIL_API_ID
        extra: dict[str, Any] = {
            "order_id": order_id,
            "requested_scope": scope,
            "requested_venue": venue,
        }
        if (guard := _mock_config_error()) is not None:
            return _stable_read_failure(
                result_key="orders",
                result_value=[],
                api_id=api_id,
                error="kiwoom_mock_config_invalid",
                error_detail=str(guard["error"]),
                extra=extra,
            )
        if order_id is not None and (guard := _order_id_error(order_id)) is not None:
            return _stable_read_failure(
                result_key="orders",
                result_value=[],
                api_id=api_id,
                error="kiwoom_mock_order_id_invalid",
                error_detail=str(guard["error"]),
                extra=extra,
            )
        if symbol is not None and (guard := _symbol_error(symbol)) is not None:
            return _stable_read_failure(
                result_key="orders",
                result_value=[],
                api_id=api_id,
                error="kiwoom_mock_symbol_invalid",
                error_detail=str(guard["error"]),
                extra={**extra, "symbol": symbol},
            )
        # Read-only venue allowlist, deliberately NOT _exchange_error: that guard
        # enforces the KRX-only ORDER boundary and must stay untouched (ROB-1155).
        if str(venue).strip().upper() not in constants.ACCOUNT_READ_VENUE_ALLOWLIST:
            return _stable_read_failure(
                result_key="orders",
                result_value=[],
                api_id=api_id,
                error="kiwoom_mock_read_venue_invalid",
                error_detail=(
                    "kiwoom_mock_get_order_detail venue must be one of "
                    f"{sorted(constants.ACCOUNT_READ_VENUE_ALLOWLIST)}; got {venue!r}"
                ),
                extra=extra,
            )
        if scope not in _ORDER_DETAIL_SCOPES:
            return _stable_read_failure(
                result_key="orders",
                result_value=[],
                api_id=api_id,
                error="kiwoom_mock_read_scope_invalid",
                error_detail=(
                    "kiwoom_mock_get_order_detail scope must be one of "
                    f"{sorted(_ORDER_DETAIL_SCOPES)}; got {scope!r}"
                ),
                extra=extra,
            )
        return await _kiwoom_mock_order_detail_impl(
            order_id=order_id,
            order_date=order_date,
            scope=scope,
            symbol=symbol,
            venue=venue,
        )

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
        # ROB-904 — cash evidence always resolves via kt00001 now (kt00010 is
        # mock-unsupported); symbol path is a fallback, not a distinct API.
        api_id = constants.ACCOUNT_DEPOSIT_API_ID
        base_source = (
            _CASH_SOURCE_DEPOSIT_FALLBACK
            if symbol is not None
            else _CASH_SOURCE_DEPOSIT
        )
        if (guard := _mock_config_error()) is not None:
            return _stable_read_failure(
                result_key="cash",
                result_value=None,
                api_id=api_id,
                error="kiwoom_mock_config_invalid",
                error_detail=str(guard["error"]),
                extra={
                    "cash_source": f"{base_source}_unavailable",
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
                    api_id=api_id,
                    error="kiwoom_mock_symbol_invalid",
                    error_detail=str(exc),
                    extra={
                        "cash_source": f"{base_source}_unavailable",
                        "symbol": symbol,
                    },
                )
        return await _kiwoom_mock_orderable_cash_impl(
            symbol=symbol, side=side, price=price
        )
