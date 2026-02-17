from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.watch_scanner import WatchScanner


@broker.task(
    task_name="scan.watch_alerts",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_watch_scan_task() -> dict:
    scanner = WatchScanner()
    try:
        return await scanner.run()
    finally:
        await scanner.close()
