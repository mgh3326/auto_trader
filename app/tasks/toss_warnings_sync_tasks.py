from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.toss_warnings import run_toss_warnings_sync

logger = logging.getLogger(__name__)


# ROB-550: shipped scheduleless. Production recurrence is operator/Prefect-
# registered (house convention, e.g. robin-prefect-automations) so enabling
# TOSS_API_ENABLED does not silently auto-start a daily batch. Suggested
# cadence when registered: "30 7 * * *" Asia/Seoul.
@broker.task(task_name="warnings.toss.sync")
async def sync_toss_warnings_task() -> dict[str, int | str | list[str]]:
    try:
        return await run_toss_warnings_sync()
    except Exception as exc:
        logger.error("TaskIQ Toss warnings sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }
