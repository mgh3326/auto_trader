from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.brokers.toss.client import TossReadClient
from app.services.toss_warnings_sync_service import sync_toss_warnings

logger = logging.getLogger(__name__)


async def run_toss_warnings_sync() -> dict[str, int | str | list[str]]:
    """
    Sync job for stock warnings from Toss API to database.
    """
    # ROB-550: graceful skip when Toss is disabled so a deployment that never
    # armed TOSS_API_ENABLED does not emit a daily ERROR.
    if not bool(getattr(settings, "toss_api_enabled", False)):
        return {"status": "disabled"}
    client: TossReadClient | None = None
    try:
        client = TossReadClient.from_settings()
        async with AsyncSessionLocal() as db:
            result = await sync_toss_warnings(db=db, client=client, market="kr")

        return {
            "status": "completed",
            **result,
        }
    except Exception as exc:
        logger.error("Toss warnings sync job failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:
                logger.warning(
                    "Failed to close Toss warnings sync client: %s",
                    exc,
                    exc_info=True,
                )
