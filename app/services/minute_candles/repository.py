"""ROB-284 — DB boundary for crypto 1m candle store.

Writes to `crypto_candles_1m` via `instrument_id`. Idempotent: a closed
candle is never overwritten by another source's row at the same bucket.
The upsert WHERE clause mirrors `DailyCandlesRepository`'s
closed-candle-protection behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause


@dataclass(frozen=True, slots=True)
class MinuteCandleRow:
    instrument_id: int
    time_utc: datetime
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float | None = None
    trade_count: int | None = None
    vwap: float | None = None
    taker_buy_base_volume: float | None = None
    taker_buy_quote_volume: float | None = None
    is_closed: bool = True
    source: str = ""
    source_event_at: datetime | None = None


class _RowcountResult:
    rowcount: int | None


class MinuteCandlesRepository:
    """Writes to crypto_candles_1m via instrument_id.

    Idempotent: a closed candle is never overwritten by a less-trustworthy
    source. The upsert WHERE clause mirrors DailyCandlesRepository's
    closed-candle-protection behavior.
    """

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def upsert_rows(self, *, rows: list[MinuteCandleRow]) -> int:
        if not rows:
            return 0
        sql = self._build_upsert()
        payload = [
            {
                "instrument_id": r.instrument_id,
                "time": r.time_utc,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "base_volume": r.base_volume,
                "quote_volume": r.quote_volume,
                "trade_count": r.trade_count,
                "vwap": r.vwap,
                "taker_buy_base_volume": r.taker_buy_base_volume,
                "taker_buy_quote_volume": r.taker_buy_quote_volume,
                "is_closed": r.is_closed,
                "source": r.source,
                "source_event_at": r.source_event_at,
            }
            for r in rows
        ]
        result = cast(
            "_RowcountResult",
            cast(object, await self._session.execute(sql, payload)),
        )
        return max(int(result.rowcount or 0), 0)

    @staticmethod
    def _build_upsert() -> TextClause:
        return text(
            """
            INSERT INTO public.crypto_candles_1m (
                instrument_id, time, open, high, low, close,
                base_volume, quote_volume, trade_count, vwap,
                taker_buy_base_volume, taker_buy_quote_volume,
                is_closed, source, source_event_at
            ) VALUES (
                :instrument_id, :time, :open, :high, :low, :close,
                :base_volume, :quote_volume, :trade_count, :vwap,
                :taker_buy_base_volume, :taker_buy_quote_volume,
                :is_closed, :source, :source_event_at
            )
            ON CONFLICT (instrument_id, time) DO UPDATE
            SET open                   = EXCLUDED.open,
                high                   = EXCLUDED.high,
                low                    = EXCLUDED.low,
                close                  = EXCLUDED.close,
                base_volume            = EXCLUDED.base_volume,
                quote_volume           = EXCLUDED.quote_volume,
                trade_count            = EXCLUDED.trade_count,
                vwap                   = EXCLUDED.vwap,
                taker_buy_base_volume  = EXCLUDED.taker_buy_base_volume,
                taker_buy_quote_volume = EXCLUDED.taker_buy_quote_volume,
                is_closed              = EXCLUDED.is_closed,
                source                 = EXCLUDED.source,
                source_event_at        = EXCLUDED.source_event_at,
                ingested_at            = now()
            WHERE
                -- Never overwrite a closed candle from the same source.
                NOT (public.crypto_candles_1m.is_closed = TRUE
                     AND EXCLUDED.is_closed = TRUE
                     AND public.crypto_candles_1m.source = EXCLUDED.source)
            """
        )
