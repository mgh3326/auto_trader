"""KIS mock order ledger writes — fully isolated from live journal/fill paths."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any
from typing import cast as typing_cast

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.models.review import KISMockOrderLedger
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService
from app.services.live_correlation import live_correlation_id
from app.services.live_place_provenance import publish_place_time_forecast

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


async def _get_kis_mock_shadow_exposure(
    *,
    normalized_symbol: str | None = None,
    market_type: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Summarize non-terminal KIS mock ledger exposure.

    On DB/query failure, confidence becomes ``unknown`` so execution paths can
    fail closed rather than over-allocating cash or sellable quantity.
    """
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
        }

    buy_reserved = sum(
        _decimal_to_float(row.get("amount")) for row in rows if row.get("side") == "buy"
    )
    sell_reserved = sum(
        _decimal_to_float(row.get("remaining_qty"))
        for row in rows
        if row.get("side") == "sell"
    )
    return {
        "confidence": KIS_MOCK_SHADOW_PENDING_CONFIDENCE,
        "source": KIS_MOCK_SHADOW_PENDING_SOURCE,
        "warning": KIS_MOCK_SHADOW_PENDING_WARNING if rows else None,
        "buy_reserved_amount": buy_reserved,
        "sell_reserved_quantity": sell_reserved,
        "orders": rows,
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
    report_item_uuid: uuid.UUID | None = None,
) -> int | None:
    """Insert one row into review.kis_mock_order_ledger.

    Returns the new primary-key id, or None on conflict / error.
    """
    resolved_lifecycle = lifecycle_state or _status_to_lifecycle_state(status)
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
                    report_item_uuid=report_item_uuid,
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
        return None


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
) -> dict[str, Any]:
    """Build ledger row from execution result and return the mock-order response dict."""
    price_val = _to_float(dry_run_result.get("price"), default=0.0)
    qty_val = _to_float(dry_run_result.get("quantity"), default=0.0)
    amt_val = _to_float(dry_run_result.get("estimated_value"), default=0.0)
    currency = "KRW" if market_type != "equity_us" else "USD"

    order_no = execution_result.get("odno") or execution_result.get("ord_no")
    order_time = execution_result.get("ord_tmd")
    raw_output = execution_result.get("output") or {}
    krx_orgno = execution_result.get("krx_fwdg_ord_orgno") or raw_output.get(
        "KRX_FWDG_ORD_ORGNO"
    )
    rt_cd = str(execution_result.get("rt_cd", "")) or None
    msg = execution_result.get("msg") or execution_result.get("msg1")

    if rt_cd == "0":
        status = "accepted"
    elif rt_cd and rt_cd != "0":
        status = "rejected"
    else:
        status = "accepted" if order_no else "unknown"

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

    ledger_id = await _save_kis_mock_order_ledger(
        symbol=normalized_symbol,
        instrument_type=market_type,
        side=side,
        order_type=order_type,
        quantity=qty_val,
        price=price_val,
        amount=amt_val,
        currency=currency,
        order_no=str(order_no) if order_no else None,
        order_time=order_time,
        krx_fwdg_ord_orgno=krx_orgno,
        status=status,
        response_code=rt_cd,
        response_message=msg,
        raw_response=execution_result,
        reason=reason,
        thesis=thesis,
        strategy=strategy,
        notes=notes,
        lifecycle_state=_status_to_lifecycle_state(status),
        holdings_baseline_qty=holdings_baseline_qty,
        correlation_id=correlation_id,
        report_item_uuid=report_item_uuid,
    )

    # ROB-730: emit the place-time forecast only for accepted orders (mirrors
    # kis_live). publish_place_time_forecast is itself buy+target-gated and runs
    # in its own isolated session, swallowing errors — a forecast hiccup never
    # affects the recorded order.
    if status == "accepted":
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
        "success": True,
        "dry_run": False,
        "preview": dry_run_result,
        "execution": execution_result,
        "account_mode": "kis_mock",
        "broker": "kis",
        "ledger_id": ledger_id,
        "order_no": str(order_no) if order_no else None,
        "odno": str(order_no) if order_no else None,
        "order_time": order_time,
        "ord_tmd": order_time,
        "krx_fwdg_ord_orgno": krx_orgno,
        "status": status,
        "response_code": rt_cd,
        "response_message": msg,
        "correlation_id": correlation_id,
        "fill_recorded": False,
        "journal_created": False,
        "message": (
            "KIS mock order recorded to kis_mock_order_ledger"
            if ledger_id
            else "KIS mock order accepted but ledger insert returned no id"
        ),
    }


async def kis_mock_reconciliation_run_impl(
    *,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """Execute KIS mock order reconciliation and return summary."""
    try:
        async with _order_session_factory()() as db:
            return await run_kis_mock_reconciliation(db, dry_run=dry_run, limit=limit)
    except Exception as exc:
        logger.exception("Failed to run KIS mock reconciliation: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "source": "mcp",
            "account_mode": "kis_mock",
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
