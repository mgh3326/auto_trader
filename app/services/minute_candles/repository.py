"""ROB-284 — DB boundary for crypto 1m candle store.

Writes to `crypto_candles_1m` via `instrument_id`. Idempotent: a closed
candle is never overwritten by another source's row at the same bucket.
The upsert WHERE clause mirrors `DailyCandlesRepository`'s
closed-candle-protection behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause


@dataclass(frozen=True, slots=True)
class MinuteCandleRow:
    instrument_id: int
    time_utc: datetime
    open: float | Decimal
    high: float | Decimal
    low: float | Decimal
    close: float | Decimal
    base_volume: float | Decimal
    quote_volume: float | Decimal | None = None
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

    async def insert_rows(self, *, rows: list[MinuteCandleRow]) -> int:
        """Insert only new buckets and return the number of inserted rows.

        This is for historical REST collectors where the first observation is
        authoritative. Re-running a collector must not rewrite that evidence.
        """
        if not rows:
            return 0
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
        inserted = 0
        # 15 values per row; stay below PostgreSQL's 65535 bind parameter cap.
        for offset in range(0, len(payload), 2000):
            inserted += await self._insert_only_chunk(payload[offset : offset + 2000])
        return inserted

    async def _insert_only_chunk(self, payload: list[dict[str, object]]) -> int:
        columns = (
            "instrument_id",
            "time",
            "open",
            "high",
            "low",
            "close",
            "base_volume",
            "quote_volume",
            "trade_count",
            "vwap",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
            "is_closed",
            "source",
            "source_event_at",
        )
        values = []
        params: dict[str, object] = {}
        for row_index, row in enumerate(payload):
            names = []
            for column in columns:
                name = f"{column}_{row_index}"
                names.append(f":{name}")
                params[name] = row[column]
            values.append(f"({', '.join(names)})")
        sql = text(
            "INSERT INTO public.crypto_candles_1m ("
            + ", ".join(columns)
            + ") VALUES "
            + ", ".join(values)
            + " ON CONFLICT (instrument_id, time) DO NOTHING RETURNING time"
        )
        result = await self._session.execute(sql, params)
        return len(result.scalars().all())

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
