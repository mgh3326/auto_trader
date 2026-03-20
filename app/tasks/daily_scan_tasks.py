from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.daily_scan import DailyScanner

# NOTE: TaskIQ schedules removed — scanning is now driven by n8n workflows:
#   - Crypto Strategy Scan (every :30) → GET /api/n8n/scan/strategy
#   - Crash Detection Scan (every :00) → GET /api/n8n/scan/crash
# Tasks are kept as manual entry points (e.g. taskiq kick) but no longer auto-scheduled.


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
