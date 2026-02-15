from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.daily_scan import DailyScanner


@broker.task(
    task_name="scan.strategy",
    schedule=[
        {"cron": "30 9 * * *", "cron_offset": "Asia/Seoul"},
        {"cron": "0 15 * * *", "cron_offset": "Asia/Seoul"},
        {"cron": "0 21 * * *", "cron_offset": "Asia/Seoul"},
    ],
)
async def run_strategy_scan_task() -> dict:
    scanner = DailyScanner()
    try:
        return await scanner.run_strategy_scan()
    finally:
        await scanner.close()


@broker.task(
    task_name="scan.crash_detection",
    schedule=[{"cron": "0 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_crash_detection_task() -> dict:
    scanner = DailyScanner()
    try:
        return await scanner.run_crash_detection()
    finally:
        await scanner.close()
