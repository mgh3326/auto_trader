"""H5 runner failure alerts wired into scripts/binance_h5_demo.py. Fakes only."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.brokers.binance.h5.alerting import (
    AlertKind,
    H5Alert,
    H5Alerter,
    H5RunMonitor,
)
from app.services.brokers.binance.h5.executor import H5TickResult
from app.services.brokers.binance.h5.strategy import DEMO_URL
from scripts import binance_h5_demo as runner

pytestmark = pytest.mark.unit


class Channel:
    def __init__(self) -> None:
        self.sent: list[H5Alert] = []

    async def send(self, alert: H5Alert) -> bool:
        self.sent.append(alert)
        return True


class _Done(BaseException):
    """Ends a scripted loop without being an Exception or a cancellation."""


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def script(monkeypatch, *items: Any) -> deque:
    """Install a fake executor that yields ``items`` then ends the loop."""
    queue: deque = deque(items)

    class FakeExecutor:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run_tick(self, *, now: Any, confirm: bool) -> H5TickResult:
            assert confirm is True
            if not queue:
                raise _Done()
            item = queue.popleft()
            if isinstance(item, BaseException):
                raise item
            return item

    async def no_sleep(seconds: float) -> None:
        assert seconds == 60

    monkeypatch.setattr(runner, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(runner, "BinanceDemoLedgerService", lambda db: object())
    monkeypatch.setattr(runner, "H5Executor", FakeExecutor)
    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)
    return queue


def tick(event: str, **kwargs: Any) -> H5TickResult:
    return H5TickResult(1_790_000_000_000, event, **kwargs)


def monitor_for(channel: Channel | None, *, enabled: bool = True) -> H5RunMonitor:
    return H5RunMonitor(H5Alerter(channel=channel, enabled=enabled))


def run_loop(args, monitor, stop=None) -> int:
    return asyncio.run(
        runner._run_ticks(
            args,
            client=SimpleNamespace(),
            strategy=SimpleNamespace(),
            state=SimpleNamespace(),
            monitor=monitor,
            stop=stop or runner._StopState(),
        )
    )


LOOP = argparse.Namespace(once=False, loop=True, confirm_demo=True)
ONCE = argparse.Namespace(once=True, loop=False, confirm_demo=True)


def printed(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


# --- disabled: behaviour unchanged ------------------------------------------


@pytest.mark.parametrize(
    ("event", "code"),
    [
        ("blocked", 2),
        ("entry_uncertain", 2),
        ("close_uncertain", 2),
        ("no_entry", 0),
        ("entry_sent", 0),
    ],
)
def test_once_exit_codes_are_unchanged_and_disabled_alerts_never_send(
    monkeypatch, capsys, event, code
):
    script(monkeypatch, tick(event))
    channel = Channel()
    assert run_loop(ONCE, monitor_for(channel, enabled=False)) == code
    assert channel.sent == []
    (line,) = printed(capsys)
    assert line["event"] == event


def test_disabled_loop_failure_is_silent_but_still_printed(monkeypatch, capsys):
    script(monkeypatch, tick("blocked", detail="x"), tick("blocked", detail="x"))
    channel = Channel()
    with pytest.raises(_Done):
        run_loop(LOOP, monitor_for(channel, enabled=False))
    assert channel.sent == []
    assert [line["event"] for line in printed(capsys)] == ["blocked", "blocked"]


def test_disabled_escape_and_cancel_behave_as_before(monkeypatch):
    channel = Channel()
    script(monkeypatch)
    monkeypatch.setattr(
        runner, "AsyncSessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db"))
    )
    with pytest.raises(RuntimeError):
        run_loop(LOOP, monitor_for(channel, enabled=False))
    script(monkeypatch, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):  # not installed: re-raised untouched
        run_loop(LOOP, monitor_for(channel, enabled=False))
    assert channel.sent == []


def test_build_monitor_is_off_unless_the_flag_is_exactly_true(monkeypatch):
    def not_built():
        raise AssertionError("channel must not be built when disabled")

    monkeypatch.setattr(runner, "build_default_channel", not_built)
    for value in (None, "", "false", "TRUE", "1"):
        if value is None:
            monkeypatch.delenv("BINANCE_H5_ALERT_ENABLED", raising=False)
        else:
            monkeypatch.setenv("BINANCE_H5_ALERT_ENABLED", value)
        assert runner._build_monitor().enabled is False
    channel = Channel()
    monkeypatch.setattr(runner, "build_default_channel", lambda: channel)
    monkeypatch.setenv("BINANCE_H5_ALERT_ENABLED", "true")
    assert runner._build_monitor().enabled is True


# --- enabled: one alert per failure kind -------------------------------------


def test_blocked_tick_alerts_once_while_it_persists(monkeypatch, capsys):
    script(monkeypatch, *[tick("blocked", detail="order reconciliation pending")] * 6)
    channel = Channel()
    with pytest.raises(_Done):
        run_loop(LOOP, monitor_for(channel))
    assert [(a.kind, a.signature) for a in channel.sent] == [
        (AlertKind.ERROR, "blocked:order reconciliation pending")
    ]
    assert len(printed(capsys)) == 6


@pytest.mark.parametrize("event", ["entry_uncertain", "close_uncertain"])
def test_uncertain_ticks_alert(monkeypatch, event):
    script(monkeypatch, tick(event), tick(event))
    channel = Channel()
    with pytest.raises(_Done):
        run_loop(LOOP, monitor_for(channel))
    assert [a.signature for a in channel.sent] == [event]


def test_tick_exception_becomes_an_error_alert_with_its_class(monkeypatch):
    script(monkeypatch, ValueError("x"), ValueError("x"))
    channel = Channel()
    with pytest.raises(_Done):
        run_loop(LOOP, monitor_for(channel))
    assert [a.signature for a in channel.sent] == ["blocked:ValueError"]


def test_recovery_then_new_failure_alerts_again(monkeypatch):
    script(monkeypatch, tick("blocked"), tick("no_entry"), tick("blocked"))
    channel = Channel()
    with pytest.raises(_Done):
        run_loop(LOOP, monitor_for(channel))
    assert len(channel.sent) == 2


def test_healthy_loop_never_alerts(monkeypatch):
    script(monkeypatch, tick("no_entry"), tick("already_processed"), tick("entry_sent"))
    channel = Channel()
    with pytest.raises(_Done):
        run_loop(LOOP, monitor_for(channel))
    assert channel.sent == []


def test_once_failure_alerts_and_exits_2(monkeypatch):
    script(monkeypatch, tick("close_uncertain"))
    channel = Channel()
    assert run_loop(ONCE, monitor_for(channel)) == 2
    assert [a.kind for a in channel.sent] == [AlertKind.ERROR]


# --- enabled: stopped --------------------------------------------------------


def test_an_exception_escaping_the_loop_is_a_stopped_alert_and_still_raises(
    monkeypatch,
):
    script(monkeypatch)

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("db gone")

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(runner, "AsyncSessionLocal", lambda: BrokenSession())
    channel = Channel()
    with pytest.raises(RuntimeError, match="db gone"):
        run_loop(LOOP, monitor_for(channel))
    assert [(a.kind, a.signature) for a in channel.sent] == [
        (AlertKind.STOPPED, "exception:RuntimeError")
    ]


def stop_state(*, operator: bool, reason: str) -> runner._StopState:
    stop = runner._StopState()
    stop.installed, stop.operator, stop.reason = True, operator, reason
    return stop


def test_sigterm_cancellation_is_a_stopped_alert_with_exit_143(monkeypatch):
    script(monkeypatch, asyncio.CancelledError())
    channel = Channel()
    code = run_loop(
        LOOP, monitor_for(channel), stop_state(operator=False, reason="sigterm")
    )
    assert code == 143
    assert [(a.kind, a.signature) for a in channel.sent] == [
        (AlertKind.STOPPED, "sigterm")
    ]


def test_operator_ctrl_c_exits_130_without_an_alert(monkeypatch):
    script(monkeypatch, asyncio.CancelledError())
    channel = Channel()
    code = run_loop(
        LOOP, monitor_for(channel), stop_state(operator=True, reason="sigint")
    )
    assert code == 130
    assert channel.sent == []


def test_keyboard_interrupt_is_the_operator_and_propagates(monkeypatch):
    script(monkeypatch, KeyboardInterrupt())
    channel = Channel()
    with pytest.raises(KeyboardInterrupt):
        run_loop(LOOP, monitor_for(channel))
    assert channel.sent == []


def test_unexpected_cancellation_without_our_handlers_is_not_swallowed(monkeypatch):
    script(monkeypatch, asyncio.CancelledError())
    channel = Channel()
    with pytest.raises(asyncio.CancelledError):
        run_loop(LOOP, monitor_for(channel))
    assert channel.sent == []


# --- signal wiring on a real loop (handlers are called, not signalled) -------


def test_signal_handlers_map_sigint_to_operator_and_sigterm_to_unexpected(
    monkeypatch,
):
    script(monkeypatch)
    started = asyncio.Event
    results: dict[str, Any] = {}

    async def scenario(sig_name: str) -> tuple[int, list[H5Alert]]:
        import signal

        loop = asyncio.get_running_loop()
        handlers: dict[int, Any] = {}
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, cb, *args: handlers.__setitem__(sig, (cb, args)),
        )
        entered = started()

        class Hold:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def run_tick(self, *, now: Any, confirm: bool):
                entered.set()
                await asyncio.Event().wait()  # until cancelled

        monkeypatch.setattr(runner, "H5Executor", Hold)
        channel = Channel()
        stop = runner._StopState()

        async def main() -> int:
            runner._install_stop_signals(stop)
            return await runner._run_ticks(
                LOOP,
                client=SimpleNamespace(),
                strategy=SimpleNamespace(),
                state=SimpleNamespace(),
                monitor=monitor_for(channel),
                stop=stop,
            )

        task = asyncio.create_task(main())
        await entered.wait()
        assert stop.installed is True
        callback, args = handlers[getattr(signal, sig_name)]
        callback(*args)
        return await task, channel.sent

    code, sent = asyncio.run(scenario("SIGTERM"))
    results["term"] = (code, [(a.kind, a.signature) for a in sent])
    code, sent = asyncio.run(scenario("SIGINT"))
    results["int"] = (code, sent)
    assert results["term"] == (143, [(AlertKind.STOPPED, "sigterm")])
    assert results["int"] == (130, [])


def test_signal_handlers_are_only_installed_when_alerts_are_enabled(monkeypatch):
    script(monkeypatch, tick("no_entry"))
    installed: list[int] = []
    monkeypatch.setattr(
        runner, "_install_stop_signals", lambda stop: installed.append(1)
    )

    class Client:
        _base_url = DEMO_URL

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        runner.H5DemoClient, "from_env", classmethod(lambda cls: Client())
    )
    monkeypatch.setenv("BINANCE_H5_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")

    monkeypatch.delenv("BINANCE_H5_ALERT_ENABLED", raising=False)
    assert asyncio.run(runner._run(ONCE)) == 0
    assert installed == []

    channel = Channel()
    monkeypatch.setenv("BINANCE_H5_ALERT_ENABLED", "true")
    monkeypatch.setattr(runner, "build_default_channel", lambda: channel)
    script(monkeypatch, tick("no_entry"))
    assert asyncio.run(runner._run(ONCE)) == 0
    assert installed == [1]


def test_a_broken_alert_path_changes_nothing_the_runner_does(monkeypatch, capsys):
    class Exploding:
        async def send(self, alert: H5Alert) -> bool:
            raise RuntimeError("webhook down")

    script(monkeypatch, tick("blocked"), tick("no_entry"))
    monitor = H5RunMonitor(H5Alerter(channel=Exploding(), enabled=True))
    with pytest.raises(_Done):
        run_loop(LOOP, monitor)
    assert [line["event"] for line in printed(capsys)] == ["blocked", "no_entry"]
