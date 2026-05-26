"""ROB-317 — WS daemon CLI default-disabled behavior."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates
from app.services.brokers.binance.demo_scalping_ws.market_stream import FuturesWsEvent
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent
from scripts.binance_demo_scalping_ws_daemon import (
    build_summary,
    main,
    resolve_confirm,
    run_daemon,
)

_MODULE = "scripts.binance_demo_scalping_ws_daemon"

_T0 = dt.datetime(2026, 5, 26, 10, 0, tzinfo=dt.UTC)


def _seq() -> list[FuturesWsEvent]:
    out: list[FuturesWsEvent] = [
        BookTickerEvent(
            symbol="XRPUSDT",
            bid_price=Decimal("0.50"),
            bid_qty=Decimal("1"),
            ask_price=Decimal("0.5001"),
            ask_qty=Decimal("1"),
            received_at=_T0,
        )
    ]
    for m in range(25):
        out.append(_cli_kline("0.50", "0.51", "0.49", m))
    out.append(_cli_kline("0.60", "0.60", "0.50", 25))
    return out


def _cli_kline(close: str, high: str, low: str, minute: int) -> KlineEvent:
    base = _T0 + dt.timedelta(minutes=minute)
    return KlineEvent(
        symbol="XRPUSDT",
        interval="1m",
        open_time=base,
        close_time=base + dt.timedelta(seconds=59),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        base_volume=Decimal("1000"),
        quote_volume=Decimal("515"),
        trade_count=42,
        is_closed=True,
    )


def test_summary_disabled_when_gates_off() -> None:
    gates = WsDaemonGates(base_enabled=False, ws_enabled=False, ws_confirm=False)
    summary = build_summary(gates)
    assert summary["status"] == "disabled"
    assert summary["subscribed"] is False


def test_summary_running_when_gates_on() -> None:
    gates = WsDaemonGates(base_enabled=True, ws_enabled=True, ws_confirm=False)
    summary = build_summary(gates)
    assert summary["status"] == "running"
    assert summary["subscribed"] is False
    assert summary["mutation_allowed"] is False


def test_main_disabled_exits_zero_and_prints_json(capsys, monkeypatch) -> None:
    for key in (
        "BINANCE_DEMO_SCALPING_ENABLED",
        "BINANCE_DEMO_SCALPING_WS_ENABLED",
        "BINANCE_DEMO_SCALPING_WS_CONFIRM",
    ):
        monkeypatch.delenv(key, raising=False)
    rc = main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "disabled"
    assert out["subscribed"] is False


@pytest.mark.asyncio
async def test_run_daemon_routes_triggers_to_injected_on_trigger() -> None:
    seen: list[str] = []

    async def fake_on_trigger(trigger) -> None:
        seen.append(trigger.symbol)

    await run_daemon(
        symbols=["XRPUSDT"],
        source_factory=lambda: _source(_seq()),
        on_trigger=fake_on_trigger,
        clock=lambda: _T0 + dt.timedelta(seconds=5),
    )
    assert seen == ["XRPUSDT"]  # one BUY breakout routed to the sink


async def _source(events: list[FuturesWsEvent]) -> AsyncIterator[FuturesWsEvent]:
    for ev in events:
        yield ev


# --- bounded operator mode (ROB-317 follow-up) -----------------------------


async def _noop_on_trigger(_trigger) -> None:
    return None


def _fail_if_called(**_kwargs) -> None:
    raise AssertionError("run_daemon must not be invoked here")


def _book_for(symbol: str) -> BookTickerEvent:
    return BookTickerEvent(
        symbol=symbol,
        bid_price=Decimal("0.50"),
        bid_qty=Decimal("1"),
        ask_price=Decimal("0.5001"),
        ask_qty=Decimal("1"),
        received_at=_T0,
    )


def _kline_for(symbol: str, close: str, high: str, low: str, minute: int) -> KlineEvent:
    base = _T0 + dt.timedelta(minutes=minute)
    return KlineEvent(
        symbol=symbol,
        interval="1m",
        open_time=base,
        close_time=base + dt.timedelta(seconds=59),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        base_volume=Decimal("1000"),
        quote_volume=Decimal("515"),
        trade_count=42,
        is_closed=True,
    )


def _breakout_for(symbol: str) -> list[FuturesWsEvent]:
    out: list[FuturesWsEvent] = [_book_for(symbol)]
    for m in range(25):
        out.append(_kline_for(symbol, "0.50", "0.51", "0.49", m))
    out.append(_kline_for(symbol, "0.60", "0.60", "0.50", 25))
    return out


def test_main_disabled_does_not_invoke_run_daemon(monkeypatch) -> None:
    # Default-disabled must exit without ever building a source / opening a
    # socket: run_daemon is never reached.
    for key in (
        "BINANCE_DEMO_SCALPING_ENABLED",
        "BINANCE_DEMO_SCALPING_WS_ENABLED",
        "BINANCE_DEMO_SCALPING_WS_CONFIRM",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(f"{_MODULE}.run_daemon", _fail_if_called)
    assert main([]) == 0


@pytest.mark.asyncio
async def test_run_daemon_max_runtime_exits_cleanly() -> None:
    # A stream that never yields must not hang — --max-runtime-sec bounds it.
    async def _never() -> AsyncIterator[FuturesWsEvent]:
        await asyncio.sleep(3600)
        yield  # pragma: no cover - unreachable, makes this an async generator

    count = await run_daemon(
        symbols=["XRPUSDT"],
        source_factory=lambda: _never(),
        on_trigger=_noop_on_trigger,
        max_runtime_sec=0.05,
    )
    assert count == 0  # exited via the runtime bound, no triggers


@pytest.mark.asyncio
async def test_run_daemon_max_triggers_stops_after_n() -> None:
    seen: list[str] = []

    async def sink(trigger) -> None:
        seen.append(trigger.symbol)

    seq = _breakout_for("XRPUSDT") + _breakout_for("DOGEUSDT")
    count = await run_daemon(
        symbols=["XRPUSDT", "DOGEUSDT"],
        source_factory=lambda: _source(seq),
        on_trigger=sink,
        clock=lambda: _T0 + dt.timedelta(seconds=5),
        max_triggers=1,
    )
    assert count == 1
    assert seen == ["XRPUSDT"]  # stopped after the first symbol's trigger


def test_resolve_confirm_requires_env_and_flag() -> None:
    all_on = WsDaemonGates(base_enabled=True, ws_enabled=True, ws_confirm=True)
    env_only = WsDaemonGates(base_enabled=True, ws_enabled=True, ws_confirm=False)
    assert resolve_confirm(all_on, confirm_flag=True) is True
    assert resolve_confirm(all_on, confirm_flag=False) is False  # --confirm missing
    assert resolve_confirm(env_only, confirm_flag=True) is False  # env gate missing


def test_main_confirm_without_bound_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_WS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_WS_CONFIRM", "true")
    # Confirmed mode with no trigger bound must fail closed before running.
    monkeypatch.setattr(f"{_MODULE}.run_daemon", _fail_if_called)
    assert main(["--confirm"]) == 2


def test_main_confirmed_bounded_wires_run_daemon(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_WS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_WS_CONFIRM", "true")
    captured: dict = {}

    async def _fake_run_daemon(**kwargs) -> int:
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(f"{_MODULE}.run_daemon", _fake_run_daemon)
    assert main(["--confirm", "--exit-after-first-trigger"]) == 0
    assert captured["confirm"] is True
    assert captured["max_triggers"] == 1
