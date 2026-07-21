"""KIS mock order ledger writes — fully isolated from live journal/fill paths."""

from __future__ import annotations

import datetime
import re
import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.models.review import KISMockOrderLedger
from app.services.brokers.kis.live_order_expiry import kr_day_order_expiry
from app.services.brokers.kis.mock_scalping_exec.tracking_state import (
    LedgerWriteError,
)
from app.services.brokers.kis.order_id import normalize_broker_order_id
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService
from app.services.live_correlation import live_correlation_id
from app.services.live_place_provenance import publish_place_time_forecast

# ROB-843: sensitive-key fragments to redact from raw broker evidence before it
# is persisted or returned. Matched case-insensitively as a substring of the key
# (covers appkey/appsecret/approval_key/approval_hash/authorization/cookie/token).
_SENSITIVE_KEY_RE = re.compile(
    r"(token|authorization|cookie|secret|credential|password|passwd|"
    r"api[_-]?key|app[_-]?key|approval|account[_-]?no)",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"


def _redact_evidence(payload: Any) -> Any:
    """Recursively redact sensitive keys from a mapping/list (non-mutating).

    Returns a fresh structure; the original object is never modified. Only keys
    are matched — scalar values under non-sensitive keys (order id, result code,
    message) are preserved verbatim for diagnostics.
    """
    if isinstance(payload, Mapping):
        return {
            k: _REDACTED
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k)
            else _redact_evidence(v)
            for k, v in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [_redact_evidence(item) for item in payload]
    return payload


KIS_MOCK_SHADOW_PENDING_SOURCE = "kis_mock_ledger_shadow"
KIS_MOCK_SHADOW_PENDING_CONFIDENCE = "db_shadow_pending"
KIS_MOCK_SHADOW_PENDING_WARNING = (
    "KIS mock pending-order broker endpoints are unsupported/incomplete; "
    "non-terminal review.kis_mock_order_ledger rows are treated as shadow pending. "
    "KIS mock daily-ccld zero rows are not proof of no pending orders."
)

_LEDGER_STATUS_TO_LIFECYCLE: dict[str, str] = {
    "accepted": "accepted",
    "rejected": "failed",
    "unknown": "anomaly",
}


def _status_to_lifecycle_state(status: str | None) -> str:
    if status is None:
        return "anomaly"
    return _LEDGER_STATUS_TO_LIFECYCLE.get(status, "anomaly")


def _accepted_or_failed_message(
    accepted: bool, status: str, ledger_id: int | None
) -> str:
    """Human-readable outcome message for the normalized mock-order response."""
    if not accepted:
        return f"KIS mock order not accepted (status={status}); evidence recorded"
    if ledger_id:
        return "KIS mock order recorded to kis_mock_order_ledger"
    return "KIS mock order accepted but ledger insert returned no id"


def _to_decimal(val: Any) -> Decimal | None:
    if val in ("", None):
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def _fetch_kis_mock_baseline_qty(
    *, normalized_symbol: str, market_type: str
) -> Decimal | None:
    """Best-effort: read current is_mock holdings qty for ``normalized_symbol``.

    Returns ``Decimal(0)`` when the position simply does not exist (legitimate
    pre-buy baseline) and ``None`` only on broker/decoding failure so that the
    reconciler can flag it as ``baseline_missing`` later.
    """
    from app.services.brokers.kis import KISClient

    try:
        kis = KISClient(is_mock=True)
        if market_type == "equity_us":
            stocks = await kis.fetch_my_stocks(is_mock=True, is_overseas=True)
            for stock in stocks or []:
                if to_db_symbol(str(stock.get("ovrs_pdno") or "")) == normalized_symbol:
                    qty = _to_decimal(stock.get("ovrs_cblc_qty"))
                    return qty if qty is not None else Decimal(0)
            return Decimal(0)
        stocks = await kis.fetch_my_stocks(is_mock=True, is_overseas=False)
        for stock in stocks or []:
            if to_db_symbol(str(stock.get("pdno") or "")) == normalized_symbol:
                qty = _to_decimal(stock.get("hldg_qty"))
                return qty if qty is not None else Decimal(0)
        return Decimal(0)
    except Exception as exc:
        logger.warning(
            "Failed to fetch KIS mock baseline for %s (%s): %s",
            normalized_symbol,
            market_type,
            exc,
        )
        return None


def _order_session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


def _decimal_to_float(value: Any) -> float:
    return _to_float(value, default=0.0)


def _derive_shadow_fill(
    row: KISMockOrderLedger, ordered_qty: float
) -> tuple[float, float, str]:
    """Derive (filled_qty, remaining_qty, status) consistent with lifecycle_state.

    A row in ``fill`` must never report status=pending/filled_qty=0. When the
    reconciler recorded ``attributed_fill_qty`` (ROB-400) we honor it; legacy
    fill rows without it fall back to a full fill so lifecycle and status agree.
    """
    if row.lifecycle_state != "fill":
        return 0.0, ordered_qty, "pending"

    # Compare in Decimal so a fractional-share fill (e.g. 9.9999999 vs 10) is not
    # mislabeled partial by float rounding; the reconciler emits Decimal values.
    ordered_dec = Decimal(str(ordered_qty))
    detail = row.last_reconcile_detail or {}
    raw = detail.get("attributed_fill_qty")
    if raw is None:
        filled_dec = ordered_dec
    else:
        try:
            filled_dec = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            filled_dec = ordered_dec
        if filled_dec < 0:
            filled_dec = Decimal("0")
        elif filled_dec > ordered_dec:
            filled_dec = ordered_dec
    remaining_dec = ordered_dec - filled_dec
    status = "filled" if filled_dec >= ordered_dec else "partial"
    return float(filled_dec), float(remaining_dec), status


def _shadow_row_to_order(row: KISMockOrderLedger) -> dict[str, Any]:
    ordered_at = row.trade_date.isoformat() if row.trade_date else None
    ordered_qty = _decimal_to_float(row.quantity)
    filled_qty, remaining_qty, status = _derive_shadow_fill(row, ordered_qty)
    return {
        "order_id": row.order_no or f"ledger:{row.id}",
        "ledger_id": row.id,
        "symbol": row.symbol,
        "market": "kr" if row.instrument_type == "equity_kr" else "us",
        "instrument_type": row.instrument_type,
        "side": row.side,
        "order_type": row.order_type,
        "status": status,
        "lifecycle_state": row.lifecycle_state,
        "ordered_qty": ordered_qty,
        "remaining_qty": remaining_qty,
        "filled_qty": filled_qty,
        "ordered_price": _decimal_to_float(row.price),
        "amount": _decimal_to_float(row.amount),
        "currency": row.currency,
        "ordered_at": ordered_at,
        "created_at": ordered_at,
        "source": KIS_MOCK_SHADOW_PENDING_SOURCE,
        "confidence": KIS_MOCK_SHADOW_PENDING_CONFIDENCE,
        "warning": KIS_MOCK_SHADOW_PENDING_WARNING,
    }


async def _list_kis_mock_shadow_pending_orders(
    *,
    normalized_symbol: str | None = None,
    market_type: str | None = None,
    side: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return DB shadow pending rows for KIS mock.

    This is intentionally DB-only. It compensates for official KIS mock pending
    inquiry gaps without treating KIS daily-ccld empty output as no pending.
    """
    async with _order_session_factory()() as db:
        svc = KISMockLifecycleService(db)
        rows = await svc.list_open_orders(
            limit=limit,
            symbol=normalized_symbol,
            instrument_type=market_type,
            side=side,
        )
    return [_shadow_row_to_order(row) for row in rows]


def _is_kr_day_order_expired_for_reservation(
    row: dict[str, Any],
    *,
    now: datetime.datetime,
) -> bool:
    """ROB-890: conservative KR DAY expiry for shadow reservation only.

    Returns True when the row's conservative expiry time has passed relative to
    ``now``. Only ``equity_kr`` rows are evaluated; ``equity_us`` rows always
    return False (KR session rules do not apply). Fail-closed: missing or
    unparseable ``ordered_at`` returns False so the reservation is kept.
    """
    if row.get("instrument_type") != "equity_kr":
        return False

    ordered_at_str = row.get("ordered_at")
    if not ordered_at_str:
        return False

    try:
        accepted_at = datetime.datetime.fromisoformat(str(ordered_at_str))
    except (ValueError, TypeError):
        return False

    expiry_iso, _reason = kr_day_order_expiry(
        accepted_at=accepted_at,
        side=row.get("side", ""),
    )
    if not expiry_iso:
        return False

    try:
        expiry_dt = datetime.datetime.fromisoformat(expiry_iso)
    except (ValueError, TypeError):
        return False

    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=9)))

    return now >= expiry_dt


async def _get_kis_mock_shadow_exposure(
    *,
    normalized_symbol: str | None = None,
    market_type: str | None = None,
    limit: int = 1000,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Summarize non-terminal KIS mock ledger exposure.

    ROB-890: KR DAY orders whose conservative session expiry has passed are
    excluded from ``buy_reserved_amount`` / ``sell_reserved_quantity`` so stale
    shadow-pending rows stop permanently locking cash and sellable quantity.
    Expired rows remain visible in ``orders`` for reconciliation; lifecycle
    state is never mutated by this function.

    On DB/query failure, confidence becomes ``unknown`` so execution paths can
    fail closed rather than over-allocating cash or sellable quantity.
    """
    if now is None:
        now = now_kst()

    try:
        rows = await _list_kis_mock_shadow_pending_orders(
            normalized_symbol=normalized_symbol,
            market_type=market_type,
            limit=limit,
        )
    except Exception as exc:
        logger.warning("Failed to read KIS mock shadow pending ledger: %s", exc)
        return {
            "confidence": "unknown",
            "error": str(exc) or exc.__class__.__name__,
            "source": KIS_MOCK_SHADOW_PENDING_SOURCE,
            "warning": KIS_MOCK_SHADOW_PENDING_WARNING,
            "buy_reserved_amount": 0.0,
            "sell_reserved_quantity": 0.0,
            "orders": [],
            "expired_reservation_count": 0,
        }

    active_rows = [
        row
        for row in rows
        if not _is_kr_day_order_expired_for_reservation(row, now=now)
    ]
    expired_count = len(rows) - len(active_rows)

    buy_reserved = sum(
        _decimal_to_float(row.get("amount"))
        for row in active_rows
        if row.get("side") == "buy"
    )
    sell_reserved = sum(
        _decimal_to_float(row.get("remaining_qty"))
        for row in active_rows
        if row.get("side") == "sell"
    )
    return {
        "confidence": KIS_MOCK_SHADOW_PENDING_CONFIDENCE,
        "source": KIS_MOCK_SHADOW_PENDING_SOURCE,
        "warning": KIS_MOCK_SHADOW_PENDING_WARNING if rows else None,
        "buy_reserved_amount": buy_reserved,
        "sell_reserved_quantity": sell_reserved,
        "orders": rows,
        "expired_reservation_count": expired_count,
    }


async def _save_kis_mock_order_ledger(
    *,
    symbol: str,
    instrument_type: str,
    side: str,
    order_type: str,
    quantity: float,
    price: float,
    amount: float,
    currency: str,
    order_no: str | None,
    order_time: str | None,
    krx_fwdg_ord_orgno: str | None,
    status: str,
    response_code: str | None,
    response_message: str | None,
    raw_response: dict[str, Any] | None,
    reason: str | None,
    thesis: str | None,
    strategy: str | None,
    notes: str | None,
    lifecycle_state: str | None = None,
    holdings_baseline_qty: Decimal | None = None,
    fee: float = 0,
    correlation_id: str | None = None,
    scalping_role: str | None = None,
    exit_reason: str | None = None,
    gross_pnl: Decimal | None = None,
    net_pnl: Decimal | None = None,
    reconciled_at: Any | None = None,
    report_item_uuid: uuid.UUID | None = None,
    mirror_cohort: str | None = None,
    mirror_source_bucket: str | None = None,
    raise_on_error: bool = False,
) -> int | None:
    """Insert one row into review.kis_mock_order_ledger.

    Returns the new primary-key id, or None on a benign on-conflict no-op. When
    ``raise_on_error`` is set, a real DB error raises ``LedgerWriteError`` so the
    caller can distinguish a conflict (row already durable) from a lost write
    (ROB-843 P1-2); otherwise a DB error is swallowed and returns None (legacy).
    """
    resolved_lifecycle = lifecycle_state or _status_to_lifecycle_state(status)
    # ROB-843: a row inserted directly as ``reconciled`` (e.g. a scalping exit
    # close) must carry an authoritative ``reconciled_at`` so cooldown can key
    # off the real close time; the reconciler sets it on job-driven transitions.
    if reconciled_at is None and resolved_lifecycle == "reconciled":
        reconciled_at = now_kst()
    try:
        async with _order_session_factory()() as db:
            stmt = (
                pg_insert(KISMockOrderLedger)
                .values(
                    trade_date=now_kst(),
                    symbol=symbol,
                    instrument_type=instrument_type,
                    side=side,
                    order_type=order_type,
                    quantity=quantity,
                    price=price,
                    amount=amount,
                    fee=fee,
                    currency=currency,
                    order_no=order_no,
                    order_time=order_time,
                    krx_fwdg_ord_orgno=krx_fwdg_ord_orgno,
                    account_mode="kis_mock",
                    broker="kis",
                    status=status,
                    response_code=response_code,
                    response_message=response_message,
                    raw_response=raw_response,
                    reason=(reason or None),
                    thesis=thesis,
                    strategy=strategy,
                    notes=notes,
                    lifecycle_state=resolved_lifecycle,
                    holdings_baseline_qty=holdings_baseline_qty,
                    correlation_id=correlation_id,
                    scalping_role=scalping_role,
                    exit_reason=exit_reason,
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    reconciled_at=reconciled_at,
                    report_item_uuid=report_item_uuid,
                    mirror_cohort=mirror_cohort,
                    mirror_source_bucket=mirror_source_bucket,
                )
                .on_conflict_do_nothing(constraint="uq_kis_mock_ledger_order_no")
            )
            result = await db.execute(stmt)
            await db.commit()
            if result.inserted_primary_key and result.inserted_primary_key[0]:
                return typing_cast(int, result.inserted_primary_key[0])
            return None
    except Exception as exc:
        logger.warning("Failed to save kis_mock order ledger row: %s", exc)
        if raise_on_error:
            raise LedgerWriteError(str(exc) or exc.__class__.__name__) from exc
        return None


async def _native_row_exists(order_no: str | None) -> bool:
    """True if a durable native (non-scalping) row exists for ``order_no``.

    Used after an on-conflict no-op to confirm the existing row is durable
    (ROB-843 P1-2) rather than treating the write as lost.
    """
    if not order_no:
        return False
    try:
        async with _order_session_factory()() as db:
            found = await db.scalar(
                select(func.count())
                .select_from(KISMockOrderLedger)
                .where(
                    func.trim(KISMockOrderLedger.order_no) == order_no.strip(),
                    KISMockOrderLedger.scalping_role.is_(None),
                )
            )
        return bool(found or 0)
    except Exception as exc:  # noqa: BLE001 — lookup failure => not durable
        logger.warning("native row lookup failed for order_no=%s: %s", order_no, exc)
        return False


async def _record_kis_mock_order(
    *,
    normalized_symbol: str,
    market_type: str,
    side: str,
    order_type: str,
    dry_run_result: dict[str, Any],
    execution_result: dict[str, Any],
    reason: str | None,
    thesis: str | None,
    strategy: str | None,
    notes: str | None,
    holdings_baseline_qty: Decimal | None = None,
    correlation_id: str | None = None,
    target_price: float | None = None,
    min_hold_days: int | None = None,
    report_item_uuid: uuid.UUID | None = None,
    mirror_cohort: str | None = None,
    mirror_source_bucket: str | None = None,
) -> dict[str, Any]:
    """Build ledger row from execution result and return the mock-order response dict."""
    price_val = _to_float(dry_run_result.get("price"), default=0.0)
    qty_val = _to_float(dry_run_result.get("quantity"), default=0.0)
    amt_val = _to_float(dry_run_result.get("estimated_value"), default=0.0)
    currency = "KRW" if market_type != "equity_us" else "USD"

    # ROB-843: normalize the native submit response truthfully. A malformed
    # (non-mapping) payload carries no order fields — treat as unknown and keep
    # a redacted marker as evidence rather than crashing on ``.get``.
    is_mapping = isinstance(execution_result, Mapping)
    raw_exec: dict[str, Any]
    if is_mapping:
        raw_exec = dict(execution_result)
    else:
        raw_exec = {"_malformed": str(execution_result)[:500]}

    # ROB-843: normalize the broker order id (strip; reject blank/whitespace/
    # malformed) so "   " is never treated as an accepted order. Domestic and
    # overseas results share this helper.
    order_no = normalize_broker_order_id(raw_exec.get("odno") or raw_exec.get("ord_no"))
    order_time = raw_exec.get("ord_tmd")
    raw_output = raw_exec.get("output") or {}
    krx_orgno = raw_exec.get("krx_fwdg_ord_orgno") or (
        raw_output.get("KRX_FWDG_ORD_ORGNO")
        if isinstance(raw_output, Mapping)
        else None
    )
    rt_cd = str(raw_exec.get("rt_cd", "")) or None
    msg = raw_exec.get("msg") or raw_exec.get("msg1")

    # Accepted success REQUIRES all of: a mapping payload, provider success
    # status (rt_cd == "0"), and a valid (non-blank) broker order ID. Anything
    # else is rejected (provider error code) or unknown (id-less / malformed).
    accepted = is_mapping and rt_cd == "0" and order_no is not None
    if accepted:
        status = "accepted"
    elif is_mapping and rt_cd and rt_cd != "0":
        status = "rejected"
    else:
        status = "unknown"

    if accepted:
        failure_reason: str | None = None
        failure_detail: str | None = None
    elif not is_mapping:
        failure_reason = "malformed_response"
        failure_detail = raw_exec["_malformed"]
    elif status == "rejected":
        failure_reason = "broker_rejected"
        failure_detail = msg or f"rt_cd={rt_cd}"
    elif rt_cd == "0":
        failure_reason = "missing_broker_order_id"
        failure_detail = msg or "provider success without broker order id"
    else:
        failure_reason = "unknown_response"
        failure_detail = msg or f"rt_cd={rt_cd} order_no={order_no}"

    # ROB-730 provenance spine: mint a deterministic place-time correlation_id so
    # this mock order joins forecast → fill → journal → retrospective, mirroring
    # the kis_live path verbatim. Preserve an explicit id (ROB-402 scalping
    # entry/exit pairing passes one) rather than overwriting it.
    if correlation_id is None:
        correlation_id = live_correlation_id(
            account_scope="kis_mock",
            symbol=normalized_symbol,
            side=side,
            price=Decimal(str(price_val)),
            quantity=Decimal(str(qty_val)),
            kst_trade_day=now_kst().strftime("%Y-%m-%d"),
            rung=0,
        )

    # ROB-843: redact sensitive keys from the raw broker evidence before it is
    # persisted or returned. Recursive, non-mutating; non-sensitive diagnostics
    # (order id / result code / message) are preserved.
    redacted_exec = _redact_evidence(raw_exec)

    # ROB-843 P1-2: distinguish a benign on-conflict no-op from a lost write. A
    # lost native write must not silently drop the order from the daily count —
    # fall back to a durable evidence row, and if that also fails, degrade
    # tracking so subsequent automated orders fail closed.
    ledger_tracking_unavailable = False
    try:
        ledger_id = await _save_kis_mock_order_ledger(
            symbol=normalized_symbol,
            instrument_type=market_type,
            side=side,
            order_type=order_type,
            quantity=qty_val,
            price=price_val,
            amount=amt_val,
            currency=currency,
            order_no=order_no,
            order_time=order_time,
            krx_fwdg_ord_orgno=krx_orgno,
            status=status,
            response_code=rt_cd,
            response_message=msg,
            raw_response=redacted_exec,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            notes=notes,
            lifecycle_state=_status_to_lifecycle_state(status),
            holdings_baseline_qty=holdings_baseline_qty,
            correlation_id=correlation_id,
            report_item_uuid=report_item_uuid,
            mirror_cohort=mirror_cohort,
            mirror_source_bucket=mirror_source_bucket,
            raise_on_error=True,
        )
    except LedgerWriteError as exc:
        logger.warning(
            "native kis_mock ledger write lost (symbol=%s order_no=%s): %s",
            normalized_symbol,
            order_no,
            exc,
        )
        ledger_id = None

    if accepted:
        # ROB-843 P1: an accepted order whose native row is NOT durable (write
        # error, and no existing row from a conflict) is "sent but not tracked".
        # We do NOT write a control row here (that shared the native write's
        # failure mode). Instead the caller keeps the write-ahead reservation
        # unresolved — a durable, restart-safe fail-close resolved only by
        # reconciliation. Signal that state to the caller.
        native_durable = ledger_id is not None or await _native_row_exists(order_no)
        if not native_durable:
            ledger_tracking_unavailable = True

    # ROB-730: emit the place-time forecast only for accepted orders (mirrors
    # kis_live). publish_place_time_forecast is itself buy+target-gated and runs
    # in its own isolated session, swallowing errors — a forecast hiccup never
    # affects the recorded order.
    if status == "accepted" and ledger_id is not None:
        await publish_place_time_forecast(
            correlation_id=correlation_id,
            symbol=normalized_symbol,
            instrument_type=market_type,
            side=side,
            target_price=target_price,
            min_hold_days=min_hold_days,
            session_label="kis_mock_place",
            created_by="auto_place_mock",
            report_item_uuid=str(report_item_uuid) if report_item_uuid else None,
        )

    return {
        # ROB-843: success is truthful — accepted iff mapping + rt_cd==0 +
        # non-empty broker order ID. Rejected/malformed/unknown/id-less never
        # report success, and the native failure/unknown evidence is preserved.
        "success": accepted,
        "dry_run": False,
        "preview": dry_run_result,
        "execution": redacted_exec,
        "account_mode": "kis_mock",
        "broker": "kis",
        "ledger_id": ledger_id,
        "order_no": order_no,
        "odno": order_no,
        "order_time": order_time,
        "ord_tmd": order_time,
        "krx_fwdg_ord_orgno": krx_orgno,
        "status": status,
        "response_code": rt_cd,
        "response_message": msg,
        "reason": failure_reason,
        "detail": failure_detail,
        "correlation_id": correlation_id,
        "fill_recorded": False,
        "journal_created": False,
        # ROB-843 P1-2: broker success is preserved; this flags that durable
        # bookkeeping was lost so the caller can fail-close the NEXT order.
        "ledger_tracking_unavailable": ledger_tracking_unavailable,
        "message": (_accepted_or_failed_message(accepted, status, ledger_id)),
    }


_MARKET_ALIASES = {"kr": "equity_kr", "us": "equity_us"}
_ALLOWED_RECONCILE_MARKETS = frozenset({"equity_kr", "equity_us"})
_ALLOWED_RECONCILE_MARKET_VALUES = sorted(_MARKET_ALIASES) + sorted(
    _ALLOWED_RECONCILE_MARKETS
)


def normalize_kis_mock_reconcile_market(market: str | None) -> str | None:
    if market is None:
        return None
    return _MARKET_ALIASES.get(market, market)


async def kis_mock_reconciliation_run_impl(
    *,
    dry_run: bool = True,
    limit: int = 100,
    market: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Execute KIS mock order reconciliation and return summary.

    ``market``/``symbol`` (ROB-1018) narrow the open-order lookup so a
    single-market/single-symbol reconciliation pass never proposes
    transitions on out-of-scope rows (e.g. a US session no longer flips KR
    resting orders to ``stale``). Both default to ``None``, preserving the
    prior full-scan behavior for existing callers (TaskIQ periodic task,
    unscoped MCP calls).

    An unrecognized ``market`` (typo or unsupported venue) is rejected
    explicitly rather than silently passed through — KIS mock only covers
    KR/US equity (``equity_kr``/``equity_us``). A silent pass-through would
    otherwise yield an ``orders_processed=0`` false-success indistinguishable
    from "scope matched but nothing was open".

    Response contract (ROB-1018 fix #2): every success/error path returns
    the *effective* (canonical, alias-normalized) scope under ``scope`` —
    e.g. a requested ``market="us"`` always echoes back as ``"equity_us"``.
    The one exception is the unknown-market rejection below: since no valid
    scope was ever established, it has no ``scope`` key and instead echoes
    the verbatim, unnormalized request under ``requested_scope`` so callers
    can't mistake it for a scope that actually ran.
    """
    normalized_market = normalize_kis_mock_reconcile_market(market)
    if (
        normalized_market is not None
        and normalized_market not in _ALLOWED_RECONCILE_MARKETS
    ):
        return {
            "success": False,
            "error": (
                f"unknown market '{market}' — allowed values: "
                f"{_ALLOWED_RECONCILE_MARKET_VALUES}"
            ),
            "allowed_markets": _ALLOWED_RECONCILE_MARKET_VALUES,
            "account_mode": "kis_mock",
            "requested_scope": {"market": market, "symbol": symbol},
        }
    try:
        async with _order_session_factory()() as db:
            return await run_kis_mock_reconciliation(
                db,
                dry_run=dry_run,
                limit=limit,
                market=normalized_market,
                symbol=symbol,
            )
    except Exception as exc:
        logger.exception("Failed to run KIS mock reconciliation: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "source": "mcp",
            "account_mode": "kis_mock",
            "scope": {"market": normalized_market, "symbol": symbol},
        }


async def resolve_mock_order_for_cancel(order_no: str) -> dict[str, Any] | None:
    """Resolve cancel/modify inputs from the ledger (no TTTC8036R inquiry).

    Returns ledger_id + the fields the KIS cancel/modify TR needs, or None
    when no row matches ``order_no``.
    """
    async with _order_session_factory()() as db:
        svc = KISMockLifecycleService(db)
        row = await svc.get_by_order_no(order_no=order_no)
        if row is None:
            return None
        return {
            "ledger_id": row.id,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": _decimal_to_float(row.quantity),
            "price": _decimal_to_float(row.price),
            "krx_fwdg_ord_orgno": row.krx_fwdg_ord_orgno,
            "instrument_type": row.instrument_type,
            "lifecycle_state": row.lifecycle_state,
        }


async def mark_kis_mock_order_cancelled(
    *,
    ledger_id: int,
    broker_confirmed: bool,
    detail: dict[str, Any],
) -> None:
    """Transition a ledger row to 'cancelled' via the single write chokepoint."""
    async with _order_session_factory()() as db:
        svc = KISMockLifecycleService(db)
        await svc.apply_lifecycle_transition(
            ledger_id=ledger_id,
            next_state="cancelled",
            reason_code=(
                "broker_cancel_confirmed"
                if broker_confirmed
                else "soft_cancel_broker_unsupported"
            ),
            detail={"broker_cancel_confirmed": broker_confirmed, **detail},
            dry_run=False,
        )


async def update_kis_mock_order_terms(
    *,
    ledger_id: int,
    price: Decimal | None = None,
    quantity: Decimal | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Reflect a broker-confirmed modify on the ledger row."""
    async with _order_session_factory()() as db:
        svc = KISMockLifecycleService(db)
        await svc.update_order_terms(
            ledger_id=ledger_id, price=price, quantity=quantity, detail=detail
        )
