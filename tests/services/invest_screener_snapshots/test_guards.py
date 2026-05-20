"""ROB-281 Stage 5 — commit-time guard unit tests.

Validates ``assert_dominant_partition`` (≥70% rule) and ``assert_min_row_count``
(KR 2500 / US 3500 locked floors). Also exercises the 2-pass
:func:`run_snapshot_build_guarded` wrapper to ensure it short-circuits commits
on guard violations without re-fetching.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.invest_screener_snapshots import (
    SnapshotBuildRequest,
    SnapshotBuildResult,
    run_snapshot_build_guarded,
)
from app.services.invest_screener_snapshots.guards import (
    InsufficientRowsError,
    SuspiciousDistributionError,
    assert_dominant_partition,
    assert_min_row_count,
)

# --- assert_dominant_partition ----------------------------------------------


def test_assert_dominant_partition_clean_majority_passes() -> None:
    dist = {"2026-05-20": 950, "2026-05-19": 50}  # 95%
    assert assert_dominant_partition(dist) == "2026-05-20"


def test_assert_dominant_partition_exactly_at_threshold_passes() -> None:
    # 70% exactly — must be inclusive at the threshold.
    dist = {"2026-05-20": 700, "2026-05-19": 300}
    assert assert_dominant_partition(dist) == "2026-05-20"


def test_assert_dominant_partition_below_threshold_raises() -> None:
    # 69% — just below 70% threshold.
    dist = {"2026-05-20": 690, "2026-05-19": 310}
    with pytest.raises(SuspiciousDistributionError) as excinfo:
        assert_dominant_partition(dist)
    msg = str(excinfo.value)
    assert "2026-05-20" in msg
    assert "69" in msg  # ratio percentage surfaced
    assert "2026-05-19" in msg  # full distribution surfaced for triage


def test_assert_dominant_partition_empty_distribution_raises() -> None:
    with pytest.raises(SuspiciousDistributionError, match="empty"):
        assert_dominant_partition({})


def test_assert_dominant_partition_zero_total_raises() -> None:
    with pytest.raises(SuspiciousDistributionError, match="non-positive"):
        assert_dominant_partition({"2026-05-20": 0, "2026-05-19": 0})


def test_assert_dominant_partition_custom_threshold() -> None:
    dist = {"2026-05-20": 600, "2026-05-19": 400}  # 60%
    # Default 70% → fails; custom 50% → passes.
    with pytest.raises(SuspiciousDistributionError):
        assert_dominant_partition(dist)
    assert assert_dominant_partition(dist, threshold=0.50) == "2026-05-20"


def test_assert_dominant_partition_single_partition_passes() -> None:
    dist = {"2026-05-20": 3867}  # all in one date
    assert assert_dominant_partition(dist) == "2026-05-20"


# --- assert_min_row_count ----------------------------------------------------


def test_assert_min_row_count_kr_at_floor_passes() -> None:
    assert_min_row_count(2500, "kr")  # exactly at floor


def test_assert_min_row_count_kr_above_floor_passes() -> None:
    assert_min_row_count(3867, "kr")  # observed 2026-05-19 KR count


def test_assert_min_row_count_kr_below_floor_raises() -> None:
    with pytest.raises(InsufficientRowsError, match=r"kr snapshots_built=2499"):
        assert_min_row_count(2499, "kr")


def test_assert_min_row_count_us_at_floor_passes() -> None:
    assert_min_row_count(3500, "us")


def test_assert_min_row_count_us_above_floor_passes() -> None:
    assert_min_row_count(5116, "us")  # observed 2026-05-19 US count


def test_assert_min_row_count_us_below_floor_raises() -> None:
    with pytest.raises(InsufficientRowsError, match=r"us snapshots_built=3499"):
        assert_min_row_count(3499, "us")


def test_assert_min_row_count_unknown_market_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown market"):
        assert_min_row_count(1000, "crypto")  # type: ignore[arg-type]


# --- run_snapshot_build_guarded ---------------------------------------------


def _build_result(
    market: str,
    *,
    snapshots_built: int,
    distribution: dict[str, int],
    committed: bool,
) -> SnapshotBuildResult:
    started = dt.datetime(2026, 5, 20, 7, 40, tzinfo=dt.UTC)
    finished = dt.datetime(2026, 5, 20, 7, 41, tzinfo=dt.UTC)
    return SnapshotBuildResult(
        market=market,
        symbols_resolved=snapshots_built,
        snapshots_built=snapshots_built,
        skipped=0,
        committed=committed,
        batches=1,
        started_at=started,
        finished_at=finished,
        snapshot_date_distribution=distribution,
        samples=(),
        warnings=(),
    )


@pytest.mark.asyncio
async def test_guarded_wrapper_passes_runs_commit_pass_when_request_commit_true() -> (
    None
):
    """Both passes execute when request.commit=True and guards pass."""
    dry_run = _build_result(
        "kr",
        snapshots_built=3000,
        distribution={"2026-05-20": 3000},
        committed=False,
    )
    commit_run = _build_result(
        "kr",
        snapshots_built=3000,
        distribution={"2026-05-20": 3000},
        committed=True,
    )
    mock = AsyncMock(side_effect=[dry_run, commit_run])
    with patch("app.jobs.invest_screener_snapshots.run_snapshot_build", new=mock):
        result = await run_snapshot_build_guarded(
            SnapshotBuildRequest(market="kr", all_symbols=True, commit=True)
        )
    assert mock.await_count == 2  # dry-run + commit
    # First call had commit=False (dry-run), second had commit=True.
    assert mock.call_args_list[0].args[0].commit is False
    assert mock.call_args_list[1].args[0].commit is True
    assert result.committed is True


@pytest.mark.asyncio
async def test_guarded_wrapper_passes_skips_commit_pass_when_request_commit_false() -> (
    None
):
    """Only the dry-run pass executes when request.commit=False."""
    dry_run = _build_result(
        "kr",
        snapshots_built=3000,
        distribution={"2026-05-20": 3000},
        committed=False,
    )
    mock = AsyncMock(return_value=dry_run)
    with patch("app.jobs.invest_screener_snapshots.run_snapshot_build", new=mock):
        result = await run_snapshot_build_guarded(
            SnapshotBuildRequest(market="kr", all_symbols=True, commit=False)
        )
    assert mock.await_count == 1  # dry-run only
    assert result.committed is False


@pytest.mark.asyncio
async def test_guarded_wrapper_raises_on_suspicious_distribution_without_commit_pass() -> (
    None
):
    """Suspicious distribution short-circuits before commit (no second fetch)."""
    dry_run = _build_result(
        "kr",
        snapshots_built=3000,
        # 60% / 40% split — below 70% threshold.
        distribution={"2026-05-20": 1800, "2026-05-19": 1200},
        committed=False,
    )
    mock = AsyncMock(return_value=dry_run)
    with patch("app.jobs.invest_screener_snapshots.run_snapshot_build", new=mock):
        with pytest.raises(SuspiciousDistributionError):
            await run_snapshot_build_guarded(
                SnapshotBuildRequest(market="kr", all_symbols=True, commit=True)
            )
    assert mock.await_count == 1  # dry-run only — commit pass skipped


@pytest.mark.asyncio
async def test_guarded_wrapper_raises_on_insufficient_rows_without_commit_pass() -> (
    None
):
    """Too-few rows short-circuit before commit."""
    dry_run = _build_result(
        "kr",
        snapshots_built=50,  # well below KR floor 2500
        distribution={"2026-05-20": 50},
        committed=False,
    )
    mock = AsyncMock(return_value=dry_run)
    with patch("app.jobs.invest_screener_snapshots.run_snapshot_build", new=mock):
        with pytest.raises(InsufficientRowsError):
            await run_snapshot_build_guarded(
                SnapshotBuildRequest(market="kr", all_symbols=True, commit=True)
            )
    assert mock.await_count == 1
