from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.upbit_symbol_universe import run_upbit_symbol_universe_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="symbols.upbit.universe.sync",
    schedule=[{"cron": "15 6 * * *", "cron_offset": "Asia/Seoul"}],
)
async def sync_upbit_symbol_universe_task() -> dict[str, int | str]:
    try:
        return await run_upbit_symbol_universe_sync()
    except Exception as exc:
        logger.error("TaskIQ Upbit symbol universe sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }
