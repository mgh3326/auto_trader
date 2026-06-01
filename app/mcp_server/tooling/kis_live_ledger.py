"""ROB-395 — KIS live order ledger writes + reconciliation.

SEND records accepted/rejected only (no trades/journal/realized_pnl). RECONCILE
applies journal mutations from order-id-keyed broker fill evidence. Fully
isolated from the mock ledger (kis_live_order_ledger vs kis_mock_order_ledger).
"""

from __future__ import annotations

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

