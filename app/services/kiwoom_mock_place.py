# app/services/kiwoom_mock_place.py
"""KR Kiwoom mock place pipeline — one boundary for MCP and proposals (hk 4732).

The confirmed ``kiwoom_mock`` place sequence (pre-submit attribution signal →
preflight → send reservation → broker POST → ledger row → outcome mark) used to
live inside the MCP tooling module. It is a service-layer pipeline now so the
``order_proposals`` submit adapter and the ``kiwoom_mock_*`` MCP tool run the
**same** sequence: an order that exists at the broker always has a committed
attribution row, a committed send reservation and a ledger row, whichever
surface placed it.

The MCP tool keeps its own thin seams (client/preflight/recorder/clock) and
passes them through :class:`KiwoomMockPlaceDeps`, so every monkeypatch point and
every response byte stays what it was; the proposal adapter uses
:func:`default_deps`, which resolves the same real implementations from this
module (module-level names, so tests can patch this module instead).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

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
from app.services.brokers.kiwoom.normalization import redact_broker_response
from app.services.brokers.kiwoom.order_preflight import (
    PreflightResult,
    run_order_preflight,
)
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
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

ACCOUNT_MODE_KIWOOM_MOCK = "kiwoom_mock"

_SAFE_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SAFE_BROKER_ORDER_ID_MAX_LEN = 32


@dataclass(frozen=True, slots=True)
class KiwoomMockPlaceDeps:
    """The seam set both callers supply; tests replace any of them."""

    new_client: Callable[[], Any]
    account_client_cls: Any
    order_client_cls: Any
    preflight: Callable[..., Awaitable[PreflightResult]]
    fetch_quote: Callable[[str], Awaitable[tuple[int | None, str]]]
    place_recorder: Callable[[], Any]
    signal_recorder: Callable[[], Any]
    utc_now: Callable[[], datetime]


def mock_config_error() -> dict[str, Any] | None:
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


def new_kiwoom_mock_client() -> KiwoomMockClient:
    """Request-scoped KiwoomMockClient factory.

    Single chokepoint for client construction so tests can count/inject.
    Each place/preview flow builds exactly one client and reuses it across
    preflight + POST to share one auth client + one cold token.
    """
    return KiwoomMockClient.from_app_settings()


async def fetch_kr_quote_for_preflight(symbol: str) -> tuple[int | None, str]:
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


async def run_kiwoom_mock_preflight(
    symbol: str,
    side: str,
    quantity: int,
    price: int,
    *,
    account_client: Any | None = None,
    fetch_quote: Callable[[str], Awaitable[tuple[int | None, str]]] | None = None,
) -> PreflightResult:
    quote_price, quote_freshness = await (fetch_quote or fetch_kr_quote_for_preflight)(
        symbol
    )
    if account_client is None:
        account_client = KiwoomDomesticAccountClient(new_kiwoom_mock_client())
    return await run_order_preflight(
        account_client=account_client,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        quote_price=quote_price,
        quote_freshness=quote_freshness,
    )


def new_place_recorder() -> KiwoomMockPlaceRecorder:
    """Single chokepoint for the place-path persistence (tests inject a fake)."""
    return KiwoomMockPlaceRecorder(AsyncSessionLocal)


def new_signal_recorder() -> KiwoomMockSignalRecorder:
    """Single chokepoint for pre-submit attribution (tests inject a fake)."""
    return KiwoomMockSignalRecorder(AsyncSessionLocal)


def utc_now() -> datetime:
    return datetime.now(UTC)


def default_deps() -> KiwoomMockPlaceDeps:
    """The real production seam set, resolved from this module's globals."""

    return KiwoomMockPlaceDeps(
        new_client=new_kiwoom_mock_client,
        account_client_cls=KiwoomDomesticAccountClient,
        order_client_cls=KiwoomDomesticOrderClient,
        preflight=run_kiwoom_mock_preflight,
        fetch_quote=fetch_kr_quote_for_preflight,
        place_recorder=new_place_recorder,
        signal_recorder=new_signal_recorder,
        utc_now=utc_now,
    )


def preflight_to_response(
    result: PreflightResult,
    *,
    symbol: str,
    side: str,
    quantity: int,
    price: int,
    preview: bool,
    not_sent: bool = True,
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
        # hk 4732 r1/r2: a refused preflight is a read-only account/quote check
        # that never reached the order TR, so say so in the closed not-sent
        # vocabulary. Only for an identity this call owns (``not_sent``): on a
        # replayed signal the earlier attempt under the same correlation id may
        # have placed a live order, and claiming "not submitted, no reconcile"
        # would terminalize a rung whose order is still at the broker.
        if not_sent:
            response["status"] = "not_submitted"
            response["dispatch_started"] = False
            response["reconcile_required"] = False
    response.update(result.to_response_extras())
    return response


def not_submitted_response(
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


def dispatch_unknown_response(base: dict[str, Any], exc: Exception) -> dict[str, Any]:
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


def kst_date(trading_day: str) -> str:
    """``YYYYMMDD`` -> ``YYYY-MM-DD`` without a second clock read."""
    return f"{trading_day[:4]}-{trading_day[4:6]}-{trading_day[6:]}"


def broker_order_id_from(broker_response: dict[str, Any]) -> str | None:
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


def classify_place_response(
    broker_response: dict[str, Any],
) -> tuple[Literal["accepted", "rejected", "uncertain"], str | None]:
    """Three-way outcome of a place answer (design §2.2 item 2).

    return_code == 0 with an order number -> accepted; return_code == 0 without
    one -> uncertain; an explicit non-zero return_code -> rejected. A missing or
    unparseable return_code proves neither, so it is uncertain.
    """
    if _derive_broker_success(broker_response):
        order_id = broker_order_id_from(broker_response)
        return ("accepted", order_id) if order_id else ("uncertain", None)
    if _is_explicit_broker_rejection(broker_response):
        return "rejected", None
    return "uncertain", None


def response_code_text(broker_response: dict[str, Any]) -> str | None:
    value = broker_response.get("return_code")
    return None if value is None else str(value)[:64]


def response_message_text(broker_response: dict[str, Any]) -> str | None:
    value = broker_response.get("return_msg")
    return None if value is None else str(value)[:500]


async def duplicate_intent_response(
    base: dict[str, Any], client_order_id: str, recorder: Any
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


def intent_unavailable_response(
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


def attribution_required_response(
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


def signal_record_unavailable_response(
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


async def mark_kiwoom_signal_outcome(
    recorder: Any,
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


async def mark_kiwoom_signal_not_sent(
    signal_recorder: Any | None,
    attribution: KiwoomMockAttribution | None,
    *,
    created: bool,
    reason: str,
    detail_patch: dict[str, Any],
) -> None:
    """Close the signal out as not sent for a provably unsent refusal.

    Two conditions gate the write, and both are needed:

    * ``created`` - the signal row was inserted by *this* call. A replayed row
      is owned by an earlier attempt whose ``recorded`` state means "outcome
      unknown"; that attempt may have placed the order (an accepted ACK whose
      post-send mark failed, or a send still in flight), so relabelling it
      would claim "never sent" on a path that may have sent.
    * the SQL compare-and-set on ``outcome_state='recorded'`` (see
      ``mark_signal_not_sent``), so an already-closed row is never rewritten.

    Only refusals that never handed an order to the broker reach this function;
    a dispatch that may have been transmitted keeps the ordinary labelling.

    Best-effort by design: the attribution is already durable, so a failure
    here degrades the outcome label. It must never change the refusal the
    caller receives.
    """
    if not created or signal_recorder is None or attribution is None:
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


async def release_after_pre_dispatch_error(
    recorder: Any, reservation: SendReservation
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


async def record_place_outcome(
    recorder: Any,
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
                response_code=response_code_text(broker_response or {}),
                response_message=response_message_text(broker_response or {}),
                raw_response=redacted,
            )
        else:
            receipt = await recorder.record_send(
                status=outcome,
                **ledger_kwargs,
                broker_order_id=broker_order_id,
                response_code=response_code_text(broker_response or {}),
                response_message=response_message_text(broker_response or {}),
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


async def place_kiwoom_mock_order(
    *,
    symbol: Any,
    side: Any,
    quantity: Any,
    price: Any,
    exchange: Any = None,
    dry_run: bool = True,
    strategy: str | None = None,
    rung: int | str | None = None,
    correlation_id: str | None = None,
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
    deps: KiwoomMockPlaceDeps | None = None,
) -> dict[str, Any]:
    """Place one KRX-only Kiwoom mock order (the single shared sequence).

    ``dry_run`` only previews: no config gate outcome is written, no
    attribution is required, and nothing reaches the reservation or the broker.
    A confirmed call commits the signal row, then the send reservation, then the
    POST, then the ledger row, then the signal outcome — in that order.

    ``pre_send_hook`` runs immediately before the broker POST (after the
    reservation). If it raises, the request was provably not sent, so the
    reservation is released and the exception propagates to the caller.
    """
    deps = deps or default_deps()

    symbol_text = str(symbol or "").strip()
    quantity_value = quantity
    price_value = price
    if quantity_value is None or price_value is None:
        raise ValueError("kiwoom_mock_place_order requires quantity and price")
    quantity_int = int(quantity_value)
    price_int = int(price_value)
    resolved_exchange = exchange or constants.MOCK_EXCHANGE_KRX

    base_response = {
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "dry_run": dry_run,
        "side": side,
        "symbol": symbol_text,
        "quantity": quantity_int,
        "price": price_int,
        "exchange": str(resolved_exchange).strip().upper(),
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
    signal_recorder: Any | None = None
    signal_created = False
    trading_day = kr_trading_day(deps.utc_now())
    kst_day = kst_date(trading_day)
    if not dry_run:
        try:
            kiwoom_attribution = resolve_kiwoom_attribution(
                symbol=symbol_text,
                side=str(side),
                price=price_int,
                quantity=quantity_int,
                strategy=strategy,
                correlation_id=correlation_id,
                kst_trade_day=kst_day,
                rung=rung,
            )
        except MissingAttribution as attribution_exc:
            logger.warning(
                "kiwoom_mock order blocked, unattributed: symbol=%s side=%s missing=%s",
                symbol_text,
                side,
                list(attribution_exc.missing),
            )
            return attribution_required_response(base_response, attribution_exc)

        signal_recorder = deps.signal_recorder()
        try:
            recorded_signal = await signal_recorder.record(
                attribution=kiwoom_attribution,
                symbol=symbol_text,
                decision="order",
                instrument_type="equity_kr",
                side=str(side),
                intended_quantity=quantity_int,
                intended_price=price_int,
                detail={"exchange": str(resolved_exchange).strip().upper()},
                kst_date=kst_day,
            )
        except Exception as signal_exc:  # noqa: BLE001 — no record, no send
            logger.error(
                "kiwoom_mock signal record failed, refusing to send: "
                "symbol=%s side=%s correlation_id=%s error=%s",
                symbol_text,
                side,
                kiwoom_attribution.correlation_id,
                type(signal_exc).__name__,
            )
            return signal_record_unavailable_response(base_response, signal_exc)
        # hk 4729 - only a row this call created proves that no earlier
        # dispatch exists under this correlation id. A replayed row also reads
        # ``recorded``, but there it means "outcome unknown" for the earlier
        # attempt, so a pre-send refusal must not relabel it.
        signal_created = recorded_signal.created
        base_response["correlation_id"] = kiwoom_attribution.correlation_id

    # ONE request-scoped client shared across preflight + POST (ROB-893 v2).
    client = deps.new_client()
    account_client = deps.account_client_cls(client)
    order_client = deps.order_client_cls(client)

    try:
        preflight = await deps.preflight(
            symbol=symbol_text,
            side=side,
            quantity=quantity_int,
            price=price_int,
            account_client=account_client,
            fetch_quote=deps.fetch_quote,
        )
    except KiwoomPreDispatchError as exc:
        await mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            created=signal_created,
            reason="preflight_refused",
            detail_patch={
                "status": "not_submitted",
                "stage": exc.stage,
                "cause_type": exc.cause_type,
            },
        )
        return not_submitted_response(base_response, exc)
    except Exception as exc:  # noqa: BLE001 - preflight must fail closed
        await mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            created=signal_created,
            reason="preflight_refused",
            detail_patch={
                "status": "not_submitted",
                "error_class": type(exc).__name__,
            },
        )
        response = {
            "success": False,
            **base_response,
            "error": f"kiwoom_mock_place_order preflight failed: {type(exc).__name__}",
        }
        if dry_run or signal_created:
            # Same closed not-sent vocabulary as every other pre-POST refusal:
            # the preflight is a read, so nothing reached the order TR. A
            # replayed identity keeps the ambiguous shape instead — the earlier
            # attempt under it may have sent.
            response["status"] = "not_submitted"
            response["dispatch_started"] = False
            response["reconcile_required"] = False
        return response
    if not preflight.ok:
        response = preflight_to_response(
            preflight,
            symbol=symbol_text,
            side=side,
            quantity=quantity_int,
            price=price_int,
            preview=dry_run,
            # A preview never claims an order identity; a confirmed refusal may
            # only speak for an identity this call created.
            not_sent=dry_run or signal_created,
        )
        response["dry_run"] = dry_run
        response["exchange"] = str(resolved_exchange).strip().upper()
        await mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            created=signal_created,
            reason="preflight_refused",
            detail_patch={
                "status": "not_submitted",
                "error_code": preflight.error_code,
            },
        )
        return response

    if dry_run:
        response = preflight_to_response(
            preflight,
            symbol=symbol_text,
            side=side,
            quantity=quantity_int,
            price=price_int,
            preview=True,
        )
        response["dry_run"] = True
        response["exchange"] = str(resolved_exchange).strip().upper()
        return response

    # Confirmed: the single preflight above IS the mutation-boundary check,
    # run immediately before POST on the SAME client/auth/token. Attach its
    # evidence as the authoritative snapshot.
    base_response.update(preflight.to_response_extras())

    # hk 4712 — the Kiwoom order body has no client idempotency field, so the
    # pre-send reservation is the only double-send defence. It is committed
    # after preflight and immediately before the POST; a duplicate refuses
    # before any broker call.
    exchange_text = str(resolved_exchange).strip().upper()
    client_order_id = build_client_order_id(
        symbol=symbol_text,
        side=str(side),
        quantity=quantity_int,
        price=price_int,
        exchange=exchange_text,
        trading_day=trading_day,
        rung=rung,
    )
    base_response["client_order_id"] = client_order_id
    recorder = deps.place_recorder()
    try:
        reservation = await recorder.reserve(
            idempotency_key=client_order_id, symbol=symbol_text, side=str(side)
        )
    except DuplicateOrderIntent:
        response = await duplicate_intent_response(
            base_response, client_order_id, recorder
        )
        await mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            created=signal_created,
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
        response = intent_unavailable_response(base_response, client_order_id, exc)
        await mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            created=signal_created,
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
        "symbol": symbol_text,
        "side": str(side),
        "quantity": quantity_int,
        "price": price_int,
        "trading_day": trading_day,
    }

    if pre_send_hook is not None:
        try:
            await pre_send_hook()
        except BaseException as hook_exc:
            # Provably not sent: the reservation is released and the caller
            # (the proposal dispatch branch) turns the raised block into a
            # window/defer outcome.
            released = await release_after_pre_dispatch_error(recorder, reservation)
            await mark_kiwoom_signal_not_sent(
                signal_recorder,
                kiwoom_attribution,
                created=signal_created,
                reason="pre_dispatch_error",
                detail_patch={
                    "status": "not_submitted",
                    "client_order_id": client_order_id,
                    "error_class": type(hook_exc).__name__,
                    "reservation_released": released,
                    "pre_send_blocked": True,
                },
            )
            raise

    try:
        if side == "buy":
            broker_response = await order_client.place_buy_order(
                symbol=symbol_text,
                quantity=quantity_int,
                price=price_int,
                exchange=resolved_exchange,
            )
        else:
            broker_response = await order_client.place_sell_order(
                symbol=symbol_text,
                quantity=quantity_int,
                price=price_int,
                exchange=resolved_exchange,
            )
    except KiwoomPreDispatchError as exc:
        # Provably never sent: no ledger row, and the same key may retry.
        released = await release_after_pre_dispatch_error(recorder, reservation)
        response = not_submitted_response(base_response, exc)
        response["reservation_released"] = released
        await mark_kiwoom_signal_not_sent(
            signal_recorder,
            kiwoom_attribution,
            created=signal_created,
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
        response = dispatch_unknown_response(base_response, exc)
        response.update(
            await record_place_outcome(
                recorder,
                outcome="uncertain",
                broker_response=None,
                broker_order_id=None,
                ledger_kwargs=ledger_kwargs,
            )
        )
        if signal_recorder is not None and kiwoom_attribution is not None:
            await mark_kiwoom_signal_outcome(
                signal_recorder,
                attribution=kiwoom_attribution,
                outcome="uncertain",
                broker_order_id=None,
                ledger_id=response.get("ledger_id"),
            )
        return response

    outcome, broker_order_id = classify_place_response(broker_response)
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
        await record_place_outcome(
            recorder,
            outcome=outcome,
            broker_response=broker_response,
            broker_order_id=broker_order_id,
            ledger_kwargs=ledger_kwargs,
        )
    )
    if signal_recorder is not None and kiwoom_attribution is not None:
        await mark_kiwoom_signal_outcome(
            signal_recorder,
            attribution=kiwoom_attribution,
            outcome=outcome,
            broker_order_id=broker_order_id,
            ledger_id=response.get("ledger_id"),
        )
    return response


__all__ = (
    "ACCOUNT_MODE_KIWOOM_MOCK",
    "KiwoomMockPlaceDeps",
    "default_deps",
    "place_kiwoom_mock_order",
)
