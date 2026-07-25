"""Paused TaskIQ entrypoint for Binance Demo root-reservation reconciliation.

No schedule is registered. Two capability gates are required even for broker
reads, and a third confirm gate is required before local lifecycle mutation.
All settings are evaluated in the running worker process: environment prefixes
on ``taskiq kick`` do not configure an already-running worker.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.binance_demo_root_reservation_reconciliation import (
    run_binance_demo_root_reservation_reconciliation_from_env,
)

_MIN_SAFE_AGE_SECONDS = 3600


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@broker.task(task_name="binance.demo_root_reservation.reconcile")  # no schedule
async def binance_demo_root_reservation_reconcile() -> dict[str, Any]:
    if not (
        settings.binance_demo_scalping_enabled
        and settings.BINANCE_DEMO_RESERVATION_RECONCILE_ENABLED
    ):
        return {
            "status": "paused",
            "message": "Binance Demo scalping + reservation reconcile gates required",
        }

    dry_run = not settings.BINANCE_DEMO_RESERVATION_RECONCILE_CONFIRM
    min_age_seconds = max(
        settings.BINANCE_DEMO_RESERVATION_RECONCILE_MIN_AGE_SECONDS,
        _MIN_SAFE_AGE_SECONDS,
    )
    now = _utcnow()
    result = await run_binance_demo_root_reservation_reconciliation_from_env(
        now=now,
        stale_before=now - dt.timedelta(seconds=min_age_seconds),
        dry_run=dry_run,
    )
    return {
        **result,
        "mutation_confirmed": not dry_run,
        "min_age_seconds": min_age_seconds,
    }
