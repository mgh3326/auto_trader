"""scripts/binance_h5_heartbeat_watch.py: gate, polling, exit codes. Fakes only."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys

import pytest

from app.services.brokers.binance.h5.alerting import (
    AlertKind,
    H5Alert,
    H5Alerter,
)
from scripts import binance_h5_heartbeat_watch as watch

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 10, 5, 3, 0, tzinfo=dt.UTC)


def run_watch(*a, **kw):
    """The watcher with a frozen clock: age is exact, never wall-clock dependent."""
    return watch._watch(*a, clock=lambda: NOW, **kw)


class Channel:
    def __init__(self, result: bool = True) -> None:
        self.sent: list[H5Alert] = []
        self.result = result

    async def send(self, alert: H5Alert) -> bool:
        self.sent.append(alert)
        return self.result


class Ticks:
    def __init__(self, *values: dt.datetime | None | Exception) -> None:
        self.values = list(values)

    async def last_tick_at(self):
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if isinstance(value, Exception):
            raise value
        return value


class Stop(BaseException):
    pass


def args(**kwargs) -> argparse.Namespace:
    base = {
        "once": False,
        "loop": False,
        "send_test": False,
        "miss_minutes": 10,
        "poll_seconds": 60,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


# --- gate ---------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "false", "TRUE", "1"])
def test_disabled_watcher_refuses_to_start_and_builds_nothing(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("BINANCE_H5_ALERT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BINANCE_H5_ALERT_ENABLED", value)

    def must_not_build():
        raise AssertionError("no channel when disabled")

    monkeypatch.setattr(watch, "build_default_channel", must_not_build)
    with pytest.raises(SystemExit, match="BINANCE_H5_ALERT_ENABLED must be true"):
        asyncio.run(watch._run(args(once=True)))


# --- polling ------------------------------------------------------------------


def test_once_exit_codes(monkeypatch, capsys):
    channel = Channel()
    alerter = H5Alerter(channel=channel, enabled=True)

    fresh = asyncio.run(run_watch(args(once=True), state=Ticks(NOW), alerter=alerter))
    stale = asyncio.run(
        run_watch(
            args(once=True),
            state=Ticks(NOW - dt.timedelta(minutes=11)),
            alerter=alerter,
        )
    )
    absent = asyncio.run(run_watch(args(once=True), state=Ticks(None), alerter=alerter))
    broken = asyncio.run(
        run_watch(args(once=True), state=Ticks(OSError("db")), alerter=alerter)
    )
    assert (fresh, stale, absent, broken) == (0, 2, 0, 2)
    assert [r["verdict"] for r in lines(capsys)] == [
        "ok",
        "missed",
        "absent",
        "unreadable",
    ]
    assert [a.kind for a in channel.sent] == [
        AlertKind.HEARTBEAT_MISSED,
        AlertKind.HEARTBEAT_MISSED,
    ]


def test_loop_alerts_once_for_a_dead_runner_and_polls_at_the_requested_pace(
    monkeypatch, capsys
):
    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 8:
            raise Stop

    monkeypatch.setattr(watch.asyncio, "sleep", fake_sleep)
    channel = Channel()
    alerter = H5Alerter(channel=channel, enabled=True)
    state = Ticks(NOW - dt.timedelta(hours=2))
    with pytest.raises(Stop):
        asyncio.run(
            run_watch(args(loop=True, poll_seconds=30), state=state, alerter=alerter)
        )
    assert sleeps == [30] * 8
    assert len(channel.sent) == 1
    assert len(lines(capsys)) == 8


def test_runner_coming_back_rearms_the_alert(monkeypatch):
    calls = 0

    async def fake_sleep(seconds: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise Stop

    monkeypatch.setattr(watch.asyncio, "sleep", fake_sleep)
    channel = Channel()
    old = NOW - dt.timedelta(hours=1)
    state = Ticks(old, NOW, old)  # stale, healthy, stale again
    with pytest.raises(Stop):
        asyncio.run(
            run_watch(
                args(loop=True),
                state=state,
                alerter=H5Alerter(channel=channel, enabled=True),
            )
        )
    assert len(channel.sent) == 2


def test_miss_threshold_comes_from_the_flag():
    channel = Channel()
    alerter = H5Alerter(channel=channel, enabled=True)
    four_min_old = Ticks(NOW - dt.timedelta(minutes=4))
    assert (
        asyncio.run(
            run_watch(
                args(once=True, miss_minutes=5), state=four_min_old, alerter=alerter
            )
        )
        == 0
    )
    assert (
        asyncio.run(
            run_watch(
                args(once=True, miss_minutes=3), state=four_min_old, alerter=alerter
            )
        )
        == 2
    )


# --- send-test ------------------------------------------------------------------


@pytest.mark.parametrize(("result", "code"), [(True, 0), (False, 2)])
def test_send_test_reports_delivery(capsys, result, code):
    channel = Channel(result)
    alerter = H5Alerter(channel=channel, enabled=True)
    assert asyncio.run(watch._send_test(alerter)) == code
    (line,) = capsys.readouterr().out.splitlines()
    assert line == json.dumps({"delivered": result, "event": "alert_test"})
    assert [a.kind for a in channel.sent] == [AlertKind.TEST]


def test_run_send_test_never_touches_the_database(monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_H5_ALERT_ENABLED", "true")
    channel = Channel()
    monkeypatch.setattr(watch, "build_default_channel", lambda: channel)

    def no_state(*a, **k):
        raise AssertionError("send-test must not build the state service")

    monkeypatch.setattr(watch, "H5StateService", no_state)
    assert asyncio.run(watch._run(args(send_test=True))) == 0
    assert len(channel.sent) == 1


# --- argparse -------------------------------------------------------------------


def parse(monkeypatch, *argv: str) -> argparse.Namespace:
    captured: dict = {}

    async def fake_run(parsed: argparse.Namespace) -> int:
        captured["args"] = parsed
        return 0

    monkeypatch.setattr(watch, "_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["binance_h5_heartbeat_watch.py", *argv])
    with pytest.raises(SystemExit) as exit_:
        watch.main()
    assert exit_.value.code == 0
    return captured["args"]


def test_modes_are_mutually_exclusive_and_required(monkeypatch):
    for argv in ([], ["--once", "--loop"], ["--loop", "--send-test"]):
        monkeypatch.setattr(sys, "argv", ["x", *argv])
        with pytest.raises(SystemExit) as exit_:
            watch.main()
        assert exit_.value.code == 2


def test_defaults_and_bounds(monkeypatch):
    parsed = parse(monkeypatch, "--loop")
    assert (parsed.miss_minutes, parsed.poll_seconds) == (10, 60)
    for bad in (["--loop", "--miss-minutes", "2"], ["--loop", "--poll-seconds", "9"]):
        monkeypatch.setattr(sys, "argv", ["x", *bad])
        with pytest.raises(SystemExit) as exit_:
            watch.main()
        assert exit_.value.code == 2


def test_ctrl_c_ends_the_watcher_cleanly(monkeypatch):
    async def interrupted(parsed: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(watch, "_run", interrupted)
    monkeypatch.setattr(sys, "argv", ["x", "--loop"])
    with pytest.raises(SystemExit) as exit_:
        watch.main()
    assert exit_.value.code == 0
