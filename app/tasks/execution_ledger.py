"""Scheduleless manual smoke task for ROB-211 execution ledger reconciliation."""

from __future__ import annotations

from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker as taskiq_broker
from app.services.execution_ledger.reconciler import ExecutionLedgerReconciler
from app.services.execution_ledger.repository import ExecutionLedgerRepository


@taskiq_broker.task(task_name="execution_ledger.reconcile_execution_ledger_smoke")
async def reconcile_execution_ledger_smoke(broker: str, window_hours: int = 24) -> dict:
    """Manual-only dry-run smoke. No schedule is registered in this PR."""
    if broker not in {"kis", "upbit"}:
        raise ValueError("broker must be kis or upbit")
    async with AsyncSessionLocal() as db:
        diff = await ExecutionLedgerReconciler(ExecutionLedgerRepository(db)).run(
            broker, window_hours=window_hours, dry_run=True
        )
        await db.rollback()
    return diff.model_dump(mode="json")
