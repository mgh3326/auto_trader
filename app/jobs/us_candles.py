from __future__ import annotations

import logging

from app.services.us_candles_sync_service import sync_us_candles

logger = logging.getLogger(__name__)


async def run_us_candles_sync(
    *,
    mode: str,
    sessions: int = 10,
    user_id: int = 1,
) -> dict[str, object]:
    try:
        result = await sync_us_candles(mode=mode, sessions=sessions, user_id=user_id)
        return {
            "status": "completed",
            **result,
        }
    except Exception as exc:
        logger.error("US candles sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "mode": mode,
            "error": str(exc),
        }
