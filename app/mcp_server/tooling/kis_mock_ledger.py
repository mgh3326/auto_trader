"""KIS mock order ledger writes — fully isolated from live journal/fill paths."""

from __future__ import annotations

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
                    fee=0,
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
