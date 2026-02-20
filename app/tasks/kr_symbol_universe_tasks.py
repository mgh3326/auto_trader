from __future__ import annotations

import logging

from app.core.task_lock import run_with_task_lock
from app.core.taskiq_broker import broker
from app.jobs.kr_symbol_universe import run_kr_symbol_universe_sync

logger = logging.getLogger(__name__)

KR_SYMBOL_UNIVERSE_LOCK_KEY = "auto-trader:task-lock:symbols.kr.universe.sync"
KR_SYMBOL_UNIVERSE_LOCK_TTL_SECONDS = 7200


async def _run_kr_symbol_universe_sync_task_body() -> dict[str, int | str]:
    return await run_kr_symbol_universe_sync()


@broker.task(
    task_name="symbols.kr.universe.sync",
    schedule=[{"cron": "10 7 * * *", "cron_offset": "Asia/Seoul"}],
)
async def sync_kr_symbol_universe_task() -> dict[str, object]:
    try:
        return await run_with_task_lock(
            lock_key=KR_SYMBOL_UNIVERSE_LOCK_KEY,
            ttl_seconds=KR_SYMBOL_UNIVERSE_LOCK_TTL_SECONDS,
            coro_factory=_run_kr_symbol_universe_sync_task_body,
        )
    except Exception as exc:
        logger.error("TaskIQ KR symbol universe sync failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "error": str(exc),
        }
