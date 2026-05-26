"""ROB-317 — supervisor trigger / freshness / debounce (fake source)."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    FuturesWsEvent,
)
from app.services.brokers.binance.demo_scalping_ws.supervisor import (
    ScalpingDaemonSupervisor,
    TriggerEvent,
)
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent

pytestmark = pytest.mark.asyncio

_T0 = dt.datetime(2026, 5, 26, 10, 0, tzinfo=dt.UTC)


def _book(now_offset: int = 0) -> BookTickerEvent:
    return BookTickerEvent(
        symbol="XRPUSDT",
        bid_price=Decimal("0.50"),
        bid_qty=Decimal("1"),
        ask_price=Decimal("0.5001"),
        ask_qty=Decimal("1"),
        received_at=_T0 + dt.timedelta(seconds=now_offset),
    )


def _kline(close: str, *, high: str, low: str, minute: int) -> KlineEvent:
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


def _breakout_sequence() -> list[FuturesWsEvent]:
    events: list[FuturesWsEvent] = [_book()]
    for m in range(25):
        events.append(_kline("0.50", high="0.51", low="0.49", minute=m))
    events.append(_kline("0.60", high="0.60", low="0.50", minute=25))
    return events


async def _source_from(events: list[FuturesWsEvent]) -> AsyncIterator[FuturesWsEvent]:
    for ev in events:
        yield ev


class _Clock:
    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now


async def test_fresh_breakout_emits_trigger() -> None:
    clock = _Clock(_T0 + dt.timedelta(seconds=5))  # within 120s of the book event
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    await sup.run(
        lambda: _source_from(_breakout_sequence()),
        on_trigger=_async_appender(captured),
    )
    assert len(captured) == 1
    assert captured[0].symbol == "XRPUSDT"
    assert captured[0].side == "BUY"


async def test_stale_quote_blocks_trigger() -> None:
    # Clock far past the single book event -> STALE_DATA, no trigger.
    clock = _Clock(_T0 + dt.timedelta(seconds=600))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    await sup.run(
        lambda: _source_from(_breakout_sequence()),
        on_trigger=_async_appender(captured),
    )
    assert captured == []


async def test_debounce_suppresses_second_trigger() -> None:
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(
        symbols=["XRPUSDT"], clock=clock, debounce_seconds=300
    )
    seq = _breakout_sequence()
    seq.append(_kline("0.70", high="0.70", low="0.60", minute=26))  # 2nd breakout
    captured: list[TriggerEvent] = []
    await sup.run(lambda: _source_from(seq), on_trigger=_async_appender(captured))
    assert len(captured) == 1  # second suppressed by debounce


def _async_appender(sink: list):
    async def _append(trigger) -> None:
        sink.append(trigger)

    return _append


async def test_run_with_reconnect_recovers_after_transient_error() -> None:
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    slept: list[float] = []

    calls = {"n": 0}

    def factory() -> AsyncIterator[FuturesWsEvent]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _raises_after_book()
        return _source_from(_breakout_sequence())

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    await sup.run_with_reconnect(
        factory,
        on_trigger=_async_appender(captured),
        sleep=fake_sleep,
    )
    assert len(captured) == 1  # recovered on the 2nd connection
    assert len(slept) == 1  # backed off once between attempts


async def test_run_with_reconnect_raises_when_unhealthy() -> None:
    clock = _Clock(_T0)
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    def always_fails() -> AsyncIterator[FuturesWsEvent]:
        return _raises_immediately()

    with pytest.raises(ConnectionError):
        await sup.run_with_reconnect(
            always_fails, on_trigger=_async_appender([]), sleep=fake_sleep
        )
    assert len(slept) == 2  # 2 backoffs before the 3rd failure trips unhealthy


async def _raises_after_book() -> AsyncIterator[FuturesWsEvent]:
    yield _book()
    raise ConnectionError("socket dropped")


async def _raises_immediately() -> AsyncIterator[FuturesWsEvent]:
    raise ConnectionError("cannot connect")
    yield  # pragma: no cover - makes this an async generator


async def test_trigger_carries_source_candle_close_time() -> None:
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    await sup.run(
        lambda: _source_from(_breakout_sequence()),
        on_trigger=_async_appender(captured),
    )
    assert len(captured) == 1
    # The breakout candle is minute=25; its close_time is open+59s.
    expected_close = _T0 + dt.timedelta(minutes=25, seconds=59)
    assert captured[0].source_candle_close_time_ms == int(
        expected_close.timestamp() * 1000
    )
