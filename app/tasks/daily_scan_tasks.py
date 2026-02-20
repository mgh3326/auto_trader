from __future__ import annotations

from app.core.task_lock import run_with_task_lock
from app.core.taskiq_broker import broker
from app.jobs.daily_scan import DailyScanner

STRATEGY_SCAN_LOCK_KEY = "auto-trader:task-lock:scan.strategy"
CRASH_DETECTION_LOCK_KEY = "auto-trader:task-lock:scan.crash_detection"
STRATEGY_SCAN_LOCK_TTL_SECONDS = 5400
CRASH_DETECTION_LOCK_TTL_SECONDS = 5400


async def _run_strategy_scan() -> dict:
    scanner = DailyScanner(alert_mode="telegram_only")
    try:
        return await scanner.run_strategy_scan()
    finally:
        await scanner.close()


async def _run_crash_detection() -> dict:
    scanner = DailyScanner(alert_mode="telegram_only")
    try:
        return await scanner.run_crash_detection()
    finally:
        await scanner.close()


@broker.task(
    task_name="scan.strategy",
    schedule=[
        {"cron": "30 * * * *", "cron_offset": "Asia/Seoul"},
    ],
)
async def run_strategy_scan_task() -> dict:
    return await run_with_task_lock(
        lock_key=STRATEGY_SCAN_LOCK_KEY,
        ttl_seconds=STRATEGY_SCAN_LOCK_TTL_SECONDS,
        coro_factory=_run_strategy_scan,
    )


@broker.task(
    task_name="scan.crash_detection",
    schedule=[{"cron": "0 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_crash_detection_task() -> dict:
    return await run_with_task_lock(
        lock_key=CRASH_DETECTION_LOCK_KEY,
        ttl_seconds=CRASH_DETECTION_LOCK_TTL_SECONDS,
        coro_factory=_run_crash_detection,
    )
