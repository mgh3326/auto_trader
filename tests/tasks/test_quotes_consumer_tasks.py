"""Task registration + the default-off gate for the #1120 consumer."""

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import quotes_consumer as job
from app.tasks import quotes_consumer_tasks as mod


def test_task_registered_without_recurring_schedule():
    import app.tasks as task_package

    assert mod in task_package.TASKIQ_TASK_MODULES
    labels = getattr(mod.consume_quotes_toss, "labels", {}) or {}
    assert labels.get("schedule") is None


@pytest.mark.asyncio
async def test_noop_when_flag_disabled():
    """Default config is off — the task returns without touching Redis/DB."""
    result = await mod.consume_quotes_toss()
    assert result["enabled"] is False
    assert result["consumed_entries"] == 0


@pytest.mark.asyncio
async def test_enabled_path_builds_the_consumer():
    """Behind the flag the job wires redis + consumer + cleanup."""
    from unittest.mock import Mock

    fake_redis = AsyncMock()
    counters = Mock()
    counters.as_dict.return_value = {"entries_read": 0}
    consumer = Mock()
    consumer.run = AsyncMock(return_value=counters)
    with (
        patch.object(job.settings, "quotes_toss_consumer_enabled", True),
        patch.object(job, "create_redis_client", AsyncMock(return_value=fake_redis)),
        patch.object(job, "QuotesTossConsumer", return_value=consumer) as cls,
    ):
        result = await job.run_quotes_toss_consumer(once=True)

    assert result["enabled"] is True
    cls.assert_called_once()
    consumer.run.assert_awaited_once()
    fake_redis.aclose.assert_awaited_once()
