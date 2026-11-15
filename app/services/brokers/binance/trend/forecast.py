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
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.review import TradeForecast
from app.services.trade_journal.forecast_service import (
    get_forecast,
    resolve_forecast,
    save_forecast,
)

from .constants import IDENTITY, TIME_EXIT_DAYS
from .state import TrendSignalSnapshot

logger = logging.getLogger(__name__)


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


def _proven_fill(signal: TrendSignalSnapshot) -> bool:
    return (
        signal.entry_qty > 0
        and signal.entry_price is not None
        and signal.state in {"holding", "closed"}
    )


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
    if not _proven_fill(signal):
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


async def repair_forecasts(
    signals: Sequence[TrendSignalSnapshot], *, now: dt.datetime
) -> tuple[dict[str, str], list[tuple[TrendSignalSnapshot, Exception]]]:
    """Batch ``repair_forecast`` for the per-tick recovery scan.

    One session and one ``review.trade_forecasts`` SELECT cover every
    candidate, so per-tick DB work no longer grows with the lane's closed
    history the way a per-signal session would. Per-signal failures are
    isolated into the returned list — this function never raises for a
    single signal's write, and the batch lookup failure fails every candidate
    the same way (the caller logs and retries next tick).
    """
    candidates = [signal for signal in signals if _proven_fill(signal)]
    if not candidates:
        return {}, []
    fids = {signal.signal_key: trend_forecast_id(signal) for signal in candidates}
    results: dict[str, str] = {}
    failures: list[tuple[TrendSignalSnapshot, Exception]] = []
    try:
        async with AsyncSessionLocal() as db:
            existing = {
                row.forecast_id: row.status
                for row in (
                    await db.scalars(
                        select(TradeForecast).where(
                            TradeForecast.forecast_id.in_(list(set(fids.values())))
                        )
                    )
                ).all()
            }
            for signal in candidates:
                fid = fids[signal.signal_key]
                row_status = existing.get(fid)
                try:
                    saved_now = False
                    if row_status is None:
                        await _save(db, signal, fid, now=now)
                        await db.commit()
                        row_status = "open"
                        saved_now = True
                    if signal.state == "closed" and row_status == "open":
                        await _resolve(db, signal, fid, now=now)
                        await db.commit()
                        results[signal.signal_key] = "resolved"
                    elif saved_now:
                        results[signal.signal_key] = "saved"
                    elif row_status == "open":
                        results[signal.signal_key] = "open"
                    else:
                        results[signal.signal_key] = "already_closed"
                except Exception as exc:
                    failures.append((signal, exc))
    except Exception as exc:
        logger.exception("trend forecast recovery lookup failed")
        return {}, [(signal, exc) for signal in candidates]
    return results, failures


__all__ = [
    "ensure_entry_forecast",
    "repair_forecast",
    "repair_forecasts",
    "resolve_held_outcome",
    "trend_forecast_id",
]
