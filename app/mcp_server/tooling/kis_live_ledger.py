"""ROB-395 — KIS live order ledger writes + reconciliation.

SEND records accepted/rejected only (no trades/journal/realized_pnl). RECONCILE
applies journal mutations from order-id-keyed broker fill evidence. Fully
isolated from the mock ledger (kis_live_order_ledger vs kis_mock_order_ledger).
"""

from __future__ import annotations

import datetime

from decimal import Decimal, InvalidOperation
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.models.review import KISLiveOrderLedger

# lifecycle_state mirrors status for live (no separate mock shadow semantics)
_STATUS_TO_LIFECYCLE: dict[str, str] = {
    "accepted": "accepted",
    "rejected": "failed",
    "unknown": "anomaly",
    "filled": "filled",
    "partial": "partial",
    "pending": "accepted",
    "cancelled": "cancelled",
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


def _today_yyyymmdd() -> str:
    return datetime.datetime.now().strftime("%Y%m%d")


async def _fetch_live_daily_rows(
    *, symbol: str, order_no: str | None
) -> list[dict[str, Any]]:
    """Fetch today's live daily-execution rows for a KR order (is_mock=False)."""
    kis = _create_live_kis_client()
    today = _today_yyyymmdd()
    rows = await kis.inquire_daily_order_domestic(
        start_date=today,
        end_date=today,
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
            await db.execute(
                update(KISLiveOrderLedger)
                .where(KISLiveOrderLedger.id == ledger_id)
                .values(
                    status=status,
                    lifecycle_state=_status_to_lifecycle(status),
                    filled_qty=filled_qty,
                    avg_fill_price=avg_fill_price,
                    trade_id=trade_id,
                    journal_id=journal_id,
                    reconciled_at=now_kst(),
                )
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to update kis_live ledger outcome id=%s: %s", ledger_id, exc)




