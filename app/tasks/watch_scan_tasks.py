from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.investment_watch_scanner import InvestmentWatchScanner
from app.jobs.watch_scanner import WatchScanner


@broker.task(
    task_name="scan.watch_alerts",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_watch_scan_task() -> dict:
    """Legacy Redis-backed scanner. Plan 5 will remove this task alongside
    the rest of the OpenClaw / watch-order-intent-ledger surface.
    """
    scanner = WatchScanner()
    try:
        return await scanner.run()
    finally:
        await scanner.close()


@broker.task(
    task_name="scan.investment_watch_alerts",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_investment_watch_scan_task() -> dict:
    """ROB-265 Plan 4 — DB-backed scanner that emits Hermes review-trigger
    notifications. Runs alongside the legacy task during the transition;
    Plan 5 removes the legacy task.
    """
    scanner = InvestmentWatchScanner()
    try:
        return await scanner.run()
    finally:
        await scanner.close()
