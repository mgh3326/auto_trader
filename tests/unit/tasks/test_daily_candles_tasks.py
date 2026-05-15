"""Tests for daily candle TaskIQ cron task registration.

Schedule access: task.labels['schedule'] is the correct attribute for this project's
TaskIQ version (AsyncTaskiqDecoratedTask). Verified via:
  uv run python -c "from app.tasks import us_candles_tasks; t = ...; print(t.labels)"
  => {'schedule': [{'cron': '*/10 * * * *', 'cron_offset': 'Asia/Seoul'}]}
"""

def test_cron_schedules_are_registered():
    from app.tasks import daily_candles_tasks

    assert hasattr(daily_candles_tasks, "sync_kr_daily_task")
    assert hasattr(daily_candles_tasks, "sync_us_daily_task")
    assert hasattr(daily_candles_tasks, "sync_crypto_daily_task")


def test_cron_schedules_use_asia_seoul_timezone():
    """Verify all three tasks use Asia/Seoul cron_offset, matching project convention."""
    from app.tasks import daily_candles_tasks

    for attr_name in ("sync_kr_daily_task", "sync_us_daily_task", "sync_crypto_daily_task"):
        task = getattr(daily_candles_tasks, attr_name)
        # task.labels is the correct attribute on AsyncTaskiqDecoratedTask
        schedule = task.labels.get("schedule") if hasattr(task, "labels") else None
        assert schedule is not None, f"{attr_name} missing schedule"
        assert any(
            entry.get("cron_offset") == "Asia/Seoul" for entry in schedule
        ), f"{attr_name} missing Asia/Seoul cron_offset"


def test_cron_expressions_match_spec():
    """Guard against typos in cron strings.

    A typo like '30 16 * * 1-6' (accidentally including Saturday for KR)
    would not be caught by the timezone check alone.
    """
    from app.tasks import daily_candles_tasks

    expected = {
        "sync_kr_daily_task": "30 16 * * 1-5",
        "sync_us_daily_task": "0 7 * * 2-6",
        "sync_crypto_daily_task": "0 9 * * *",
    }
    for attr_name, expected_cron in expected.items():
        task = getattr(daily_candles_tasks, attr_name)
        schedule = task.labels.get("schedule")
        assert schedule, f"{attr_name} missing schedule"
        assert schedule[0]["cron"] == expected_cron, (
            f"{attr_name} cron mismatch: {schedule[0]['cron']!r} != {expected_cron!r}"
        )
