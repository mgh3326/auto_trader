"""ROB-265 — Investment watch scanner scheduled task.

Replaces the legacy ``scan.watch_alerts`` (Redis-backed) task that was
removed in Plan 5. The investment_watch scanner reads DB-backed
``investment_watch_alerts``, writes ``investment_watch_events`` with
the immutable trigger-identity snapshot, and emits Hermes
review-trigger notifications.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.investment_watch_scanner import InvestmentWatchScanner


@broker.task(
    task_name="scan.investment_watch_alerts",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_investment_watch_scan_task() -> dict:
    """DB-backed scanner that emits Hermes review-trigger notifications."""
    scanner = InvestmentWatchScanner()
    try:
        return await scanner.run()
    finally:
        await scanner.close()
