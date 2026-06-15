from unittest.mock import AsyncMock, patch

import pytest

from app.tasks import toss_live_reconcile_tasks as mod


def test_task_registered_without_recurring_schedule():
    import app.tasks as task_package

    assert mod in task_package.TASKIQ_TASK_MODULES
    labels = getattr(mod.toss_live_reconcile_periodic, "labels", {}) or {}
    assert labels.get("schedule") is None


@pytest.mark.asyncio
async def test_paused_when_flag_disabled():
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", False),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", True
        ),
        patch.object(mod, "toss_reconcile_orders_impl", AsyncMock()) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()

    assert result["status"] == "paused"
    assert "TOSS_LIVE_AUTO_RECONCILE_ENABLED" in result["message"]
    kernel.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_when_safety_review_flag_disabled():
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", False
        ),
        patch.object(mod, "toss_reconcile_orders_impl", AsyncMock()) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()

    assert result["status"] == "paused"
    assert "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED" in result["message"]
    kernel.assert_not_awaited()


@pytest.mark.asyncio
async def test_runs_kernel_when_enabled():
    fake = {"success": True, "counts": {"filled": 1}}
    with (
        patch.object(mod.settings, "TOSS_LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(
            mod.settings, "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED", True
        ),
        patch.object(
            mod, "toss_reconcile_orders_impl", AsyncMock(return_value=fake)
        ) as kernel,
    ):
        result = await mod.toss_live_reconcile_periodic()

    kernel.assert_awaited_once_with(dry_run=False)
    assert result == fake
