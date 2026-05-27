"""Callback→async-iterator queue adapter tests (ROB-321 PR4b)."""

from __future__ import annotations

import pytest

from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
)
from app.services.brokers.kis.mock_scalping_ws.quote_queue import QuoteEventQueue


def _tick(p: float) -> QuoteTick:
    return QuoteTick(symbol="005930", last_price=p, ts="000000")


def _book() -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="005930", bid=1.0, ask=2.0, bid_qty=1.0, ask_qty=1.0
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_callbacks_enqueue_and_iterator_yields_in_order() -> None:
    q = QuoteEventQueue()
    q.on_tick(_tick(100.0))
    q.on_book(_book())
    q.on_tick(_tick(101.0))

    it = q.iterator()
    first = await anext(it)
    second = await anext(it)
    third = await anext(it)

    assert isinstance(first, QuoteTick) and first.last_price == 100.0
    assert isinstance(second, OrderBookSnapshot)
    assert isinstance(third, QuoteTick) and third.last_price == 101.0


@pytest.mark.unit
def test_full_queue_drops_and_counts() -> None:
    q = QuoteEventQueue(maxsize=2)
    q.on_tick(_tick(1.0))
    q.on_tick(_tick(2.0))
    q.on_tick(_tick(3.0))  # full -> dropped
    assert q.dropped == 1
