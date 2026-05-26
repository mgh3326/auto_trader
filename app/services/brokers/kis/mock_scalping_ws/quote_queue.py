"""ROB-321 PR4b — callback → async-iterator adapter.

``KISQuoteWebSocket`` delivers parsed frames via ``on_tick``/``on_book``
callbacks; the supervisor consumes an async iterator. ``QuoteEventQueue`` bridges
the two: the WS callbacks enqueue, ``iterator()`` yields. Bounded — if the
consumer can't keep up the oldest-vs-newest tradeoff is to drop newest and count
it (a stalled scalper must not grow memory unbounded). Read-side only; imports
no order/ledger code (import-guard safe).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
)

logger = logging.getLogger("rob321.kis_mock_scalping_ws")

QuoteEvent = QuoteTick | OrderBookSnapshot


class QuoteEventQueue:
    def __init__(self, maxsize: int = 2000) -> None:
        self._q: asyncio.Queue[QuoteEvent] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def on_tick(self, tick: QuoteTick) -> None:
        self._offer(tick)

    def on_book(self, book: OrderBookSnapshot) -> None:
        self._offer(book)

    def _offer(self, event: QuoteEvent) -> None:
        try:
            self._q.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning(
                "quote queue full; dropped event (total dropped=%s)", self.dropped
            )

    async def iterator(self) -> AsyncIterator[QuoteEvent]:
        while True:
            yield await self._q.get()
