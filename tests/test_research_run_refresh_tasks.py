"""ROB-26 Taskiq cron task tests."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.tasks import research_run_refresh_tasks as mod


EXPECTED_SCHEDULES = {
    "kr_preopen_research_refresh": ("10 8 * * 1-5", "preopen"),
    "kr_regular_open_live_refresh": ("3 9 * * 1-5", "preopen"),
    "nxt_aftermarket_refresh_1545": ("45 15 * * 1-5", "nxt_aftermarket"),
    "nxt_aftermarket_refresh_1630": ("30 16 * * 1-5", "nxt_aftermarket"),
    "nxt_aftermarket_refresh_1730": ("30 17 * * 1-5", "nxt_aftermarket"),
    "nxt_aftermarket_refresh_1830": ("30 18 * * 1-5", "nxt_aftermarket"),
    "nxt_aftermarket_refresh_1930": ("30 19 * * 1-5", "nxt_aftermarket"),
    "nxt_final_check_1955": ("55 19 * * 1-5", "nxt_aftermarket"),
}


def test_all_expected_tasks_exist():
    for name in EXPECTED_SCHEDULES:
        assert hasattr(mod, name), f"missing task: {name}"


def test_cron_strings_match_schedule_matrix():
    for name, (cron, _stage) in EXPECTED_SCHEDULES.items():
        task = getattr(mod, name)
        labels = getattr(task, "labels", {})
        schedules = labels.get("schedule") or []
        assert any(
            s.get("cron") == cron and s.get("cron_offset") == "Asia/Seoul"
            for s in schedules
        ), (
            f"{name}: expected cron={cron!r} cron_offset='Asia/Seoul', got {schedules}"
        )


def test_module_registered_in_taskiq_task_modules():
    import app.tasks as task_package

    assert mod in task_package.TASKIQ_TASK_MODULES


@pytest.mark.asyncio
async def test_each_task_delegates_to_runner():
    for name, (_cron, stage) in EXPECTED_SCHEDULES.items():
        task = getattr(mod, name)
        with patch(
            "app.tasks.research_run_refresh_tasks.run_research_run_refresh",
            AsyncMock(return_value={"status": "skipped", "reason": "test"}),
        ) as runner:
            result = await task()  # invoke task body directly
            assert result == {"status": "skipped", "reason": "test"}
            runner.assert_awaited_once_with(stage=stage, market_scope="kr")
