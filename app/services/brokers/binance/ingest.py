"""ROB-285 — Bridge from KlineEvent → MinuteCandlesRepository.

Caches ``(venue=binance, product=spot, venue_symbol)`` → ``instrument_id``
lookups in memory to reduce DB churn during normal streaming. On cache
miss, queries ``crypto_instruments``; on still-missing, logs ``WARNING``
and skips — the adapter does NOT auto-create instrument rows. Operators
or a follow-up seeding script own the master-table writes.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.ws_client import KlineEvent
from app.services.minute_candles.repository import (
    MinuteCandleRow,
    MinuteCandlesRepository,
)

logger = logging.getLogger("app.services.brokers.binance.ingest")


class BinanceCandleIngester:
    """Routes closed ``KlineEvent`` rows into ``crypto_candles_1m``."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        repository: MinuteCandlesRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or MinuteCandlesRepository(session=session)
        # venue_symbol → instrument_id (spot only).
        self._cache: dict[str, int] = {}

    async def _resolve(self, venue_symbol: str) -> int | None:
        cached = self._cache.get(venue_symbol)
        if cached is not None:
            return cached
        result = await self._session.execute(
            select(CryptoInstrument.id).where(
                CryptoInstrument.venue == "binance",
                CryptoInstrument.product == "spot",
                CryptoInstrument.venue_symbol == venue_symbol,
            )
        )
        row = result.first()
        if row is None:
            return None
        instrument_id = int(row[0])
        self._cache[venue_symbol] = instrument_id
        return instrument_id

    async def ingest(self, event: KlineEvent) -> bool:
        """Returns True if the kline was persisted, False if skipped."""
        if not event.is_closed:
            # Defensive — WS client should drop these already (§B.3).
            return False
        instrument_id = await self._resolve(event.symbol)
        if instrument_id is None:
            logger.warning(
                "binance.ingest skip: no crypto_instruments row for "
                "(binance, spot, %s)",
                event.symbol,
            )
            return False
        await self._repo.upsert_rows(
            rows=[
                MinuteCandleRow(
                    instrument_id=instrument_id,
                    time_utc=event.open_time,
                    open=float(event.open),
                    high=float(event.high),
                    low=float(event.low),
                    close=float(event.close),
                    base_volume=float(event.base_volume),
                    quote_volume=float(event.quote_volume),
                    trade_count=event.trade_count,
                    is_closed=True,
                    source="binance_sdk_ws",
                    source_event_at=event.close_time,
                )
            ]
        )
        return True
