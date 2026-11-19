from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts import order_proposal_night_sweep, sync_daily_candles


def test_daily_candles_cli_prints_job_result_and_succeeds(monkeypatch, capsys) -> None:
    calls: list[dict[str, str]] = []

    async def run_daily_candles_sync(*, market: str) -> dict[str, object]:
        calls.append({"market": market})
        return {"status": "ok", "market": market, "synced": 12}

    monkeypatch.setattr(
        sync_daily_candles, "run_daily_candles_sync", run_daily_candles_sync
    )

    assert sync_daily_candles.main(["--market", "kr"]) == 0
    assert calls == [{"market": "kr"}]
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok",
        "market": "kr",
        "synced": 12,
    }


def test_daily_candles_cli_fails_for_job_failure(monkeypatch, capsys) -> None:
    async def run_daily_candles_sync(*, market: str) -> dict[str, object]:
        return {"status": "failed", "market": market, "error": "sync failed"}

    monkeypatch.setattr(
        sync_daily_candles, "run_daily_candles_sync", run_daily_candles_sync
    )

    assert sync_daily_candles.main(["--market", "us"]) != 0
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_daily_candles_cli_fails_on_exception(monkeypatch, capsys) -> None:
    async def run_daily_candles_sync(*, market: str) -> dict[str, object]:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(
        sync_daily_candles, "run_daily_candles_sync", run_daily_candles_sync
    )

    assert sync_daily_candles.main(["--market", "crypto"]) != 0
    assert capsys.readouterr().out == ""


def test_night_sweep_gate_off_prints_skipped_and_does_not_call_job(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        order_proposal_night_sweep.settings,
        "order_proposal_night_sweep_enabled",
        False,
    )

    async def unexpected_call(**kwargs):
        raise AssertionError("night sweep job must not run while disabled")

    monkeypatch.setattr(
        order_proposal_night_sweep,
        "run_order_proposal_night_sweep",
        unexpected_call,
    )

    assert order_proposal_night_sweep.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "skipped"}


def test_night_sweep_enabled_calls_job_and_prints_task_summary(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        order_proposal_night_sweep.settings,
        "order_proposal_night_sweep_enabled",
        True,
    )
    now = datetime(2026, 11, 19, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(order_proposal_night_sweep, "now_kst", lambda: now)
    calls: list[datetime] = []

    async def run_order_proposal_night_sweep(*, now: datetime) -> dict[str, object]:
        calls.append(now)
        return {
            "expiry": {"swept_count": 2},
            "inference": {"applied": 1, "blocked_rows": ["blocked"]},
        }

    monkeypatch.setattr(
        order_proposal_night_sweep,
        "run_order_proposal_night_sweep",
        run_order_proposal_night_sweep,
    )

    assert order_proposal_night_sweep.main() == 0
    assert calls == [now]
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok",
        "swept": 2,
        "inferred": 1,
        "inference_blocked": 1,
    }


def test_night_sweep_job_exception_is_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        order_proposal_night_sweep.settings,
        "order_proposal_night_sweep_enabled",
        True,
    )

    async def run_order_proposal_night_sweep(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(
        order_proposal_night_sweep,
        "run_order_proposal_night_sweep",
        run_order_proposal_night_sweep,
    )

    assert order_proposal_night_sweep.main() != 0
    assert capsys.readouterr().out == ""
