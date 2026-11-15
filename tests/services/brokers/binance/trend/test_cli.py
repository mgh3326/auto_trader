"""TREND CLI: mode gates before any I/O, manage-only passthrough, alert isolation."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import pytest

from app.services.brokers.binance.trend.alerting import (
    TrendAlert,
    TrendAlerter,
    TrendRunMonitor,
)
from app.services.brokers.binance.trend.executor import TrendEvent, TrendTickResult
from scripts import binance_trend_demo as runner

pytestmark = pytest.mark.unit

MODES = ["once", "loop", "manage_only", "readiness"]


def args_for(mode: str, *, confirm: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        once=mode == "once",
        loop=mode == "loop",
        manage_only=mode == "manage_only",
        readiness=mode == "readiness",
        confirm_demo=confirm,
    )


class Boom:
    """Any network or DB construction is a test failure."""

    @classmethod
    def from_env(cls):
        raise AssertionError("client constructed before the gate")

    def __call__(self) -> Any:
        raise AssertionError("database opened before the gate")


class Session:
    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def set_gates(monkeypatch, trend: str | None, futures: str | None) -> None:
    for name, value in (
        ("BINANCE_TREND_DEMO_ENABLED", trend),
        ("BINANCE_FUTURES_DEMO_ENABLED", futures),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


# --------------------------------------------------------------- the gate


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("confirm", "trend", "futures", "message"),
    [
        (False, "true", "true", "--confirm-demo"),
        (True, None, "true", "BINANCE_TREND_DEMO_ENABLED"),
        (True, "true", None, "BINANCE_FUTURES_DEMO_ENABLED"),
        (True, "TRUE", "true", "BINANCE_TREND_DEMO_ENABLED"),
        (True, "true", "1", "BINANCE_FUTURES_DEMO_ENABLED"),
    ],
)
def test_every_mode_refuses_before_network_or_db(
    monkeypatch, mode, confirm, trend, futures, message
):
    monkeypatch.setattr(runner, "TrendDemoClient", Boom)
    monkeypatch.setattr(runner, "AsyncSessionLocal", Boom())
    set_gates(monkeypatch, trend, futures)
    with pytest.raises(SystemExit, match=message):
        asyncio.run(runner._run(args_for(mode, confirm=confirm)))


# ------------------------------------------------------------- readiness


def test_readiness_reports_facts_without_http_db_or_credentials(monkeypatch, capsys):
    monkeypatch.setattr(runner, "TrendDemoClient", Boom)
    monkeypatch.setattr(runner, "AsyncSessionLocal", Boom())
    set_gates(monkeypatch, "true", "true")
    monkeypatch.setenv("BINANCE_TREND_ALERT_ENABLED", "true")
    code = asyncio.run(runner._run(args_for("readiness")))
    out = json.loads(capsys.readouterr().out.strip())
    assert code == 0
    assert out["lane"] == "TREND-TSMOM-v1"
    assert out["lookback_days"] == 60
    assert out["hard_stop_fraction"] == "0.10"
    assert out["leg_notional_cap_usdt"] == "60"
    assert out["max_gross_notional_usdt"] == "240"
    assert out["max_conflicts_per_utc_week"] == 3
    assert out["scheduler"] == "none"
    assert out["gates"] == {
        "BINANCE_TREND_DEMO_ENABLED": True,
        "BINANCE_FUTURES_DEMO_ENABLED": True,
        "BINANCE_TREND_ALERT_ENABLED": True,
    }


def test_the_parser_exposes_exactly_the_four_exclusive_modes():
    parser = runner._parser()
    for flag in ("--once", "--loop", "--manage-only", "--readiness"):
        assert parser.parse_args([flag, "--confirm-demo"]) is not None
    with pytest.raises(SystemExit):
        parser.parse_args(["--once", "--loop", "--confirm-demo"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--confirm-demo"])  # a mode is required


# -------------------------------------------------------------- ticks


class RecordingExecutor:
    seen: list[dict[str, Any]] = []
    result_events = (TrendEvent("BTCUSDT", "no_signal"),)
    ticks = 0

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def run_tick(self, *, now, confirm, manage_only=False):
        RecordingExecutor.seen.append({"confirm": confirm, "manage_only": manage_only})
        RecordingExecutor.ticks += 1
        return TrendTickResult(
            decision_ts=1_790_000_000_000,
            events=RecordingExecutor.result_events,
        )


class BrokenChannel:
    async def send(self, alert: TrendAlert) -> bool:
        raise RuntimeError("webhook down")


@pytest.fixture
def fake_executor(monkeypatch):
    RecordingExecutor.seen = []
    RecordingExecutor.ticks = 0
    RecordingExecutor.result_events = (TrendEvent("BTCUSDT", "no_signal"),)
    monkeypatch.setattr(runner, "TrendExecutor", RecordingExecutor)
    monkeypatch.setattr(runner, "AsyncSessionLocal", lambda: Session())
    monkeypatch.setattr(runner, "BinanceDemoLedgerService", lambda db: object())
    return RecordingExecutor


def run_tick_once(monitor, *, mode: str = "once") -> int:
    return asyncio.run(
        runner._run_ticks(
            args_for(mode),
            client=object(),
            strategy=object(),
            state=object(),
            monitor=monitor,
            stop=runner._StopState(),
            heartbeat=None,
        )
    )


def test_a_manage_only_run_passes_the_flag_to_the_executor(fake_executor, capsys):
    code = run_tick_once(
        TrendRunMonitor(TrendAlerter(channel=None, enabled=False)), mode="manage_only"
    )
    assert code == 0
    assert fake_executor.seen == [{"confirm": True, "manage_only": True}]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["decision_ts"] == 1_790_000_000_000


def test_a_once_run_does_not_ask_for_manage_only(fake_executor, capsys):
    code = run_tick_once(TrendRunMonitor(TrendAlerter(channel=None, enabled=False)))
    assert code == 0
    assert fake_executor.seen == [{"confirm": True, "manage_only": False}]
    capsys.readouterr()


def test_a_failure_tick_exits_2_without_alerts_enabled(fake_executor, capsys):
    fake_executor.result_events = (TrendEvent("BTCUSDT", "blocked", "X"),)
    monitor = TrendRunMonitor(TrendAlerter(channel=None, enabled=False))
    assert run_tick_once(monitor) == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["events"][0]["event"] == "blocked"


def test_a_broken_alert_sender_never_changes_the_tick_result(fake_executor, capsys):
    fake_executor.result_events = (TrendEvent("BTCUSDT", "blocked", "X"),)
    monitor = TrendRunMonitor(TrendAlerter(channel=BrokenChannel(), enabled=True))
    assert monitor.enabled is True
    assert run_tick_once(monitor) == 2  # same code as the healthy-alert path
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["events"][0]["event"] == "blocked"


def test_an_executor_exception_is_one_blocked_line_and_exit_2(
    fake_executor, monkeypatch, capsys
):
    class Exploding(RecordingExecutor):
        async def run_tick(self, *, now, confirm, manage_only=False):
            raise TimeoutError("broker")

    monkeypatch.setattr(runner, "TrendExecutor", Exploding)
    code = run_tick_once(TrendRunMonitor(TrendAlerter(channel=None, enabled=False)))
    assert code == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"event": "blocked", "error_class": "TimeoutError"}
