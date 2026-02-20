from __future__ import annotations

import logging

from app.services.us_symbol_universe_service import sync_us_symbol_universe

logger = logging.getLogger(__name__)


async def run_us_symbol_universe_sync() -> dict[str, int | str]:
    try:
        result = await sync_us_symbol_universe()
        return {
            "status": "completed",
            **result,
        }
    except Exception as exc:
        logger.error("US symbol universe sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }
