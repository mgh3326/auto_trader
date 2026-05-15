"""DB boundary for the daily candle store.

This module knows about the database. It does NOT call external APIs.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from app.services.candles_sync_common import SyncTableConfig


class MarketKey(enum.StrEnum):
    US = "us"
    KR = "kr"
    CRYPTO = "crypto"


_TABLE_CONFIGS: dict[MarketKey, SyncTableConfig] = {
    MarketKey.US: SyncTableConfig(table_name="us_candles_1d", partition_col="exchange"),
    MarketKey.KR: SyncTableConfig(table_name="kr_candles_1d", partition_col="venue"),
    MarketKey.CRYPTO: SyncTableConfig(
        table_name="crypto_candles_1d", partition_col="market"
    ),
}


@dataclass(frozen=True, slots=True)
class DailyCandleRow:
    time_utc: datetime
    symbol: str
    partition: str  # exchange (US) / venue (KR) / market (crypto)
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: float
    value: float
    source: str


class _RowcountResult:
    rowcount: int | None


class DailyCandlesRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """Expose the underlying session for callers that need to run their own queries.

        Used by the daily-candle sync orchestrator for universe-resolution queries
        that should share the repository's session and transaction context.
        """
        return self._session

    @staticmethod
    def _config(market: MarketKey) -> SyncTableConfig:
        return _TABLE_CONFIGS[market]

    @staticmethod
    def _supports_adj_close(market: MarketKey) -> bool:
        return market == MarketKey.US

    async def upsert_rows(
        self, *, market: MarketKey, rows: list[DailyCandleRow]
    ) -> int:
        if not rows:
            return 0

        cfg = self._config(market)
        upsert_sql = self._build_market_upsert(
            cfg, with_adj_close=self._supports_adj_close(market)
        )
        payload: list[dict[str, object]] = []
        for row in rows:
            entry: dict[str, object] = {
                "time": row.time_utc,
                "symbol": row.symbol,
                cfg.partition_col: row.partition,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "value": row.value,
                "source": row.source,
            }
            if self._supports_adj_close(market):
                entry["adj_close"] = row.adj_close
            payload.append(entry)

        result = cast(
            "_RowcountResult",
            cast(object, await self._session.execute(upsert_sql, payload)),
        )
        return max(int(result.rowcount or 0), 0)

    @staticmethod
    def _build_market_upsert(
        cfg: SyncTableConfig, *, with_adj_close: bool
    ) -> TextClause:
        cols = [
            "time",
            "symbol",
            cfg.partition_col,
            "open",
            "high",
            "low",
            "close",
            "volume",
            "value",
            "source",
        ]
        if with_adj_close:
            cols.insert(7, "adj_close")
        placeholders = ", ".join(f":{c}" for c in cols)
        col_list = ", ".join(cols)
        update_cols = [
            c for c in cols if c not in {"time", "symbol", cfg.partition_col}
        ]
        update_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
        return text(
            f"""
            INSERT INTO public.{cfg.table_name} ({col_list})
            VALUES ({placeholders})
            ON CONFLICT (time, symbol, {cfg.partition_col}) DO UPDATE
            SET {update_clause}, ingested_at = now()
            WHERE public.{cfg.table_name}.source = 'yahoo_fallback'
               OR EXCLUDED.source = 'kis'
               OR EXCLUDED.source = public.{cfg.table_name}.source
            """
        )

    async def latest_time_utc(
        self, *, market: MarketKey, symbol: str, partition: str
    ) -> datetime | None:
        cfg = self._config(market)
        sql = text(
            f"""
            SELECT MAX(time) AS latest
            FROM public.{cfg.table_name}
            WHERE symbol = :symbol AND {cfg.partition_col} = :partition
            """
        )
        result = await self._session.execute(
            sql, {"symbol": symbol, "partition": partition}
        )
        row = result.first()
        if row is None or row.latest is None:
            return None
        return row.latest

    async def fetch_recent(
        self, *, market: MarketKey, symbol: str, partition: str, count: int
    ) -> list[DailyCandleRow]:
        cfg = self._config(market)
        adj_close_select = (
            "adj_close, " if self._supports_adj_close(market) else "NULL AS adj_close, "
        )
        sql = text(
            f"""
            SELECT time, symbol, {cfg.partition_col} AS partition,
                   open, high, low, close, {adj_close_select}volume, value, source
            FROM public.{cfg.table_name}
            WHERE symbol = :symbol AND {cfg.partition_col} = :partition
            ORDER BY time DESC
            LIMIT :count
            """
        )
        result = await self._session.execute(
            sql, {"symbol": symbol, "partition": partition, "count": int(count)}
        )
        out: list[DailyCandleRow] = []
        for row in result.mappings().all():
            out.append(
                DailyCandleRow(
                    time_utc=row["time"],
                    symbol=row["symbol"],
                    partition=row["partition"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    adj_close=(
                        float(row["adj_close"])
                        if row["adj_close"] is not None
                        else None
                    ),
                    volume=float(row["volume"]),
                    value=float(row["value"]),
                    source=row["source"],
                )
            )
        return list(reversed(out))  # ascending order for consumers
