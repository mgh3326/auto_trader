"""ROB-99 — scheduled crypto pending-order reminders.

Current repository scheduling is TaskIQ-based. The job runner is intentionally
scheduler-agnostic so a Prefect deployment can call the same entrypoint without
changing broker lookup or notification policy.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.crypto_pending_order_alert_runner import run_crypto_pending_order_reminder

_KST = "Asia/Seoul"


@broker.task(
    task_name="crypto.pending_orders.reminder_0830",
    schedule=[{"cron": "30 8 * * *", "cron_offset": _KST}],
)
async def crypto_pending_orders_reminder_0830() -> dict[str, object]:
    return await run_crypto_pending_order_reminder(execute=True)


@broker.task(
    task_name="crypto.pending_orders.reminder_2200",
    schedule=[{"cron": "0 22 * * *", "cron_offset": _KST}],
)
async def crypto_pending_orders_reminder_2200() -> dict[str, object]:
    return await run_crypto_pending_order_reminder(execute=True)
