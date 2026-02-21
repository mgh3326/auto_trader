from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.kr_symbol_universe import run_kr_symbol_universe_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="symbols.kr.universe.sync",
    schedule=[{"cron": "10 7 * * *", "cron_offset": "Asia/Seoul"}],
)
async def sync_kr_symbol_universe_task() -> dict[str, int | str]:
    try:
        return await run_kr_symbol_universe_sync()
    except Exception as exc:
        logger.error("TaskIQ KR symbol universe sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }
