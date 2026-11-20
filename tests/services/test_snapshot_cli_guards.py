"""CLI exit-code guards for snapshot commits (hk 5752 r1).

Monkeypatched job functions only: no database, no network.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.jobs import financial_fundamentals_snapshots as fin_job
from app.jobs import invest_screener_snapshots as scr_job
from scripts import build_financial_fundamentals_snapshots as fin_cli
from scripts import build_invest_screener_snapshots as scr_cli

_T0 = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)


def _screener_result(*, committed: bool, built: int, distribution=None):
    return scr_job.SnapshotBuildResult(
        market="us",
        symbols_resolved=3,
        snapshots_built=built,
        skipped=0,
        committed=committed,
        batches=1,
        started_at=_T0,
        finished_at=_T0,
        snapshot_date_distribution=distribution or {},
    )


@pytest.mark.asyncio
async def test_commit_with_zero_rows_skips_retention_and_exits_4(monkeypatch, capsys):
    calls: list[dict] = []

    async def fake_build(request):
        assert request.commit is True
        return _screener_result(committed=True, built=0)

    async def fake_prune(market, **kwargs):
        calls.append(kwargs)
        raise AssertionError("prune must not run when the commit wrote no rows")

    monkeypatch.setattr(scr_cli.snapshot_job, "run_snapshot_build", fake_build)
    monkeypatch.setattr(scr_cli.snapshot_job, "prune_snapshot_history", fake_prune)

    code = await scr_cli.run(
        scr_cli.parse_args(
            [
                "--market",
                "us",
                "--allow-partial",
                "--commit",
                "--keep-trading-days",
                "5",
            ]
        )
    )

    assert code == 4
    assert calls == []
    assert "retention: skipped" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_dry_run_passes_build_dates_to_the_cutoff(monkeypatch):
    seen: dict = {}

    async def fake_build(request):
        assert request.commit is False
        return _screener_result(
            committed=False,
            built=2,
            distribution={"2026-11-20": 2},
        )

    async def fake_prune(market, **kwargs):
        seen.update(kwargs)
        return scr_job.RetentionResult(
            market=market,
            keep_trading_days=5,
            cutoff=None,
            rows=0,
            committed=False,
        )

    monkeypatch.setattr(scr_cli.snapshot_job, "run_snapshot_build", fake_build)
    monkeypatch.setattr(scr_cli.snapshot_job, "prune_snapshot_history", fake_prune)

    code = await scr_cli.run(
        scr_cli.parse_args(
            ["--market", "us", "--limit", "3", "--keep-trading-days", "5"]
        )
    )

    assert code == 0
    assert seen["commit"] is False
    assert seen["pending_dates"] == {dt.date(2026, 11, 20): 2}


@pytest.mark.asyncio
async def test_financial_commit_suppressed_by_budget_exits_3(monkeypatch, capsys):
    async def fake_resolve(market, override, limit):
        return ["005930"]

    async def fake_build(request, **kwargs):
        return fin_job.FinancialFundamentalsSnapshotBuildResult(
            market="kr",
            symbols_resolved=1,
            snapshots_built=0,
            committed=False,
            started_at=_T0,
            finished_at=_T0,
            commit_suppressed=True,
        )

    monkeypatch.setattr(fin_job, "resolve_symbols", fake_resolve)
    monkeypatch.setattr(
        fin_job, "run_financial_fundamentals_snapshot_build", fake_build
    )

    code = await fin_cli.run(
        fin_cli.parse_args(
            ["--market", "kr", "--limit", "1", "--commit", "--allow-partial"]
        )
    )

    assert code == 3
    assert "COMMIT SUPPRESSED" in capsys.readouterr().out
