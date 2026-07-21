"""ROB-976 R3 operator CLI: bounded, dry-run default, real commit path."""

from __future__ import annotations

import datetime as dt

import pytest

from app.jobs.support_proximity_snapshots import (
    SupportProximityBuildResult,
    SupportProximitySample,
)
from scripts import build_support_proximity_snapshot as cli


def _result(*, committed: bool, partition: dt.date | None = dt.date(2026, 7, 20)):
    return SupportProximityBuildResult(
        market="kr",
        source_partition_date=partition,
        candidates_resolved=1 if partition else 0,
        snapshots_built=1 if partition else 0,
        supports_built=1 if partition else 0,
        skipped=0,
        committed=committed,
        started_at=dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 7, 20, 10, 1, tzinfo=dt.UTC),
        samples=(
            SupportProximitySample(
                symbol="005930",
                snapshot_date=dt.date(2026, 7, 20),
                latest_close="80000",
                support_price="78500",
                support_kind="bb_lower",
                support_strength="strong",
                dist_to_support_pct="1.8750",
                market_cap="400000000000000",
                support_computed_at=dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.UTC),
            ),
        )
        if partition
        else (),
    )


def test_defaults_are_bounded_dry_run():
    args = cli.parse_args([])
    assert args.market == "kr"
    assert args.commit is False
    assert args.dry_run is True
    assert args.limit == 10
    assert args.candidate_pool_limit == 30


def test_commit_is_explicit_opt_in():
    args = cli.parse_args(["--commit", "--candidate-pool-limit", "10"])
    assert args.commit is True
    assert args.dry_run is False
    assert args.candidate_pool_limit == 10


@pytest.mark.asyncio
async def test_run_returns_1_when_base_partition_is_missing(monkeypatch, capsys):
    async def _fake_run(request):
        assert request.commit is False
        return _result(committed=False, partition=None)

    monkeypatch.setattr(cli.snapshot_job, "run_support_proximity_build", _fake_run)

    exit_code = await cli.run(cli.parse_args([]))

    assert exit_code == 1
    assert "no rows written" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_threads_commit_and_prints_persisted_result(monkeypatch, capsys):
    async def _fake_run(request):
        assert request.commit is True
        assert request.candidate_pool_limit == 10
        return _result(committed=True)

    monkeypatch.setattr(cli.snapshot_job, "run_support_proximity_build", _fake_run)

    exit_code = await cli.run(
        cli.parse_args(["--commit", "--candidate-pool-limit", "10"])
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "005930" in output
    assert "committed 1 row" in output
