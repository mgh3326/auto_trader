from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.kr_candles import run_kr_candles_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="candles.kr.sync",
    schedule=[{"cron": "*/1 * * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def sync_kr_candles_incremental_task() -> dict[str, object]:
    try:
        return await run_kr_candles_sync(mode="incremental")
    except Exception as exc:
        logger.error("TaskIQ KR candles sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "mode": "incremental",
            "error": str(exc),
        }
