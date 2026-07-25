"""ROB-1050 — Read-only observation metrics for unreconciled live order ledger entries."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
from app.models.review import KISLiveOrderLedger, LiveOrderLedger

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("accepted", "submitted", "pending")


def _row_timestamp(row: Any) -> datetime | None:
    ts = getattr(row, "created_at", None) or getattr(row, "trade_date", None)
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


async def get_unreconciled_live_order_metrics(
    db: AsyncSession | None = None,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Calculate age-bracketed metrics for unreconciled live orders across all markets."""
    ref_time = as_of or datetime.now(UTC)
    if ref_time.tzinfo is None:
        ref_time = ref_time.replace(tzinfo=UTC)
    else:
        ref_time = ref_time.astimezone(UTC)

    if db is not None:
        return await _compute_metrics(db, ref_time)

    async with _order_session_factory()() as session:
        return await _compute_metrics(session, ref_time)


async def _compute_metrics(db: AsyncSession, ref_time: datetime) -> dict[str, Any]:
    live_stmt = select(LiveOrderLedger).where(
        LiveOrderLedger.reconciled_at.is_(None),
        LiveOrderLedger.status.in_(OPEN_STATUSES),
    )
    kis_stmt = select(KISLiveOrderLedger).where(
        KISLiveOrderLedger.reconciled_at.is_(None),
        KISLiveOrderLedger.status.in_(OPEN_STATUSES),
    )

    live_rows = list((await db.execute(live_stmt)).scalars().all())
    kis_rows = list((await db.execute(kis_stmt)).scalars().all())

    all_items: list[tuple[str, str, datetime]] = []

    for r in live_rows:
        market = str(r.market).lower() if r.market else "unknown"
        broker = str(r.broker).lower() if r.broker else "unknown"
        ts = _row_timestamp(r)
        if ts:
            all_items.append((market, broker, ts))

    for r in kis_rows:
        market = "kr"
        broker = str(r.broker).lower() if r.broker else "kis"
        ts = _row_timestamp(r)
        if ts:
            all_items.append((market, broker, ts))

    by_market_broker: dict[str, dict[str, int]] = {}
    total_1h = 0
    total_24h = 0
    total_72h = 0

    for market, broker, ts in all_items:
        key = f"{market}:{broker}"
        if key not in by_market_broker:
            by_market_broker[key] = {
                "total": 0,
                "1h_plus": 0,
                "24h_plus": 0,
                "72h_plus": 0,
            }

        age_seconds = (ref_time - ts).total_seconds()
        by_market_broker[key]["total"] += 1

        if age_seconds >= 3600:
            by_market_broker[key]["1h_plus"] += 1
            total_1h += 1
        if age_seconds >= 86400:
            by_market_broker[key]["24h_plus"] += 1
            total_24h += 1
        if age_seconds >= 259200:
            by_market_broker[key]["72h_plus"] += 1
            total_72h += 1

    return {
        "as_of": ref_time.isoformat(),
        "total_unreconciled": len(all_items),
        "by_market_broker": by_market_broker,
        "by_bracket": {
            "1h_plus": total_1h,
            "24h_plus": total_24h,
            "72h_plus": total_72h,
        },
    }
