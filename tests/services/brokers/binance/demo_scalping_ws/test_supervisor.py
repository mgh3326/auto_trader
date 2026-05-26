"""ROB-317 — supervisor trigger / freshness / debounce (fake source)."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from websockets.exceptions import ConnectionClosedError

from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    AggTradeEvent,
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


def _agg(now_offset: int = 0) -> AggTradeEvent:
    return AggTradeEvent(
        symbol="XRPUSDT",
        price=Decimal("0.50"),
        qty=Decimal("1"),
        trade_time=_T0 + dt.timedelta(seconds=now_offset),
        is_buyer_maker=False,
    )


def _klines_then_breakout() -> list[FuturesWsEvent]:
    """25 flat 1m candles then a breakout close (no quote events)."""
    out: list[FuturesWsEvent] = []
    for m in range(25):
        out.append(_kline("0.50", high="0.51", low="0.49", minute=m))
    out.append(_kline("0.60", high="0.60", low="0.50", minute=25))
    return out


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


async def test_fresh_aggtrade_without_bookticker_blocks_trigger() -> None:
    # (a) A fresh aggTrade must NOT satisfy the quote-freshness gate: without a
    # bookTicker there is no bid/ask, so the spread guard would be bypassed.
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    events: list[FuturesWsEvent] = [_agg(now_offset=0), *_klines_then_breakout()]
    await sup.run(lambda: _source_from(events), on_trigger=_async_appender(captured))
    assert captured == []


async def test_fresh_aggtrade_with_stale_bookticker_blocks_trigger() -> None:
    # (b) A fresh aggTrade cannot rescue a stale bookTicker: the quote is what
    # the spread guard depends on, so a stale quote blocks the trigger.
    clock = _Clock(_T0 + dt.timedelta(seconds=300))  # book is 300s old (> 120)
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    events: list[FuturesWsEvent] = [
        _book(now_offset=0),  # stale quote at T0
        _agg(now_offset=290),  # fresh trade at T0+290s
        *_klines_then_breakout(),
    ]
    await sup.run(lambda: _source_from(events), on_trigger=_async_appender(captured))
    assert captured == []


async def test_fresh_bookticker_emits_trigger_with_book_age() -> None:
    # (c) A fresh bookTicker carries bid/ask and a valid book age into the
    # trigger, so the downstream spread/freshness guard gets real values.
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    events: list[FuturesWsEvent] = [_book(now_offset=0), *_klines_then_breakout()]
    await sup.run(lambda: _source_from(events), on_trigger=_async_appender(captured))
    assert len(captured) == 1
    assert captured[0].bid_price is not None
    assert captured[0].ask_price is not None
    assert captured[0].data_age_seconds == 5.0  # bookTicker age, not aggTrade


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


async def test_run_with_reconnect_recovers_after_websocket_close() -> None:
    # websockets.exceptions.ConnectionClosed is NOT a ConnectionError/OSError
    # subclass — the reconnect loop must catch it explicitly or a real socket
    # close would crash the daemon instead of reconnecting.
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    slept: list[float] = []
    calls = {"n": 0}

    def factory() -> AsyncIterator[FuturesWsEvent]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _raises_connection_closed()
        return _source_from(_breakout_sequence())

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    await sup.run_with_reconnect(
        factory, on_trigger=_async_appender(captured), sleep=fake_sleep
    )
    assert len(captured) == 1  # recovered on the 2nd connection after a WS close
    assert len(slept) == 1


async def _raises_after_book() -> AsyncIterator[FuturesWsEvent]:
    yield _book()
    raise ConnectionError("socket dropped")


async def _raises_connection_closed() -> AsyncIterator[FuturesWsEvent]:
    yield _book()
    raise ConnectionClosedError(None, None)


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
