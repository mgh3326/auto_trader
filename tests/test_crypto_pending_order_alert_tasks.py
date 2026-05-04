"""ROB-99 scheduled crypto pending-order reminder task tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.tasks import crypto_pending_order_alert_tasks as mod

EXPECTED_SCHEDULES = {
    "crypto_pending_orders_reminder_0830": "30 8 * * *",
    "crypto_pending_orders_reminder_2200": "0 22 * * *",
}


def test_crypto_pending_order_tasks_are_registered():
    import app.tasks as task_package

    assert mod in task_package.TASKIQ_TASK_MODULES


def test_crypto_pending_order_schedule_matrix():
    for name, cron in EXPECTED_SCHEDULES.items():
        task = getattr(mod, name)
        schedules = getattr(task, "labels", {}).get("schedule") or []
        assert any(
            item.get("cron") == cron and item.get("cron_offset") == "Asia/Seoul"
            for item in schedules
        )


@pytest.mark.asyncio
async def test_crypto_pending_order_tasks_delegate_to_runner():
    for name in EXPECTED_SCHEDULES:
        task = getattr(mod, name)
        with patch(
            "app.tasks.crypto_pending_order_alert_tasks.run_crypto_pending_order_reminder",
            AsyncMock(return_value={"status": "success"}),
        ) as runner:
            result = await task()
            assert result == {"status": "success"}
            runner.assert_awaited_once_with(execute=True)
