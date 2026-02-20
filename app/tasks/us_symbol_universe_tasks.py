from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.us_symbol_universe import run_us_symbol_universe_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="symbols.us.universe.sync",
    schedule=[{"cron": "30 21 * * *", "cron_offset": "Asia/Seoul"}],
)
async def sync_us_symbol_universe_task() -> dict[str, int | str]:
    try:
        return await run_us_symbol_universe_sync()
    except Exception as exc:
        logger.error("TaskIQ US symbol universe sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }
