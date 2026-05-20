"""ROB-281 Stage 3/4 — TaskIQ scheduled wrapper unit tests.

Covers:

* Schedule-registration helpers respect ``invest_screener_schedule_enabled``.
* KR cron labels carry ``cron_offset=Asia/Seoul``; US cron carries
  ``cron_offset=America/New_York`` (ROB-281 D2 DST safety).
* ``is_market_session_today`` correctly skips XKRX weekends, XNYS holidays,
  and Black Friday half-days are recognized as US sessions.
* ``_run_scheduled_build`` skips on non-session days and constructs
  ``SnapshotBuildRequest`` with the right market / commit gate /
  ``common_stocks_only`` selection.

The actual ``run_snapshot_build`` is mocked — these tests do not hit the
database or external screener data sources.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.invest_screener_snapshots import (
    SnapshotBuildRequest,
    SnapshotBuildResult,
)
from app.tasks import invest_screener_snapshot_tasks as task_module


def _fake_build_result(market: str, *, committed: bool = False) -> SnapshotBuildResult:
    started = dt.datetime(2026, 5, 20, 7, 40, tzinfo=dt.UTC)
    finished = dt.datetime(2026, 5, 20, 7, 41, tzinfo=dt.UTC)
    return SnapshotBuildResult(
        market=market,
        symbols_resolved=10,
        snapshots_built=10,
        skipped=0,
        committed=committed,
        batches=1,
        started_at=started,
        finished_at=finished,
        snapshot_date_distribution={"2026-05-20": 10},
        samples=(),
        warnings=(),
    )


# --- Schedule-registration helpers ------------------------------------------


def test_kr_schedule_labels_empty_when_schedule_disabled() -> None:
    with patch.object(task_module.settings, "invest_screener_schedule_enabled", False):
        assert task_module._scheduled_kr_pre_market_repair_labels() == []
        assert task_module._scheduled_kr_krx_preliminary_labels() == []
        assert task_module._scheduled_kr_nxt_final_labels() == []


def test_us_schedule_labels_empty_when_schedule_disabled() -> None:
    with patch.object(task_module.settings, "invest_screener_schedule_enabled", False):
        assert task_module._scheduled_us_post_close_labels() == []


def test_kr_schedule_labels_carry_kst_offset_when_enabled() -> None:
    with patch.object(task_module.settings, "invest_screener_schedule_enabled", True):
        assert task_module._scheduled_kr_pre_market_repair_labels() == [
            {"cron": "40 7 * * 1-5", "cron_offset": "Asia/Seoul"}
        ]
        assert task_module._scheduled_kr_krx_preliminary_labels() == [
            {"cron": "20 16 * * 1-5", "cron_offset": "Asia/Seoul"}
        ]
        assert task_module._scheduled_kr_nxt_final_labels() == [
            {"cron": "20 20 * * 1-5", "cron_offset": "Asia/Seoul"}
        ]


def test_us_schedule_label_carries_et_offset_when_enabled() -> None:
    """ROB-281 D2: US cron runs in America/New_York to avoid DST drift."""
    with patch.object(task_module.settings, "invest_screener_schedule_enabled", True):
        assert task_module._scheduled_us_post_close_labels() == [
            {"cron": "20 17 * * 1-5", "cron_offset": "America/New_York"}
        ]


# --- is_market_session_today (XKRX / XNYS) ----------------------------------


def test_is_market_session_today_kr_regular_weekday() -> None:
    # 2026-05-20 Wed at 14:00 UTC = 23:00 KST — regular KR trading day.
    now = dt.datetime(2026, 5, 20, 14, 0, tzinfo=dt.UTC)
    assert task_module.is_market_session_today("kr", now=now) is True


def test_is_market_session_today_kr_weekend() -> None:
    # 2026-05-23 Sat → not a KR session day.
    now = dt.datetime(2026, 5, 23, 5, 0, tzinfo=dt.UTC)
    assert task_module.is_market_session_today("kr", now=now) is False


def test_is_market_session_today_us_independence_day_is_holiday() -> None:
    """2025-07-04 Fri — NYSE closed for Independence Day."""
    # 18:00 UTC == 14:00 EDT on July 4.
    now = dt.datetime(2025, 7, 4, 18, 0, tzinfo=dt.UTC)
    assert task_module.is_market_session_today("us", now=now) is False


def test_is_market_session_today_us_black_friday_is_session() -> None:
    """Half-day Black Friday 2025-11-28 is still a US session."""
    # 18:00 UTC == 13:00 EST on 2025-11-28.
    now = dt.datetime(2025, 11, 28, 18, 0, tzinfo=dt.UTC)
    assert task_module.is_market_session_today("us", now=now) is True


def test_is_market_session_today_christmas_closed_for_both_markets() -> None:
    # 2025-12-25 Thu — closed for both KRX and NYSE.
    now = dt.datetime(2025, 12, 25, 12, 0, tzinfo=dt.UTC)
    assert task_module.is_market_session_today("kr", now=now) is False
    assert task_module.is_market_session_today("us", now=now) is False


# --- _run_scheduled_build ---------------------------------------------------


@pytest.mark.asyncio
async def test_run_scheduled_build_skips_on_non_session_day() -> None:
    with patch.object(task_module, "is_market_session_today", return_value=False):
        result = await task_module._run_scheduled_build(
            market="kr", slot="krx_preliminary"
        )
    assert result == {
        "market": "kr",
        "slot": "krx_preliminary",
        "skipped": "non_session_day",
        "committed": False,
    }


@pytest.mark.asyncio
async def test_run_scheduled_build_kr_session_day_constructs_request_without_common_stocks() -> (
    None
):
    fake_result = _fake_build_result("kr", committed=False)
    mock_run = AsyncMock(return_value=fake_result)
    with (
        patch.object(task_module, "is_market_session_today", return_value=True),
        patch.object(task_module, "run_snapshot_build_guarded", new=mock_run),
        patch.object(
            task_module.settings,
            "invest_screener_snapshots_commit_enabled",
            False,
        ),
    ):
        result = await task_module._run_scheduled_build(market="kr", slot="nxt_final")

    assert mock_run.await_count == 1
    request: SnapshotBuildRequest = mock_run.call_args.args[0]
    assert request.market == "kr"
    assert request.all_symbols is True
    assert request.common_stocks_only is False  # KR does NOT use the US filter
    assert request.commit is False  # commit gate respected
    assert result["market"] == "kr"
    assert result["slot"] == "nxt_final"
    assert result["snapshotsBuilt"] == 10
    assert result["snapshotDateDistribution"] == {"2026-05-20": 10}


@pytest.mark.asyncio
async def test_run_scheduled_build_us_session_day_uses_common_stocks_filter() -> None:
    fake_result = _fake_build_result("us", committed=False)
    mock_run = AsyncMock(return_value=fake_result)
    with (
        patch.object(task_module, "is_market_session_today", return_value=True),
        patch.object(task_module, "run_snapshot_build_guarded", new=mock_run),
        patch.object(
            task_module.settings,
            "invest_screener_snapshots_commit_enabled",
            False,
        ),
    ):
        await task_module._run_scheduled_build(market="us", slot="post_close")

    request: SnapshotBuildRequest = mock_run.call_args.args[0]
    assert request.market == "us"
    assert request.common_stocks_only is True  # US uses common-stocks filter
    assert request.all_symbols is True


@pytest.mark.asyncio
async def test_run_scheduled_build_commit_gate_passes_through() -> None:
    fake_result = _fake_build_result("kr", committed=True)
    mock_run = AsyncMock(return_value=fake_result)
    with (
        patch.object(task_module, "is_market_session_today", return_value=True),
        patch.object(task_module, "run_snapshot_build_guarded", new=mock_run),
        patch.object(
            task_module.settings,
            "invest_screener_snapshots_commit_enabled",
            True,
        ),
    ):
        result = await task_module._run_scheduled_build(market="kr", slot="nxt_final")

    request: SnapshotBuildRequest = mock_run.call_args.args[0]
    assert request.commit is True  # commit gate True → request.commit True
    assert result["committed"] is True


# --- Alert wiring (Stage 6) -------------------------------------------------


@pytest.mark.asyncio
async def test_run_scheduled_build_does_not_alert_on_success() -> None:
    """CRITICAL contract: success path must NEVER fire an ops alert."""
    fake_result = _fake_build_result("kr", committed=True)
    mock_run = AsyncMock(return_value=fake_result)
    mock_alert = AsyncMock()
    with (
        patch.object(task_module, "is_market_session_today", return_value=True),
        patch.object(task_module, "run_snapshot_build_guarded", new=mock_run),
        patch.object(task_module, "send_screener_refresh_alert", new=mock_alert),
    ):
        await task_module._run_scheduled_build(market="kr", slot="nxt_final")

    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_scheduled_build_fires_alert_on_suspicious_distribution() -> None:
    from app.services.invest_screener_snapshots.guards import (
        SuspiciousDistributionError,
    )

    exc = SuspiciousDistributionError(
        "no dominant partition: top=2026-05-20",
        distribution={"2026-05-20": 1800, "2026-05-19": 1200},
    )
    mock_run = AsyncMock(side_effect=exc)
    mock_alert = AsyncMock(return_value=True)
    with (
        patch.object(task_module, "is_market_session_today", return_value=True),
        patch.object(task_module, "run_snapshot_build_guarded", new=mock_run),
        patch.object(task_module, "send_screener_refresh_alert", new=mock_alert),
    ):
        with pytest.raises(SuspiciousDistributionError):
            await task_module._run_scheduled_build(market="kr", slot="krx_preliminary")

    mock_alert.assert_awaited_once()
    alert_kwargs = mock_alert.await_args.kwargs
    assert alert_kwargs["slot"] == "krx_preliminary"
    assert alert_kwargs["market"] == "kr"
    assert alert_kwargs["exception"] is exc
    assert alert_kwargs["distribution"] == {
        "2026-05-20": 1800,
        "2026-05-19": 1200,
    }
    assert alert_kwargs["commit_status"] == "skipped"


@pytest.mark.asyncio
async def test_run_scheduled_build_fires_alert_on_insufficient_rows() -> None:
    from app.services.invest_screener_snapshots.guards import (
        InsufficientRowsError,
    )

    exc = InsufficientRowsError(
        "kr snapshots_built=50 below floor=2500",
        count=50,
        market="kr",
        distribution={"2026-05-20": 50},
    )
    mock_run = AsyncMock(side_effect=exc)
    mock_alert = AsyncMock(return_value=True)
    with (
        patch.object(task_module, "is_market_session_today", return_value=True),
        patch.object(task_module, "run_snapshot_build_guarded", new=mock_run),
        patch.object(task_module, "send_screener_refresh_alert", new=mock_alert),
    ):
        with pytest.raises(InsufficientRowsError):
            await task_module._run_scheduled_build(market="kr", slot="nxt_final")

    alert_kwargs = mock_alert.await_args.kwargs
    assert alert_kwargs["distribution"] == {"2026-05-20": 50}
    assert alert_kwargs["commit_status"] == "skipped"


@pytest.mark.asyncio
async def test_run_scheduled_build_fires_alert_on_generic_failure() -> None:
    """Generic exceptions (network, DB, etc.) also alert, with commit=failed."""
    exc = RuntimeError("upstream timeout")
    mock_run = AsyncMock(side_effect=exc)
    mock_alert = AsyncMock(return_value=True)
    with (
        patch.object(task_module, "is_market_session_today", return_value=True),
        patch.object(task_module, "run_snapshot_build_guarded", new=mock_run),
        patch.object(task_module, "send_screener_refresh_alert", new=mock_alert),
    ):
        with pytest.raises(RuntimeError, match="upstream timeout"):
            await task_module._run_scheduled_build(market="us", slot="post_close")

    alert_kwargs = mock_alert.await_args.kwargs
    assert alert_kwargs["slot"] == "post_close"
    assert alert_kwargs["market"] == "us"
    assert alert_kwargs["exception"] is exc
    assert alert_kwargs["commit_status"] == "failed"


@pytest.mark.asyncio
async def test_run_scheduled_build_holiday_skip_does_not_alert() -> None:
    """Holiday skips are expected — must NOT fire an alert."""
    mock_alert = AsyncMock()
    with (
        patch.object(task_module, "is_market_session_today", return_value=False),
        patch.object(task_module, "send_screener_refresh_alert", new=mock_alert),
    ):
        result = await task_module._run_scheduled_build(market="kr", slot="nxt_final")

    assert result["skipped"] == "non_session_day"
    mock_alert.assert_not_awaited()
