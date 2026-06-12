from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.toss_warnings import run_toss_warnings_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="warnings.toss.sync",
    schedule=[{"cron": "30 7 * * *", "cron_offset": "Asia/Seoul"}],
)
async def sync_toss_warnings_task() -> dict[str, int | str | list[str]]:
    try:
        return await run_toss_warnings_sync()
    except Exception as exc:
        logger.error("TaskIQ Toss warnings sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }
