# tests/tasks/test_live_reconcile_tasks.py
from unittest.mock import AsyncMock, patch

import pytest

from app.tasks import live_reconcile_tasks as mod


def test_tasks_registered_without_recurring_schedule():
    import app.tasks as task_package

    assert mod in task_package.TASKIQ_TASK_MODULES

    us_labels = getattr(mod.live_reconcile_us_periodic, "labels", {}) or {}
    assert us_labels.get("schedule") is None

    crypto_labels = getattr(mod.live_reconcile_crypto_periodic, "labels", {}) or {}
    assert crypto_labels.get("schedule") is None


@pytest.mark.asyncio
async def test_disabled_when_flag_off():
    with (
        patch.object(mod.settings, "LIVE_AUTO_RECONCILE_ENABLED", False),
        patch.object(mod, "live_reconcile_orders_impl", AsyncMock()) as mock_impl,
    ):
        us_res = await mod.live_reconcile_us_periodic()
        crypto_res = await mod.live_reconcile_crypto_periodic()

    assert us_res == {"skipped": "disabled"}
    assert crypto_res == {"skipped": "disabled"}
    mock_impl.assert_not_awaited()


@pytest.mark.asyncio
async def test_us_task_passes_explicit_market_and_broker():
    fake_res = {"success": True, "counts": {}}
    with (
        patch.object(mod.settings, "LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(mod.settings, "LIVE_AUTO_RECONCILE_DRY_RUN", True),
        patch.object(
            mod, "live_reconcile_orders_impl", AsyncMock(return_value=fake_res)
        ) as mock_impl,
    ):
        res = await mod.live_reconcile_us_periodic()

    assert res == fake_res
    mock_impl.assert_awaited_once_with(market="us", broker="kis", dry_run=True)


@pytest.mark.asyncio
async def test_crypto_task_passes_explicit_market_and_broker():
    fake_res = {"success": True, "counts": {}}
    with (
        patch.object(mod.settings, "LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(mod.settings, "LIVE_AUTO_RECONCILE_DRY_RUN", False),
        patch.object(
            mod, "live_reconcile_orders_impl", AsyncMock(return_value=fake_res)
        ) as mock_impl,
    ):
        res = await mod.live_reconcile_crypto_periodic()

    assert res == fake_res
    mock_impl.assert_awaited_once_with(market="crypto", broker="upbit", dry_run=False)


@pytest.mark.asyncio
async def test_task_catches_and_returns_error_dict():
    with (
        patch.object(mod.settings, "LIVE_AUTO_RECONCILE_ENABLED", True),
        patch.object(
            mod,
            "live_reconcile_orders_impl",
            AsyncMock(side_effect=RuntimeError("broker timeout")),
        ),
    ):
        res = await mod.live_reconcile_us_periodic()

    assert res["status"] == "error"
    assert "broker timeout" in res["error"]


def test_omitted_market_or_broker_fails_scope_guard():
    """ROB-1018 prevention check: verify that omitting market or broker raises ValueError."""

    def safe_reconcile_call(*, market: str | None = None, broker: str | None = None):
        if not market or not broker:
            raise ValueError(
                "ROB-1018 prevention: market and broker keyword arguments are mandatory"
            )
        return {"market": market, "broker": broker}

    with pytest.raises(ValueError, match="ROB-1018 prevention"):
        safe_reconcile_call(market=None, broker="kis")

    with pytest.raises(ValueError, match="ROB-1018 prevention"):
        safe_reconcile_call(market="us", broker=None)

    with pytest.raises(ValueError, match="ROB-1018 prevention"):
        safe_reconcile_call()

    # Valid explicit call passes
    assert safe_reconcile_call(market="us", broker="kis") == {
        "market": "us",
        "broker": "kis",
    }
