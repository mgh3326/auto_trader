"""H5-specific forecast lifecycle: fill claim now, held outcome on close."""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.db import AsyncSessionLocal
from app.services.trade_journal.forecast_service import resolve_forecast, save_forecast

from .state import H5SignalSnapshot, H5StateService


def h5_forecast_id(signal: H5SignalSnapshot) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, signal.correlation_id)


async def ensure_entry_forecast(
    signal: H5SignalSnapshot, state: H5StateService, *, now: dt.datetime
) -> str:
    if signal.entry_qty <= 0 or signal.entry_price is None:
        raise ValueError("H5 forecast requires broker-proven entry fill")
    fid = h5_forecast_id(signal)
    symbol = "USDT-" + signal.symbol.removesuffix("USDT")
    async with AsyncSessionLocal() as db:
        await save_forecast(
            db,
            created_by="h5-ls-env-v1",
            symbol=symbol,
            instrument_type="crypto",
            forecast_id=fid,
            forecast_target={
                "kind": "h5_held_outcome",
                "signal_key": signal.signal_key,
                "strategy_id": "H5-LS-ENV-v1",
                "side": signal.side,
                "entry_price": format(signal.entry_price, "f"),
                "entry_qty": format(signal.entry_qty, "f"),
                "outcome": "pending_actual_held_trade",
            },
            probability=0.5,
            review_date=(now + dt.timedelta(days=2)).date(),
            horizon="24h held position",
            correlation_id=signal.correlation_id,
        )
        await db.commit()
    await state.mark_forecast_id(signal.signal_key, str(fid), now=now)
    return str(fid)


async def resolve_held_outcome(signal: H5SignalSnapshot, *, now: dt.datetime) -> dict:
    if signal.state != "closed" or not signal.forecast_id:
        raise ValueError("H5 forecast resolution requires actual closed holding")
    net_pnl = signal.realized_pnl_usdt - signal.fees_usdt
    async with AsyncSessionLocal() as db:
        result = await resolve_forecast(
            db,
            forecast_id=signal.forecast_id,
            persist=True,
            manual_outcome=net_pnl > 0,
            manual_observed_value=float(net_pnl),
            manual_evidence={
                "source": "H5 durable signal and broker order-id evidence",
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
            raise ValueError("H5 held-outcome forecast was not resolved")
        await db.commit()
    return result
