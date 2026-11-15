"""TREND-TSMOM-v1 forecast lifecycle: fill claim now, held outcome on close.

Mirrors the H5 adapter (``app.services.brokers.binance.h5.forecast``): the
forecast id is deterministic — ``uuid5`` of the signal correlation id — the
entry forecast is saved only after a broker-proven entry fill, and the held
outcome is resolved only when the managed position is actually closed. The
trend state tables carry no forecast columns, so the ``review.trade_forecasts``
row itself is the idempotency record: an existing row means the save already
happened and a non-``open`` status means the resolve already happened.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.db import AsyncSessionLocal
from app.services.trade_journal.forecast_service import (
    get_forecast,
    resolve_forecast,
    save_forecast,
)

from .constants import IDENTITY, TIME_EXIT_DAYS
from .state import TrendSignalSnapshot


def trend_forecast_id(signal: TrendSignalSnapshot) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, signal.correlation_id)


async def _save(
    db, signal: TrendSignalSnapshot, fid: uuid.UUID, *, now: dt.datetime
) -> None:
    await save_forecast(
        db,
        created_by="trend-tsmom-v1",
        symbol="USDT-" + signal.symbol.removesuffix("USDT"),
        instrument_type="crypto",
        forecast_id=fid,
        forecast_target={
            "kind": "trend_held_outcome",
            "signal_key": signal.signal_key,
            "strategy_id": IDENTITY,
            "side": signal.side,
            "entry_price": format(signal.entry_price, "f"),
            "entry_qty": format(signal.entry_qty, "f"),
            "outcome": "pending_actual_held_trade",
        },
        probability=0.5,
        review_date=(now + dt.timedelta(days=TIME_EXIT_DAYS)).date(),
        horizon=f"{TIME_EXIT_DAYS}d held position",
        correlation_id=signal.correlation_id,
    )


async def _resolve(
    db, signal: TrendSignalSnapshot, fid: uuid.UUID, *, now: dt.datetime
) -> dict:
    net_pnl = signal.realized_pnl_usdt - signal.fees_usdt
    result = await resolve_forecast(
        db,
        forecast_id=fid,
        persist=True,
        manual_outcome=net_pnl > 0,
        manual_observed_value=float(net_pnl),
        manual_evidence={
            "source": "TREND-TSMOM-v1 durable signal and broker order-id evidence",
            "signal_key": signal.signal_key,
            "correlation_id": signal.correlation_id,
            "entry_client_order_id": signal.entry_client_order_id,
            "entry_qty": format(signal.entry_qty, "f"),
            "closed_qty": format(signal.closed_qty, "f"),
            "realized_pnl_usdt": format(signal.realized_pnl_usdt, "f"),
            "fees_usdt": format(signal.fees_usdt, "f"),
            "exit_reason": signal.exit_reason,
            "exit_at": signal.exit_at.isoformat() if signal.exit_at else None,
        },
        now=now,
        backfill_missing=False,
    )
    if result.get("status") not in {"resolved", "already_closed"}:
        raise ValueError("TREND-TSMOM-v1 held-outcome forecast was not resolved")
    return result


async def ensure_entry_forecast(
    signal: TrendSignalSnapshot, *, now: dt.datetime
) -> str:
    """Save the entry forecast once, after a broker-proven entry fill.

    Idempotent: the deterministic forecast id means an existing
    ``review.trade_forecasts`` row — open or already resolved — is the record
    that the save already happened, so a re-run writes nothing new.
    """
    if signal.entry_qty <= 0 or signal.entry_price is None:
        raise ValueError("trend forecast requires broker-proven entry fill")
    fid = trend_forecast_id(signal)
    async with AsyncSessionLocal() as db:
        if await get_forecast(db, fid) is None:
            await _save(db, signal, fid, now=now)
            await db.commit()
    return str(fid)


async def resolve_held_outcome(
    signal: TrendSignalSnapshot, *, now: dt.datetime
) -> dict:
    """Resolve the held-outcome forecast on the actual close.

    Idempotent through ``resolve_forecast`` itself: a forecast that is no
    longer ``open`` comes back ``already_closed`` and is never re-scored.
    """
    if signal.state != "closed":
        raise ValueError("trend forecast resolution requires actual closed holding")
    fid = trend_forecast_id(signal)
    async with AsyncSessionLocal() as db:
        if await get_forecast(db, fid) is None:
            raise ValueError("trend held outcome has no entry forecast")
        result = await _resolve(db, signal, fid, now=now)
        await db.commit()
    return result


async def repair_forecast(
    signal: TrendSignalSnapshot, *, now: dt.datetime
) -> str | None:
    """Retry a missed save or resolve for one proven-fill signal.

    Runs in the manage section of every tick (and therefore in manage-only
    ticks): a signal that reached ``holding``/``closed`` with a settled entry
    but whose forecast write failed earlier is repaired here. Failures raise
    to the caller, which logs and continues — they never change a trading
    decision or an order.
    """
    if (
        signal.entry_qty <= 0
        or signal.entry_price is None
        or signal.state not in {"holding", "closed"}
    ):
        return None
    fid = trend_forecast_id(signal)
    async with AsyncSessionLocal() as db:
        row = await get_forecast(db, fid)
        if row is None:
            await _save(db, signal, fid, now=now)
            await db.commit()
            status = "open"
        else:
            status = row.status
        if signal.state == "closed" and status == "open":
            await _resolve(db, signal, fid, now=now)
            await db.commit()
            return "resolved"
        if row is None:
            return "saved"
        return "open" if status == "open" else "already_closed"


__all__ = [
    "ensure_entry_forecast",
    "repair_forecast",
    "resolve_held_outcome",
    "trend_forecast_id",
]
