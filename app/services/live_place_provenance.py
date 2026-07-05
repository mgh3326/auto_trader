"""Place-time forecast auto-publish for LIVE orders (ROB-714).

Mirrors the paper path (paper_limit_order_service.py:335-373): a live BUY with a
profit target auto-publishes a minimal price_target forecast keyed by
correlation_id, so /insights forecast<->retrospective join is not "structurally
dead". Buy+target only (can't fabricate a target); sells and target-less buys
carry only the correlation_id spine. Best-effort in an ISOLATED session --
a forecast hiccup must never roll back the order. Fixed defaults (D1):
probability=0.5, review_date = trade-day + N calendar days (N=min_hold_days
or 10) -- calendar-day offset keeps the order hot path network-free (ROB-671).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.trade_journal.forecast_service import save_forecast

logger = logging.getLogger(__name__)

_DEFAULT_HORIZON_DAYS = 10


async def publish_place_time_forecast(
    *,
    correlation_id: str,
    symbol: str,
    instrument_type: str,
    side: str,
    target_price: float | None,
    min_hold_days: int | None,
    session_label: str,
    created_by: str,
    report_item_uuid: str | None = None,
) -> str | None:
    if side.lower() != "buy" or target_price is None:
        return None
    horizon_days = (
        min_hold_days if (min_hold_days and min_hold_days > 0) else _DEFAULT_HORIZON_DAYS
    )
    review_date = (now_kst().date() + timedelta(days=horizon_days)).isoformat()
    try:
        async with AsyncSessionLocal() as fdb:
            _action, fc = await save_forecast(
                fdb,
                created_by=created_by,
                symbol=symbol,
                instrument_type=instrument_type,
                forecast_target={
                    "kind": "price_target",
                    "direction": "at_or_above",
                    "target_price": float(Decimal(str(target_price))),
                },
                probability=0.5,
                review_date=review_date,
                correlation_id=correlation_id,
                horizon=f"P{horizon_days}D",
                model_label=None,
                session_label=session_label,
                report_item_uuid=report_item_uuid,
            )
            fid = getattr(fc, "forecast_id", None)
            await fdb.commit()
        return str(fid) if fid is not None else None
    except Exception:
        logger.exception(
            "live place: failed to publish place-time forecast for correlation_id=%s",
            correlation_id,
        )
        return None
