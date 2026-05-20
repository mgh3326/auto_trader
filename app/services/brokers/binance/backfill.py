"""ROB-285 — REST kline backfill engine with bounded caps.

Pagination is forward-in-time (``startTime`` anchored), advancing
``cursor`` past the last received kline's ``open_time``. Stops when:

- the API returns fewer rows than ``page_size`` (caught up), OR
- ``max_candles`` is reached, OR
- ``max_requests`` is reached.

If either cap is hit before catch-up, raises ``BinanceBackfillCapExceeded``.
The caller (Task 12 orchestration) is expected to react by marking the
instrument as ``manual_backfill_required`` via
``CryptoInstrumentHealthService``.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Protocol

from app.services.brokers.binance.dto import BinanceKlineRow
from app.services.brokers.binance.errors import BinanceBackfillCapExceeded


@dataclass(frozen=True, slots=True)
class BackfillCaps:
    max_candles: int
    max_requests: int
    page_size: int

    @classmethod
    def from_env(cls) -> BackfillCaps:
        return cls(
            max_candles=int(os.getenv("BINANCE_KLINE_BACKFILL_MAX_CANDLES", "5000")),
            max_requests=int(os.getenv("BINANCE_KLINE_BACKFILL_MAX_REQUESTS", "10")),
            page_size=int(os.getenv("BINANCE_KLINE_BACKFILL_PAGE_SIZE", "1000")),
        )


@dataclass(frozen=True, slots=True)
class BackfillResult:
    klines: list[BinanceKlineRow]
    requests_used: int


class _RestKlineClient(Protocol):
    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: dt.datetime,
        end_time: dt.datetime | None = None,
        limit: int,
    ) -> list[BinanceKlineRow]: ...


class RestBackfiller:
    """Forward, ``startTime``-anchored kline backfiller with bounded caps."""

    def __init__(self, *, rest: _RestKlineClient, caps: BackfillCaps) -> None:
        self._rest = rest
        self._caps = caps

    async def backfill(
        self,
        *,
        symbol: str,
        interval: str,
        since: dt.datetime,
    ) -> BackfillResult:
        out: list[BinanceKlineRow] = []
        requests = 0
        cursor = since
        while True:
            if requests >= self._caps.max_requests:
                raise BinanceBackfillCapExceeded(
                    f"max_requests={self._caps.max_requests} exceeded; "
                    f"collected {len(out)} klines for {symbol} {interval}"
                )
            if len(out) >= self._caps.max_candles:
                raise BinanceBackfillCapExceeded(
                    f"max_candles={self._caps.max_candles} exceeded; "
                    f"collected {len(out)} klines for {symbol} {interval}"
                )
            page = await self._rest.klines(
                symbol=symbol,
                interval=interval,
                start_time=cursor,
                limit=self._caps.page_size,
            )
            requests += 1
            if not page:
                break
            out.extend(page)
            if len(page) < self._caps.page_size:
                break  # caught up
            # Advance cursor past the last kline received to avoid duplicates.
            cursor = page[-1].open_time + dt.timedelta(milliseconds=1)
        return BackfillResult(klines=out, requests_used=requests)
