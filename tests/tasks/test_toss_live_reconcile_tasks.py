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


def test_toss_fill_poll_schedule_labels_default_off():
    with patch.object(mod.settings, "TOSS_FILL_POLL_ENABLED", False):
        assert mod._scheduled_toss_fill_poll_labels() == []


def test_toss_fill_poll_schedule_labels_when_enabled():
    with (
        patch.object(mod.settings, "TOSS_FILL_POLL_ENABLED", True),
        patch.object(mod.settings, "TOSS_FILL_POLL_CRON", "*/2 * * * *"),
    ):
        assert mod._scheduled_toss_fill_poll_labels() == [
            {"cron": "*/2 * * * *", "cron_offset": "Asia/Seoul"}
        ]


@pytest.mark.asyncio
async def test_toss_fill_poller_paused_when_disabled():
    with (
        patch.object(mod.settings, "TOSS_FILL_POLL_ENABLED", False),
        patch.object(mod, "TossFillPollerService") as svc,
        patch.object(mod, "toss_reconcile_orders_impl", AsyncMock()) as reconcile,
    ):
        result = await mod.toss_live_poll_fills_periodic()

    assert result["status"] == "paused"
    assert "TOSS_FILL_POLL_ENABLED" in result["message"]
    svc.assert_not_called()
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_toss_fill_poller_discovers_then_reconciles():
    fake_client = AsyncMock()
    fake_client.aclose = AsyncMock()
    fake_service = AsyncMock()
    fake_service.discover_external_orders = AsyncMock(
        return_value={"success": True, "seeded": 1}
    )

    class _ServiceFactory:
        def __call__(self, db, *, client):
            assert client is fake_client
            return fake_service

    with (
        patch.object(mod.settings, "TOSS_FILL_POLL_ENABLED", True),
        patch.object(mod.settings, "TOSS_FILL_POLL_MARKET_GATE_ENABLED", False),
        patch.object(mod.settings, "TOSS_FILL_POLL_LOOKBACK_DAYS", 7),
        patch.object(mod.settings, "TOSS_FILL_POLL_CLOSED_PAGE_CAP", 20),
        patch.object(mod.settings, "TOSS_FILL_POLL_RECONCILE_LIMIT", 100),
        patch.object(mod.TossReadClient, "from_settings", return_value=fake_client),
        patch.object(mod, "TossFillPollerService", _ServiceFactory()),
        patch.object(
            mod,
            "toss_reconcile_orders_impl",
            AsyncMock(return_value={"success": True, "counts": {"filled": 1}}),
        ) as reconcile,
    ):
        result = await mod.toss_live_poll_fills_periodic()

    fake_service.discover_external_orders.assert_awaited_once_with(
        dry_run=False,
        lookback_days=7,
        closed_page_cap=20,
    )
    reconcile.assert_awaited_once_with(dry_run=False, limit=100)
    fake_client.aclose.assert_awaited_once()
    assert result["success"] is True
    assert result["discover"]["seeded"] == 1
    assert result["reconcile"]["counts"] == {"filled": 1}
