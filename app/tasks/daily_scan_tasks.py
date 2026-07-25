from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.daily_scan import DailyScanner

# NOTE: TaskIQ schedules removed. These scan tasks are kept as manual entry
# points (e.g. taskiq kick) but are no longer auto-scheduled. (The former n8n
# HTTP scan surface that triggered them has been retired.)


@broker.task(task_name="scan.strategy")
async def run_strategy_scan_task() -> dict:
    scanner = DailyScanner(alert_mode="telegram_only")
    try:
        return await scanner.run_strategy_scan()
    finally:
        await scanner.close()


@broker.task(task_name="scan.crash_detection")
async def run_crash_detection_task() -> dict:
    scanner = DailyScanner(alert_mode="telegram_only")
    try:
        return await scanner.run_crash_detection()
    finally:
        await scanner.close()
