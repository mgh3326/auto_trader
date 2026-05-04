from __future__ import annotations

from typing import Any

import pytest

from app.tasks import crypto_pending_order_alert_tasks as tasks


@pytest.mark.asyncio
async def test_crypto_pending_order_taskiq_morning_task_runs_execute_true(monkeypatch):
    called: dict[str, Any] = {}

    async def fake_run_crypto_pending_order_alert(*, execute: bool) -> dict[str, Any]:
        called["execute"] = execute
        return {"success": True, "status": "no_orders", "sent": False}

    monkeypatch.setattr(
        tasks,
        "run_crypto_pending_order_alert",
        fake_run_crypto_pending_order_alert,
    )

    result = await tasks.run_crypto_pending_order_morning_alert_task()

    assert result == {"success": True, "status": "no_orders", "sent": False}
    assert called == {"execute": True}


@pytest.mark.asyncio
async def test_crypto_pending_order_taskiq_us_prep_task_runs_execute_true(monkeypatch):
    called: dict[str, Any] = {}

    async def fake_run_crypto_pending_order_alert(*, execute: bool) -> dict[str, Any]:
        called["execute"] = execute
        return {"success": True, "status": "orders_found", "sent": True}

    monkeypatch.setattr(
        tasks,
        "run_crypto_pending_order_alert",
        fake_run_crypto_pending_order_alert,
    )

    result = await tasks.run_crypto_pending_order_us_prep_alert_task()

    assert result == {"success": True, "status": "orders_found", "sent": True}
    assert called == {"execute": True}


def test_crypto_pending_order_taskiq_schedules_are_kst() -> None:
    morning_labels = tasks.run_crypto_pending_order_morning_alert_task.labels
    us_prep_labels = tasks.run_crypto_pending_order_us_prep_alert_task.labels

    assert morning_labels["schedule"] == [
        {"cron": "30 8 * * *", "cron_offset": "Asia/Seoul"}
    ]
    assert us_prep_labels["schedule"] == [
        {"cron": "0 22 * * *", "cron_offset": "Asia/Seoul"}
    ]
