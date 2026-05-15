"""Daily candle sync entry point used by TaskIQ cron tasks and the backfill CLI.

Thin wrapper that builds the DailyCandleSyncService with default
dependencies and runs sync_market_universe for one market.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.daily_candles.constants import (
    DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
    DAILY_CANDLE_BACKFILL_BARS_KR,
    DAILY_CANDLE_BACKFILL_BARS_US,
)
from app.services.daily_candles.sync_service import _build_default_service

logger = logging.getLogger(__name__)

_HORIZON_BY_MARKET = {
    "kr": DAILY_CANDLE_BACKFILL_BARS_KR,
    "us": DAILY_CANDLE_BACKFILL_BARS_US,
    "crypto": DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
}


async def run_daily_candles_sync(market: str) -> dict[str, Any]:
    """Run a daily-candle sync for the given market.

    On exception, returns a dict with status='failed' rather than raising
    so the TaskIQ cron job can record a structured failure result.
    """
    if market not in _HORIZON_BY_MARKET:
        return {"status": "failed", "market": market, "error": f"unknown market: {market}"}

    horizon = _HORIZON_BY_MARKET[market]
    try:
        svc = await _build_default_service()
        result = await svc.sync_market_universe(market=market, horizon_bars=horizon)
        result["status"] = "ok"
        return result
    except Exception as exc:
        logger.error("Daily candle sync failed market=%s: %s", market, exc, exc_info=True)
        return {"status": "failed", "market": market, "error": str(exc)}
