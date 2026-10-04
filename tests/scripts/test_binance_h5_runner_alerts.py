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


async def _turn() -> None:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    loop.call_soon(future.set_result, None)
    await future


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
        # The real runner sleeps 60 s between ticks, which is when background
        # alert sends run; give the loop a few turns to model that gap.
        assert seconds == 60
        for _ in range(3):
            await _turn()

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


def test_a_cancellation_without_our_handlers_is_reported_and_still_propagates(
    monkeypatch,
):
    """Round-1 B4: the signal-install fallback must not silence the stop alert."""
    script(monkeypatch, asyncio.CancelledError())
    channel = Channel()
    with pytest.raises(asyncio.CancelledError):
        run_loop(LOOP, monitor_for(channel))  # stop.installed is False
    assert [(a.kind, a.signature) for a in channel.sent] == [
        (AlertKind.STOPPED, "cancelled")
    ]


def test_installing_the_handlers_can_fail_without_losing_the_alert(monkeypatch):
    script(monkeypatch)
    channel = Channel()
    monkeypatch.setenv("BINANCE_H5_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_H5_ALERT_ENABLED", "true")
    monkeypatch.setattr(runner, "build_default_channel", lambda: channel)

    class Client:
        _base_url = DEMO_URL

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        runner.H5DemoClient, "from_env", classmethod(lambda cls: Client())
    )

    async def scenario() -> None:
        entered = asyncio.Event()

        class Hold:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def run_tick(self, *, now: Any, confirm: bool):
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(runner, "H5Executor", Hold)

        def unavailable(*args: Any) -> None:
            raise NotImplementedError("loop without signal handler support")

        monkeypatch.setattr(
            asyncio.get_running_loop(), "add_signal_handler", unavailable
        )
        task = asyncio.create_task(runner._run(LOOP))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [a.kind for a in channel.sent] == [AlertKind.STOPPED]


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


# --- round-1 B1 / B2 through the real loop and the real tick ----------------


def test_a_slow_webhook_does_not_hold_up_the_next_tick(monkeypatch):
    """B1: tick 2 starts while the alert for tick 1 is still being delivered."""
    monkeypatch.setattr(runner, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(runner, "BinanceDemoLedgerService", lambda db: object())

    async def scenario() -> tuple[bool, int]:
        release = asyncio.Event()
        calls: list[int] = []
        reached_second = asyncio.Event()

        class Executor:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def run_tick(self, *, now: Any, confirm: bool):
                calls.append(1)
                if len(calls) == 1:
                    return tick("blocked", detail="temporary read failure")
                reached_second.set()
                release.set()
                raise _Done()

        class Slow:
            sent: list[H5Alert] = []

            async def send(self, alert: H5Alert) -> bool:
                await release.wait()
                self.sent.append(alert)
                return True

        async def instant(seconds: float) -> None:
            await _turn()

        monkeypatch.setattr(runner, "H5Executor", Executor)
        monkeypatch.setattr(runner.asyncio, "sleep", instant)
        channel = Slow()
        task = asyncio.create_task(
            runner._run_ticks(
                LOOP,
                client=SimpleNamespace(),
                strategy=SimpleNamespace(),
                state=SimpleNamespace(),
                monitor=monitor_for(channel),
                stop=runner._StopState(),
            )
        )
        await asyncio.wait_for(reached_second.wait(), timeout=2)
        with pytest.raises(_Done):
            await task
        return reached_second.is_set(), len(channel.sent)

    assert asyncio.run(scenario()) == (True, 1)  # drain delivered it on the way out


def test_once_failure_alert_is_delivered_before_the_process_returns(monkeypatch):
    """The drain in finally means a --once failure still reaches the channel."""
    script(monkeypatch, tick("close_uncertain"))

    class Slow:
        sent: list[H5Alert] = []

        async def send(self, alert: H5Alert) -> bool:
            await _turn()
            await _turn()
            self.sent.append(alert)
            return True

    channel = Slow()
    assert run_loop(ONCE, monitor_for(channel)) == 2
    assert len(channel.sent) == 1


def test_real_tick_with_alternating_transport_errors_is_one_alert(monkeypatch):
    """B2 through the real run_tick: one unresolved intent, errors alternate."""
    import dataclasses
    import datetime as dt
    from unittest.mock import AsyncMock

    import httpx

    from app.services.brokers.binance.h5.executor import H5Executor

    now0 = dt.datetime(2026, 10, 5, tzinfo=dt.UTC)
    clock = [now0]
    channel = Channel()
    monitor = H5RunMonitor(
        H5Alerter(channel=channel, enabled=True, clock=lambda: clock[0])
    )
    intent = SimpleNamespace(state="sending", client_order_id="h5-same-intent")
    state = SimpleNamespace(
        record_nav=AsyncMock(),
        list_unresolved_intents=AsyncMock(return_value=(intent,)),
    )
    executor = H5Executor(
        client=SimpleNamespace(
            read_account=AsyncMock(return_value=SimpleNamespace(nav_usdt=1000))
        ),
        strategy=SimpleNamespace(),
        state=state,
        demo_ledger=object(),
        ledger_session=object(),
    )
    executor._guard = lambda **kwargs: None  # type: ignore[method-assign]
    executor._reconcile_intent = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            httpx.ConnectError("outage"),
            httpx.ReadTimeout("outage"),
            httpx.ConnectError("outage"),
            httpx.ReadTimeout("outage"),
        ]
    )

    async def scenario() -> None:
        for minute in range(4):
            clock[0] = now0 + dt.timedelta(minutes=minute)
            result = await executor.run_tick(now=clock[0], confirm=True)
            assert result.event == "blocked"
            await monitor.tick_done(dataclasses.asdict(result))
            await monitor.drain()

    asyncio.run(scenario())
    assert [a.signature for a in channel.sent] == ["blocked:ConnectError"]
