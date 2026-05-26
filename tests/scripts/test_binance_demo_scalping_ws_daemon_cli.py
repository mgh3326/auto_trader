"""ROB-317 — WS daemon CLI default-disabled behavior."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates
from app.services.brokers.binance.demo_scalping_ws.market_stream import FuturesWsEvent
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent
from scripts.binance_demo_scalping_ws_daemon import build_summary, main, run_daemon

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
