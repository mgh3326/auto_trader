"""Execution-ledger reconciliation tasks.

ROB-214 keeps recurring reconciliation inert by default.  Operators must enable
``execution_ledger_reconcile_scheduler_enabled`` for the scheduler label to be
registered, and writes still require the independent ``EXECUTION_LEDGER_COMMIT_ENABLED``
activation flag.
"""

from __future__ import annotations

from typing import Literal

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker as taskiq_broker
from app.services.execution_ledger.reconciler import ExecutionLedgerReconciler
from app.services.execution_ledger.repository import ExecutionLedgerRepository

ExecutionLedgerBroker = Literal["kis", "upbit"]


def _scheduled_reconcile_labels() -> list[dict[str, str]]:
    if not settings.execution_ledger_reconcile_scheduler_enabled:
        return []
    return [
        {
            "cron": settings.execution_ledger_reconcile_scheduler_cron,
            "cron_offset": "Asia/Seoul",
        }
    ]


async def _run_reconciliation(broker: ExecutionLedgerBroker, window_hours: int) -> dict:
    async with AsyncSessionLocal() as db:
        dry_run = not settings.EXECUTION_LEDGER_COMMIT_ENABLED
        try:
            diff = await ExecutionLedgerReconciler(ExecutionLedgerRepository(db)).run(
                broker,
                window_hours=window_hours,
                dry_run=dry_run,
            )
        except Exception:
            if dry_run:
                # Dry-run skips ledger upserts; commit only preserves the run audit row.
                await db.commit()
            else:
                await db.rollback()
            raise
        # Dry-run skips ledger upserts; commit only preserves the run audit row.
        await db.commit()
    return diff.model_dump(mode="json")


@taskiq_broker.task(task_name="execution_ledger.reconcile_execution_ledger_smoke")
async def reconcile_execution_ledger_smoke(broker: str, window_hours: int = 24) -> dict:
    """Manual smoke. Dry-run unless EXECUTION_LEDGER_COMMIT_ENABLED is true."""
    if broker not in {"kis", "upbit"}:
        raise ValueError("broker must be kis or upbit")
    return await _run_reconciliation(
        broker,  # type: ignore[arg-type]
        window_hours=window_hours,
    )


@taskiq_broker.task(
    task_name="execution_ledger.reconcile_execution_ledger_recurring",
    schedule=_scheduled_reconcile_labels(),
)
async def reconcile_execution_ledger_recurring() -> dict[str, dict]:
    """Recurring freshness reconciliation for KIS and Upbit.

    The task is scheduleless by default.  When explicitly enabled, it still runs
    as dry-run unless EXECUTION_LEDGER_COMMIT_ENABLED is also true.
    """
    window_hours = settings.execution_ledger_reconcile_scheduler_window_hours
    return {
        "kis": await _run_reconciliation("kis", window_hours=window_hours),
        "upbit": await _run_reconciliation("upbit", window_hours=window_hours),
    }
