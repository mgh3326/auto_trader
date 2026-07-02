"""ROB-395 — KIS live order ledger writes + reconciliation.

SEND records accepted/rejected only (no trades/journal/realized_pnl). RECONCILE
applies journal mutations from order-id-keyed broker fill evidence. Fully
isolated from the mock ledger (kis_live_order_ledger vs kis_mock_order_ledger).
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.order_journal import (
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _save_order_fill,
)
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.models.review import KISLiveOrderLedger
from app.services.brokers.kis.live_order_expiry import (
    classify_day_order_expiry,
    nxt_session_closed,
)
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillVerdict,
    classify_fill_evidence,
)

# lifecycle_state mirrors status for live (no separate mock shadow semantics)
_STATUS_TO_LIFECYCLE: dict[str, str] = {
    "accepted": "accepted",
    "rejected": "failed",
    "unknown": "anomaly",
    "filled": "filled",
    "partial": "partial",
    "pending": "accepted",
    "cancelled": "cancelled",
    "expired": "cancelled",  # ROB-476 — terminal, no journal side-effect
    "anomaly": "anomaly",
}


def _status_to_lifecycle(status: str) -> str:
    return _STATUS_TO_LIFECYCLE.get(status, "anomaly")


def _derive_live_send_status(*, rt_cd: str | None, order_no: str | None) -> str:
    """Derive accepted|rejected|unknown from broker submit response.

    Never fakes success: a non-zero rt_cd is broker evidence of rejection.
    """
    if rt_cd == "0":
        return "accepted"
    if rt_cd and rt_cd != "0":
        return "rejected"
    return "accepted" if order_no else "unknown"


def _order_session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


def _to_decimal(val: Any) -> Decimal | None:
    if val in ("", None):
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def _save_kis_live_order_ledger(
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
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    exit_reason: str | None,
    indicators_snapshot: dict[str, Any] | None,
    fee: float = 0.0,
    report_item_uuid: uuid.UUID | None = None,
    approval_hash: str | None = None,
    idempotency_key: str | None = None,
) -> int | None:
    """Insert one accepted/rejected live order row. Returns new id or None."""
    try:
        async with _order_session_factory()() as db:
            stmt = (
                pg_insert(KISLiveOrderLedger)
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
                    account_mode="kis_live",
                    broker="kis",
                    status=status,
                    lifecycle_state=_status_to_lifecycle(status),
                    response_code=response_code,
                    response_message=response_message,
                    raw_response=raw_response,
                    reason=(reason or None),
                    thesis=thesis,
                    strategy=strategy,
                    target_price=_to_decimal(target_price),
                    stop_loss=_to_decimal(stop_loss),
                    min_hold_days=min_hold_days,
                    notes=notes,
                    exit_reason=exit_reason,
                    indicators_snapshot=indicators_snapshot,
                    report_item_uuid=report_item_uuid,
                    approval_hash=approval_hash,
                    idempotency_key=idempotency_key,
                )
                .on_conflict_do_nothing(constraint="uq_kis_live_ledger_order_no")
            )
            result = await db.execute(stmt)
            await db.commit()
            if result.inserted_primary_key and result.inserted_primary_key[0]:
                return typing_cast(int, result.inserted_primary_key[0])
            return None
    except Exception as exc:
        logger.warning("Failed to save kis_live order ledger row: %s", exc)
        return None


_BROKER_EXCHANGE_KEYS = ("EXCG_ID_DVSN_CD", "excg_id_dvsn_cd", "exg_id_dvsn_cd")


def _expected_day_order_expiry(now: datetime.datetime) -> str | None:
    """Day-order expiry = NXT close 20:00 KST of the send date (ISO 8601), or None.

    ROB-487: SOR day orders stay alive in the NXT session until 20:00 KST. The
    old KRX 15:30 stamp gave a 15:31 NXT-session order an expected_expiry that
    was already in the past at send time.
    """
    try:
        local = now.astimezone(KST)
        close = local.replace(hour=20, minute=0, second=0, microsecond=0)
        return close.isoformat()
    except (ValueError, OverflowError):
        return None


def _extract_broker_exchange(execution_result: dict[str, Any]) -> str | None:
    """Read the broker-reported exchange factually; None if absent (no fabrication)."""
    output = execution_result.get("output") or {}
    for source in (execution_result, output):
        for key in _BROKER_EXCHANGE_KEYS:
            val = source.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return None


async def _record_kis_live_order(
    *,
    normalized_symbol: str,
    market_type: str,
    side: str,
    order_type: str,
    dry_run_result: dict[str, Any],
    execution_result: dict[str, Any],
    reason: str | None,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    report_item_uuid: uuid.UUID | None = None,
    approval_hash: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Record a live KR order as accepted/rejected. No fill/journal/pnl booked."""
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

    status = _derive_live_send_status(
        rt_cd=rt_cd, order_no=str(order_no) if order_no else None
    )

    ledger_id = await _save_kis_live_order_ledger(
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
        target_price=target_price,
        stop_loss=stop_loss,
        min_hold_days=min_hold_days,
        notes=notes,
        exit_reason=exit_reason,
        indicators_snapshot=indicators_snapshot,
        report_item_uuid=report_item_uuid,
        approval_hash=approval_hash,
        idempotency_key=idempotency_key,
    )

    return {
        "success": True,
        "dry_run": False,
        "preview": dry_run_result,
        "execution": execution_result,
        "account_mode": "kis_live",
        "broker": "kis",
        "ledger_id": ledger_id,
        "order_id": str(order_no) if order_no else None,
        "odno": str(order_no) if order_no else None,
        "order_time": order_time,
        "krx_fwdg_ord_orgno": krx_orgno,
        "broker_status": status,
        "response_code": rt_cd,
        "response_message": msg,
        "fill_recorded": False,
        "journal_created": False,
        "order_validity": "day",
        "routing": {
            "requested_venue": "auto",
            "note": "SOR auto-route (KRX; NXT-eligible)",
        },
        "expected_expiry": _expected_day_order_expiry(now_kst()),
        "broker_exchange": _extract_broker_exchange(execution_result),
        "message": (
            "KIS live order accepted (pending fill); run kis_live_reconcile_orders "
            "to record fill/journal once the broker confirms execution"
            if status == "accepted"
            else f"KIS live order not accepted (broker_status={status})"
        ),
    }


def _create_live_kis_client() -> Any:
    from app.services.brokers.kis import KISClient

    return KISClient()


_KST = datetime.timezone(datetime.timedelta(hours=9))

# KIS inquire-daily-ccld supports up to ~3 months; anchor the window on the
# order's trade_date so older stuck rows can still recover their evidence.
_LIVE_DAILY_ORDER_LOOKBACK_DAYS = 90


def _today_yyyymmdd() -> str:
    return datetime.datetime.now(_KST).strftime("%Y%m%d")


def _coerce_order_date(
    value: datetime.datetime | datetime.date | str | None,
) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            return value.astimezone(_KST).date()
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if len(text) >= 8 and text[:8].isdigit():
            try:
                return datetime.datetime.strptime(text[:8], "%Y%m%d").date()
            except ValueError:
                return None
        try:
            return datetime.date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _live_daily_order_window(
    order_trade_date: datetime.datetime | datetime.date | str | None,
) -> tuple[str, str]:
    today = datetime.datetime.strptime(_today_yyyymmdd(), "%Y%m%d").date()
    earliest = today - datetime.timedelta(days=_LIVE_DAILY_ORDER_LOOKBACK_DAYS - 1)
    order_date = _coerce_order_date(order_trade_date)
    if order_date is None:
        return earliest.strftime("%Y%m%d"), today.strftime("%Y%m%d")

    # ROB-487 follow-up: live TTTC8001R accepted exact order-date probes for
    # prior-day rows, while a broad order_date..today range can fail with
    # KIER2570 "조회일자를 확인하십시오". For known ledger rows, reconcile the
    # broker evidence on the single order date; keep the 90-day cap/future clamp
    # as a fail-closed boundary for stale or malformed rows.
    start = max(order_date, earliest)
    if start > today:
        start = today
    return start.strftime("%Y%m%d"), start.strftime("%Y%m%d")


def _order_date_kst(row: Any) -> datetime.date | None:
    """Ledger row's order date in KST (created_at first, trade_date fallback).

    Naive timestamps are assumed KST (app/core/timezone convention). Returns
    None when underivable — callers must then refuse terminal markings
    (fail-closed) because the evidence window cannot be proven to cover the
    order date.
    """
    for attr in ("created_at", "trade_date"):
        dt = getattr(row, attr, None)
        if isinstance(dt, datetime.datetime):
            if dt.tzinfo is None:
                return dt.date()
            return dt.astimezone(KST).date()
    return None


async def _fetch_live_daily_rows(
    *,
    symbol: str,
    order_no: str | None,
    order_trade_date: datetime.datetime | datetime.date | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch live daily-execution rows for a KR order (is_mock=False).

    ROB-487: TTTC8001R is ORDER-DATE-windowed — a today-only window returns
    zero rows for prior-day orders (live-verified 2026-06-10: the 20260610
    window contained none of the 6/9 orders). Callers must pass the ledger
    row's order date as ``order_trade_date`` so next-day reconciles can still
    see prior-day fills. Follow-up live smoke showed that a broad
    order_date..today range can fail with KIER2570; known ledger rows therefore
    query the exact order date (start_date == end_date == order_date), bounded
    by the 90-day cap in ``_live_daily_order_window``.
    """
    kis = _create_live_kis_client()
    start_date, end_date = _live_daily_order_window(order_trade_date)
    rows = await kis.inquire_daily_order_domestic(
        start_date=start_date,
        end_date=end_date,
        stock_code=symbol,
        order_number=order_no or "",
        is_mock=False,
    )
    return rows or []


async def _update_ledger_outcome(
    *,
    ledger_id: int,
    status: str,
    filled_qty: Decimal | None = None,
    avg_fill_price: Decimal | None = None,
    trade_id: int | None = None,
    journal_id: int | None = None,
) -> None:
    """Update a ledger row's reconcile outcome (status + fill + linkage)."""
    try:
        async with _order_session_factory()() as db:
            values: dict[str, Any] = {
                "status": status,
                "lifecycle_state": _status_to_lifecycle(status),
                "reconciled_at": now_kst(),
            }
            if filled_qty is not None:
                values["filled_qty"] = filled_qty
            if avg_fill_price is not None:
                values["avg_fill_price"] = avg_fill_price
            if trade_id is not None:
                values["trade_id"] = trade_id
            if journal_id is not None:
                values["journal_id"] = journal_id
            await db.execute(
                update(KISLiveOrderLedger)
                .where(KISLiveOrderLedger.id == ledger_id)
                .values(**values)
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "Failed to update kis_live ledger outcome id=%s: %s", ledger_id, exc
        )


async def _mark_ledger_cancelled(order_no: str | None) -> int:
    """Mark a non-terminal live ledger row as cancelled (proactive on cancel).

    Idempotent: only touches accepted/pending/partial rows for ``order_no`` so a
    later reconcile cannot reopen it. Returns the number of rows updated.
    """
    if not order_no:
        return 0
    try:
        async with _order_session_factory()() as db:
            result = await db.execute(
                update(KISLiveOrderLedger)
                .where(
                    KISLiveOrderLedger.order_no == order_no,
                    KISLiveOrderLedger.status.in_(("accepted", "pending", "partial")),
                )
                .values(
                    status="cancelled",
                    lifecycle_state=_status_to_lifecycle("cancelled"),
                    reconciled_at=now_kst(),
                )
            )
            await db.commit()
            return result.rowcount or 0
    except Exception as exc:
        logger.warning(
            "Failed to mark kis_live ledger cancelled order_no=%s: %s", order_no, exc
        )
        return 0


async def _repoint_ledger_after_modify(
    *,
    old_order_no: str | None,
    new_order_no: str | None,
    new_price: float | None = None,
    new_quantity: float | None = None,
) -> int:
    """Re-point a live ledger row to a modified order's new order_no.

    KIS 정정주문 issues a fresh odno, so keep the captured intent attached to the
    live order — otherwise reconcile would mark the old order cancelled and lose
    track of the replacement. Updates price/quantity only when supplied. Returns
    the number of rows updated.
    """
    if not old_order_no or not new_order_no:
        return 0
    values: dict[str, Any] = {"order_no": new_order_no}
    if new_price is not None:
        values["price"] = new_price
    if new_quantity is not None:
        values["quantity"] = new_quantity
    try:
        async with _order_session_factory()() as db:
            result = await db.execute(
                update(KISLiveOrderLedger)
                .where(
                    KISLiveOrderLedger.order_no == old_order_no,
                    KISLiveOrderLedger.status.in_(("accepted", "pending", "partial")),
                )
                .values(**values)
            )
            await db.commit()
            return result.rowcount or 0
    except Exception as exc:
        logger.warning(
            "Failed to repoint kis_live ledger %s->%s: %s",
            old_order_no,
            new_order_no,
            exc,
        )
        return 0


async def _load_ledger_row(ledger_id: int) -> KISLiveOrderLedger:
    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(KISLiveOrderLedger).where(KISLiveOrderLedger.id == ledger_id)
            )
        ).scalar_one()
        db.expunge(row)
        return row


async def _list_open_ledger_rows(
    *, symbol: str | None, order_no: str | None, limit: int
) -> list[KISLiveOrderLedger]:
    """Non-terminal live ledger rows (accepted/pending) needing reconcile."""
    async with _order_session_factory()() as db:
        stmt = select(KISLiveOrderLedger).where(
            KISLiveOrderLedger.status.in_(("accepted", "pending", "partial"))
        )
        if symbol:
            stmt = stmt.where(KISLiveOrderLedger.symbol == symbol)
        if order_no:
            stmt = stmt.where(KISLiveOrderLedger.order_no == order_no)
        stmt = stmt.order_by(KISLiveOrderLedger.created_at.asc()).limit(limit)
        rows = list((await db.execute(stmt)).scalars().all())
        for r in rows:
            db.expunge(r)
        return rows


async def _reconcile_one_ledger_row(
    row: KISLiveOrderLedger, *, dry_run: bool
) -> dict[str, Any]:
    """Classify one accepted/pending order and apply journal mutation if filled.

    Pending -> noop, or expired/cancelled on broker evidence after NXT close
    (20:00 KST). NONE verdict -> fail-closed noop_no_evidence with
    requires_manual_review (missing evidence is never cancellation evidence).
    Filled/partial -> delta-idempotent booking from BROKER-confirmed qty/price.
    """
    order_no = row.order_no
    order_date = _order_date_kst(row)
    rows = await _fetch_live_daily_rows(
        symbol=row.symbol,
        order_no=order_no,
        order_trade_date=order_date or getattr(row, "trade_date", None),
    )
    evidence = classify_fill_evidence(order_no=order_no, rows=rows)

    base = {
        "ledger_id": row.id,
        "order_id": order_no,
        "symbol": row.symbol,
        "side": row.side,
        "verdict": str(evidence.verdict),
        "filled_qty": float(evidence.filled_qty)
        if evidence.filled_qty is not None
        else None,
        "avg_price": float(evidence.avg_price)
        if evidence.avg_price is not None
        else None,
    }

    if evidence.verdict == FillVerdict.PENDING:
        # ROB-487: SOR day order는 NXT 마감(20:00 KST)까지 살아있다. KRX 전용
        # 주문도 20:00까지 보수적으로 대기 — evidence-first booking이라 늦은
        # terminal 마킹은 무해하고, 조기 마킹(6/9 19:02 사례)은 유해하다.
        nxt_closed = order_date is not None and nxt_session_closed(
            order_date=order_date, now=now_kst()
        )
        expiry = classify_day_order_expiry(
            rows=rows, order_no=order_no, nxt_closed=nxt_closed
        )
        if expiry in ("expired", "cancelled"):
            base["verdict"] = expiry
            base["action"] = (
                f"marked_{expiry}" if not dry_run else f"would_mark_{expiry}"
            )
            if not dry_run:
                await _update_ledger_outcome(ledger_id=row.id, status=expiry)
            return base
        base["action"] = "noop_pending"
        return base

    if evidence.verdict == FillVerdict.NONE:
        # Missing evidence is not cancellation evidence (absence-as-evidence
        # 금지). True cancels leave positive broker rows and resolve in the
        # PENDING branch via classify_day_order_expiry. Leave the ledger open
        # so tomorrow's reconcile or operator review can still recover fills.
        base["action"] = "noop_no_evidence"
        base["requires_manual_review"] = True
        base["reason"] = (
            "no broker fill evidence in lookback window; ledger left open "
            "instead of marking cancelled"
        )
        return base

    # FILLED or PARTIAL — broker-confirmed values only. Booking is
    # delta-idempotent (ROB-407 커널 패턴): the broker reports *cumulative*
    # filled qty, so repeated reconciles of the same partial row must not
    # re-create journals or double-close sells.
    broker_cum = evidence.filled_qty or Decimal("0")
    already_booked = getattr(row, "filled_qty", None) or Decimal("0")
    delta_qty = broker_cum - already_booked
    avg_price = evidence.avg_price or Decimal("0")
    new_status = "filled" if evidence.verdict == FillVerdict.FILLED else "partial"
    base["delta_qty"] = float(delta_qty)

    if delta_qty <= 0:
        base["action"] = "noop_already_booked"
        if not dry_run:
            await _update_ledger_outcome(
                ledger_id=row.id,
                status=new_status,
                filled_qty=broker_cum,
                avg_fill_price=avg_price,
            )
        return base

    if dry_run:
        base["action"] = f"would_book_{new_status}"
        return base

    trade_id = await _save_order_fill(
        symbol=row.symbol,
        instrument_type=row.instrument_type,
        side=row.side,
        price=float(avg_price),
        quantity=float(delta_qty),
        total_amount=float(avg_price) * float(delta_qty),
        fee=float(row.fee or 0.0),
        currency=row.currency or "KRW",
        account="kis",
        order_id=order_no,
    )

    journal_id: int | None = getattr(row, "journal_id", None)
    if row.side == "buy" and journal_id is None:
        buy_preview = {
            "price": float(avg_price),
            "quantity": float(broker_cum),
            "estimated_value": float(avg_price) * float(broker_cum),
        }
        journal_result = await _create_trade_journal_for_buy(
            symbol=row.symbol,
            market_type=row.instrument_type,
            preview=buy_preview,
            thesis=(row.thesis or "").strip() or "reconciled fill",
            strategy=(row.strategy or "").strip() or "reconciled fill",
            target_price=float(row.target_price)
            if row.target_price is not None
            else None,
            stop_loss=float(row.stop_loss) if row.stop_loss is not None else None,
            min_hold_days=row.min_hold_days,
            notes=row.notes,
            indicators_snapshot=row.indicators_snapshot,
            account_type="live",
            account="kis",
        )
        journal_id = journal_result.get("journal_id")
        if trade_id and journal_id:
            await _link_journal_to_fill(
                row.symbol, trade_id, account_type="live", account="kis"
            )
    elif row.side == "sell":
        close_result = await _close_journals_on_sell(
            symbol=row.symbol,
            sell_quantity=float(delta_qty),
            sell_price=float(avg_price),
            exit_reason=row.exit_reason or row.reason,
            account_type="live",
            account="kis",
        )
        base["journals_closed"] = close_result["journals_closed"]
        base["closed_journal_ids"] = close_result["closed_ids"]
        base["realized_pnl_pct"] = close_result["total_pnl_pct"]
        # ROB-544: label the basis. realized_pnl_pct is the FIFO lot /
        # journal-entry basis (per-lot entry_price), NOT the account-average
        # pchs_avg_pric shown in place_order preview / get_holdings.
        base["realized_pnl_basis"] = close_result.get(
            "realized_pnl_basis", "journal_entry"
        )
        # Explicit alias so the journal-entry semantics are unambiguous to
        # consumers that already read realized_pnl_pct for back-compat.
        base["journal_pnl_pct"] = close_result["total_pnl_pct"]

    await _update_ledger_outcome(
        ledger_id=row.id,
        status=new_status,
        filled_qty=broker_cum,
        avg_fill_price=avg_price,
        trade_id=trade_id,
        journal_id=journal_id,
    )
    base["action"] = f"booked_{new_status}"
    base["trade_id"] = trade_id
    base["journal_id"] = journal_id
    return base


async def kis_live_reconcile_orders_impl(
    *,
    symbol: str | None = None,
    order_id: str | None = None,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """Reconcile accepted/pending live KR orders against broker fill evidence."""
    try:
        rows = await _list_open_ledger_rows(
            symbol=symbol, order_no=order_id, limit=limit
        )
    except Exception as exc:
        logger.exception("Failed to list open kis_live ledger rows: %s", exc)
        return {
            "success": False,
            "error": str(exc) or exc.__class__.__name__,
            "account_mode": "kis_live",
        }

    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        try:
            outcome = await _reconcile_one_ledger_row(row, dry_run=dry_run)
        except Exception as exc:
            logger.warning("reconcile failed for order_no=%s: %s", row.order_no, exc)
            outcome = {
                "ledger_id": row.id,
                "order_id": row.order_no,
                "verdict": "anomaly",
                "error": str(exc) or exc.__class__.__name__,
            }
        reconciled.append(outcome)
        verdict = str(outcome.get("verdict", "anomaly"))
        counts[verdict] = counts.get(verdict, 0) + 1

    if rows:
        message = (
            f"Reconciled {len(reconciled)} live order(s) (dry_run={dry_run}): {counts}"
        )
    else:
        # ROB-487 UX: 후보 0건(모든 ledger 행이 terminal)을 누락과 구분해 표기.
        message = (
            "No open candidates (all ledger rows terminal) — nothing to reconcile "
            f"(dry_run={dry_run})"
        )
    return {
        "success": True,
        "account_mode": "kis_live",
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "message": message,
    }


async def list_kis_live_orders_by_report_item_uuid(
    report_item_uuid: uuid.UUID,
) -> list[dict[str, Any]]:
    """ROB-473 — live KR orders linked to a report item (audit).

    ROB-554 — projects via the shared LinkedOrderView (account_mode ->
    account_scope, market="kr") so KR and US/crypto share one field mapping.
    """
    from app.services.investment_reports.linked_orders import (
        project_kis_live_order,
    )

    async with _order_session_factory()() as db:
        rows = (
            (
                await db.execute(
                    select(KISLiveOrderLedger)
                    .where(KISLiveOrderLedger.report_item_uuid == report_item_uuid)
                    .order_by(KISLiveOrderLedger.id.desc())
                )
            )
            .scalars()
            .all()
        )
    return [project_kis_live_order(r).model_dump(mode="json") for r in rows]
