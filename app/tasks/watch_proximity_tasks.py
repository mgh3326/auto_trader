from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.watch_proximity_monitor import WatchProximityMonitor


@broker.task(
    task_name="scan.watch_proximity",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_watch_proximity_task() -> dict:
    monitor = WatchProximityMonitor()
    try:
        return await monitor.run()
    finally:
        await monitor.close()
