from __future__ import annotations

from app.core.task_lock import run_with_task_lock
from app.core.taskiq_broker import broker
from app.jobs.watch_scanner import WatchScanner

WATCH_ALERTS_LOCK_KEY = "auto-trader:task-lock:scan.watch_alerts"
WATCH_ALERTS_LOCK_TTL_SECONDS = 300


async def _run_watch_scan() -> dict:
    scanner = WatchScanner()
    try:
        return await scanner.run()
    finally:
        await scanner.close()


@broker.task(
    task_name="scan.watch_alerts",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_watch_scan_task() -> dict:
    return await run_with_task_lock(
        lock_key=WATCH_ALERTS_LOCK_KEY,
        ttl_seconds=WATCH_ALERTS_LOCK_TTL_SECONDS,
        coro_factory=_run_watch_scan,
    )
