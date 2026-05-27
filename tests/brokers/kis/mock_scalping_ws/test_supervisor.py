"""KIS mock scalping supervisor tests (ROB-321 PR3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping.signal import SignalConfig
from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
)
from app.services.brokers.kis.mock_scalping_ws.supervisor import (
    KisScalpingSupervisor,
    TriggerEvent,
)

SYMBOL = "005930"

# Small config so a handful of candles suffice; disable no-chase for the
# trigger-path tests (separate signal tests already cover CHASE_TOO_FAR).
_CFG = SignalConfig(
    sma_fast=2, sma_slow=3, breakout_lookback=3, max_chase_bps=Decimal("100000")
)


def _tick(price: float) -> QuoteTick:
    return QuoteTick(symbol=SYMBOL, last_price=price, ts="000000")


def _book(bid: float = 999.0, ask: float = 1001.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol=SYMBOL, bid=bid, ask=ask, bid_qty=10.0, ask_qty=10.0
    )


def _clock_from(times: list[float]):
    it = iter(times)
    return lambda: next(it)


def _source(events: list):
    async def _agen() -> AsyncIterator:
        for e in events:
            yield e

    return _agen


async def _collect(
    supervisor: KisScalpingSupervisor, events, times
) -> list[TriggerEvent]:
    triggers: list[TriggerEvent] = []

    async def _on_trigger(t: TriggerEvent) -> None:
        triggers.append(t)

    supervisor._clock = _clock_from(times)
    await supervisor.run(_source(events), on_trigger=_on_trigger)
    return triggers


def _uptrend_stream() -> tuple[list, list[float]]:
    """5 (book, tick) pairs with ascending prices → BUY breakout on the last."""
    events: list = []
    times: list[float] = []
    for i, price in enumerate([1000.0, 1001.0, 1002.0, 1003.0, 1004.0]):
        events.append(_book())
        times.append(i * 60.0)
        events.append(_tick(price))
        times.append(i * 60.0 + 1)
    return events, times


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uptrend_breakout_emits_trigger_with_fresh_book() -> None:
    sup = KisScalpingSupervisor(symbols=[SYMBOL], signal_config=_CFG)
    events, times = _uptrend_stream()
    triggers = await _collect(sup, events, times)
    assert len(triggers) == 1
    assert triggers[0].symbol == SYMBOL
    assert triggers[0].side == "BUY"
    assert triggers[0].bid == 999.0
    assert triggers[0].account_mode == "kis_mock"
    assert triggers[0].data_age_seconds is not None
    assert triggers[0].data_age_seconds <= 60.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_book_suppresses_trigger() -> None:
    sup = KisScalpingSupervisor(
        symbols=[SYMBOL], signal_config=_CFG, max_data_age_seconds=60.0
    )
    # One book at t=0 then only ticks afterwards → book goes stale by the breakout.
    events: list = [_book()]
    times: list[float] = [0.0]
    for i, price in enumerate([1000.0, 1001.0, 1002.0, 1003.0, 1004.0]):
        events.append(_tick(price))
        times.append(i * 60.0 + 1)
    triggers = await _collect(sup, events, times)
    assert triggers == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orderbook_and_unknown_symbol_do_not_trigger() -> None:
    sup = KisScalpingSupervisor(symbols=[SYMBOL], signal_config=_CFG)
    other = QuoteTick(symbol="999999", last_price=1.0, ts="000000")
    triggers = await _collect(sup, [_book(), other], [0.0, 1.0])
    assert triggers == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_debounce_suppresses_second_trigger() -> None:
    sup = KisScalpingSupervisor(
        symbols=[SYMBOL], signal_config=_CFG, debounce_seconds=300.0
    )
    events, times = _uptrend_stream()
    # Append another fresh book + breakout tick one minute later (within debounce).
    events.append(_book())
    times.append(300.0)
    events.append(_tick(1010.0))
    times.append(301.0)
    triggers = await _collect(sup, events, times)
    assert len(triggers) == 1  # second breakout debounced
