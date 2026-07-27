from __future__ import annotations

import logging

from app.services.kr_symbol_universe_service import sync_kr_symbol_universe

logger = logging.getLogger(__name__)


async def run_kr_symbol_universe_sync(
    *,
    dry_run: bool = False,
) -> dict[str, int | str | bool]:
    try:
        result = await sync_kr_symbol_universe(dry_run=dry_run)
        payload: dict[str, int | str | bool] = {
            "status": "completed",
            **result,
        }
        if dry_run:
            payload["dry_run"] = True
        return payload
    except Exception as exc:
        logger.error("KR symbol universe sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
            **({"dry_run": True} if dry_run else {}),
        }
