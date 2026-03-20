from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.us_candles import run_us_candles_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="candles.us.sync",
    schedule=[{"cron": "*/10 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def sync_us_candles_incremental_task() -> dict[str, object]:
    try:
        return await run_us_candles_sync(mode="incremental")
    except Exception as exc:
        logger.error("TaskIQ US candles sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "mode": "incremental",
            "error": str(exc),
        }
