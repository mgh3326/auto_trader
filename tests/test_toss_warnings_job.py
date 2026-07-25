"""ROB-550: the Toss warnings sync job/task ships scheduleless and skips
gracefully (no ERROR) when the Toss API is disabled."""

from __future__ import annotations

import pytest

from app.jobs import toss_warnings

pytestmark = pytest.mark.asyncio


async def test_run_toss_warnings_sync_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(
        toss_warnings.settings, "toss_api_enabled", False, raising=False
    )

    def _boom():
        raise AssertionError("must not build a client when Toss is disabled")

    monkeypatch.setattr(toss_warnings.TossReadClient, "from_settings", _boom)

    result = await toss_warnings.run_toss_warnings_sync()

    assert result["status"] == "disabled"


def test_sync_toss_warnings_task_is_scheduleless():
    """Recurrence is operator/Prefect-registered (house convention); the task
    must not ship with an embedded cron schedule."""
    from app.tasks import toss_warnings_sync_tasks as task_mod

    task = task_mod.sync_toss_warnings_task
    labels = getattr(task, "labels", {}) or {}
    schedule = labels.get("schedule")
    assert not schedule, f"expected scheduleless task, found schedule={schedule!r}"
