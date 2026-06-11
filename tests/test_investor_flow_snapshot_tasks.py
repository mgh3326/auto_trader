from __future__ import annotations

import datetime as dt

import pytest

from app.jobs.investor_flow_snapshots import (
    InvestorFlowSnapshotBuildResult,
    InvestorFlowSnapshotSample,
)
from app.tasks import investor_flow_snapshot_tasks as tasks


def test_parse_args_defaults_to_dry_run_and_rejects_invalid_combinations():
    from scripts import build_investor_flow_snapshots as cli

    args = cli.parse_args(["--market", "kr"])
    assert args.market == "kr"
    assert args.limit == 20
    assert args.days == 20
    assert args.commit is False
    assert args.dry_run is True

    commit_args = cli.parse_args(["--market", "kr", "--symbol", "005930", "--commit"])
    assert commit_args.commit is True
    assert commit_args.dry_run is False

    with pytest.raises(SystemExit):
        cli.parse_args(["--market", "kr", "--all", "--limit", "20"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--market", "kr", "--days", "0"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--market", "kr", "--days", "61"])


@pytest.mark.asyncio
async def test_task_wrapper_defaults_to_dry_run_and_returns_camel_case(monkeypatch):
    captured = {}

    async def fake_runner(request):
        captured["commit"] = request.commit
        captured["days"] = request.days
        return InvestorFlowSnapshotBuildResult(
            market="kr",
            symbols_resolved=1,
            snapshots_built=1,
            committed=request.commit,
            batches=1,
            started_at=dt.datetime(2026, 5, 12, 7, 0, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 5, 12, 7, 1, tzinfo=dt.UTC),
            snapshot_date_distribution={"2026-05-12": 1},
            idempotency={"wouldInsert": 1, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            samples=(
                InvestorFlowSnapshotSample(
                    market="kr",
                    symbol="005930",
                    snapshot_date=dt.date(2026, 5, 12),
                    source="naver_finance",
                    foreign_net=100,
                    institution_net=50,
                    individual_net=-150,
                    double_buy=True,
                    double_sell=False,
                ),
            ),
        )

    monkeypatch.setattr(tasks, "run_investor_flow_snapshot_build", fake_runner)

    raw_func = getattr(
        tasks.build_investor_flow_snapshots,
        "original_func",
        tasks.build_investor_flow_snapshots,
    )
    payload = await raw_func(market="kr", symbols=["005930"], days=20)

    assert captured == {"commit": False, "days": 20}
    assert payload["symbolsResolved"] == 1
    assert payload["snapshotsBuilt"] == 1
    assert payload["committed"] is False
    assert payload["idempotency"]["wouldInsert"] == 1
    assert payload["samples"][0]["snapshotDate"] == "2026-05-12"
    assert payload["samples"][0]["doubleBuy"] is True


def test_recurring_schedule_is_default_off():
    # ROB-438: the module now has a recurring scheduled task, but it is DEFAULT-OFF
    # — merging this PR alone registers no cron. The manual build task still carries
    # no schedule; the scheduled task's cron labels are empty unless the schedule
    # flag is set (operator-gated, mirroring invest_screener ROB-281).
    from unittest.mock import patch

    from app.tasks import TASKIQ_TASK_MODULES

    assert tasks in TASKIQ_TASK_MODULES
    labels = getattr(tasks.build_investor_flow_snapshots, "labels", {}) or {}
    assert labels.get("schedule") is None  # manual task: no schedule
    with patch.object(tasks.settings, "investor_flow_schedule_enabled", False):
        assert tasks._kr_flow_schedule("40 16 * * 1-5") == []
    with patch.object(tasks.settings, "investor_flow_schedule_enabled", True):
        assert tasks._kr_flow_schedule("40 16 * * 1-5") == [
            {"cron": "40 16 * * 1-5", "cron_offset": "Asia/Seoul"}
        ]


def test_scheduled_cron_is_next_morning_kst():
    """ROB-512 갭4: Naver frgn 일별 수급 확정 행은 당일 저녁엔 부분 발행
    (2026-06-10 18:10 KST 실측 144/3,909 종목 = thin 파티션 → older_fallback)이고
    익일 아침에 완성된다(2026-06-11 오전 라이브 검증). 구 ROB-438의 16:40 KST는
    구조적으로 당일 데이터를 못 잡으므로 등록 cron은 익일 아침(개장 전)이어야 한다."""
    assert tasks._KR_FLOW_CRON == "30 8 * * 1-5"
