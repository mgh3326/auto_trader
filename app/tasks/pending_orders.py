"""ROB-119 — TaskIQ background task for syncing pending orders."""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.pending_order_sync import run_pending_order_sync


@broker.task(task_name="sync_pending_orders_all_venues")
async def sync_pending_orders_all_venues(user_id: int) -> dict[str, int]:
    """Sync pending orders from all supported brokers."""
    return await run_pending_order_sync(user_id=user_id)
