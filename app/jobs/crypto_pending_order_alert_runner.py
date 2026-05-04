"""Scheduler-agnostic runner for ROB-99 crypto pending-order reminders."""

from __future__ import annotations

from app.services.crypto_pending_order_alert_service import (
    run_crypto_pending_order_alert,
)


async def run_crypto_pending_order_reminder(
    *, execute: bool = True
) -> dict[str, object]:
    """Run the read-only crypto pending-order reminder.

    TaskIQ/Prefect/CLI wrappers should call this thin function rather than
    duplicating broker lookup, formatting, or delivery policy.
    """
    return await run_crypto_pending_order_alert(execute=execute)
