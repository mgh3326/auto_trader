"""DB boundary for the daily candle store.

This module knows about the database. It does NOT call external APIs.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import bindparam, text
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
    # ROB-284: crypto_candles_1d no longer has a `market`/`symbol` partition;
    # identity is `(instrument_id, time)`. The partition_col entry is kept
    # only so legacy KR/US-shape config consumers don't NPE — the crypto
    # write/read paths in this class take a separate code branch and
    # resolve `(symbol, partition)` -> `instrument_id` at the boundary.
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


def _recent_time_floor(count: int, *, now: datetime) -> datetime:
    """Lower time bound for chunk exclusion on daily-candle reads.

    Sized generously (>= 400 days, or count*3 calendar days) so the bounded
    window never returns fewer rows than the unbounded LIMIT would for
    realistic history — chunk exclusion is a speed hint, not a data filter.
    """
    window_days = max(400, int(count) * 3)
    return now - timedelta(days=window_days)


def _crypto_venue_for_partition(partition: str) -> str:
    """Map a legacy crypto ``partition`` label to its ``crypto_instruments.venue`` value.

    Today only Upbit KRW is producing crypto rows; ``partition='upbit_krw'`` maps
    to ``venue='upbit'``. Children B/C will add Binance/Alpaca mappings via
    additional rows in ``crypto_instruments``.
    """
    return "upbit" if partition == "upbit_krw" else partition.split("_")[0]


def _build_kr_us_recent_sql(partition_col: str, adj_close_select: str) -> str:
    return f"""
        SELECT time, symbol, {partition_col} AS partition,
               open, high, low, close, {adj_close_select}volume, value, source
        FROM public.{{table_name}}
        WHERE symbol = :symbol AND {partition_col} = :partition
          AND time >= :time_floor
        ORDER BY time DESC
        LIMIT :count
    """


_CRYPTO_RECENT_SQL = """
    SELECT time, :symbol AS symbol, :partition AS partition,
           open, high, low, close,
           NULL::numeric AS adj_close,
           base_volume AS volume, quote_volume AS value, source
    FROM public.crypto_candles_1d
    WHERE instrument_id = :iid
      AND time >= :time_floor
    ORDER BY time DESC
    LIMIT :count
"""


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
        self,
        *,
        market: MarketKey,
        rows: list[DailyCandleRow],
        update_adj_close: bool = True,
    ) -> int:
        """Upsert daily candle rows.

        ``update_adj_close=False`` (US only) keeps ``adj_close`` out of the
        ON CONFLICT UPDATE SET so a frame without adjusted closes (plain
        Yahoo/Toss write-back) does not null existing ``yahoo_fallback``
        values. New rows still insert ``adj_close`` (as NULL).
        """
        if not rows:
            return 0

        if market == MarketKey.CRYPTO:
            # ROB-284: crypto_candles_1d uses instrument_id FK; resolve at
            # the boundary so callers continue to pass legacy (symbol,
            # partition).
            return await self._upsert_crypto_rows(rows=rows)

        cfg = self._config(market)
        upsert_sql = self._build_market_upsert(
            cfg,
            with_adj_close=self._supports_adj_close(market),
            update_adj_close=update_adj_close,
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

    async def resolve_crypto_instrument_ids(
        self, *, symbols: list[str], partition: str
    ) -> dict[str, int]:
        """Batch-resolve legacy ``(symbol, partition)`` -> ``instrument_id``.

        Single ``SELECT ... WHERE venue_symbol IN (:symbols)`` so a fan-out over
        many crypto symbols collapses to one round-trip. Unknown symbols are
        silently absent; callers that need fail-on-unknown must look them up
        explicitly via :meth:`_resolve_instrument_id`.
        """
        normalized = sorted(
            {str(symbol).strip().upper() for symbol in symbols if symbol}
        )
        if not normalized:
            return {}
        sql = text(
            "SELECT venue_symbol, id FROM crypto_instruments "
            "WHERE venue = :venue AND product = 'spot' "
            "AND venue_symbol IN :symbols"
        ).bindparams(bindparam("symbols", expanding=True))
        result = await self._session.execute(
            sql,
            {
                "venue": _crypto_venue_for_partition(partition),
                "symbols": normalized,
            },
        )
        return {str(row.venue_symbol): int(row.id) for row in result}

    async def _resolve_instrument_id(self, *, symbol: str, partition: str) -> int:
        """Single-symbol identity resolver. Raises ``LookupError`` if not seeded."""
        resolved = await self.resolve_crypto_instrument_ids(
            symbols=[symbol], partition=partition
        )
        iid = resolved.get(str(symbol).strip().upper())
        if iid is None:
            venue = _crypto_venue_for_partition(partition)
            raise LookupError(
                f"No crypto_instruments row for venue={venue!r} symbol={symbol!r}; "
                "seed the instrument before writing candles."
            )
        return iid

    async def fetch_recent_crypto_by_instrument_id(
        self,
        *,
        instrument_id: int,
        symbol: str,
        partition: str,
        count: int,
    ) -> list[DailyCandleRow]:
        """Read crypto daily candles directly by instrument_id (no identity lookup)."""
        sql = text(_CRYPTO_RECENT_SQL)
        result = await self._session.execute(
            sql,
            {
                "iid": int(instrument_id),
                "symbol": symbol,
                "partition": partition,
                "count": int(count),
                "time_floor": _recent_time_floor(int(count), now=datetime.now(UTC)),
            },
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
                    adj_close=None,
                    volume=(float(row["volume"]) if row["volume"] is not None else 0.0),
                    value=float(row["value"]) if row["value"] is not None else 0.0,
                    source=row["source"],
                )
            )
        return list(reversed(out))

    async def upsert_crypto_rows_by_instrument_id(
        self, *, instrument_id: int, rows: list[DailyCandleRow]
    ) -> int:
        """Upsert crypto daily candles directly by instrument_id.

        Conflict policy preserves the original close when the existing row is
        already closed with the same source.
        """
        if not rows:
            return 0
        payload = [
            {
                "instrument_id": int(instrument_id),
                "time": row.time_utc,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "base_volume": row.volume,
                "quote_volume": row.value,
                "is_closed": True,
                "source": row.source,
            }
            for row in rows
        ]
        sql = text(
            """
            INSERT INTO public.crypto_candles_1d (
                instrument_id, time, open, high, low, close,
                base_volume, quote_volume, is_closed, source
            ) VALUES (
                :instrument_id, :time, :open, :high, :low, :close,
                :base_volume, :quote_volume, :is_closed, :source
            )
            ON CONFLICT (instrument_id, time) DO UPDATE
            SET open         = EXCLUDED.open,
                high         = EXCLUDED.high,
                low          = EXCLUDED.low,
                close        = EXCLUDED.close,
                base_volume  = EXCLUDED.base_volume,
                quote_volume = EXCLUDED.quote_volume,
                is_closed    = EXCLUDED.is_closed,
                source       = EXCLUDED.source,
                ingested_at  = now()
            WHERE
                NOT (public.crypto_candles_1d.is_closed = TRUE
                     AND EXCLUDED.is_closed = TRUE
                     AND public.crypto_candles_1d.source = EXCLUDED.source)
            """
        )
        result = cast(
            "_RowcountResult",
            cast(object, await self._session.execute(sql, payload)),
        )
        return max(int(result.rowcount or 0), 0)

    async def _upsert_crypto_rows(self, *, rows: list[DailyCandleRow]) -> int:
        if not rows:
            return 0

        # Group rows by (symbol, partition) so unique identities resolve once,
        # not once per row.
        identities_by_pair: dict[tuple[str, str], int] = {}
        for row in rows:
            pair = (str(row.symbol).strip().upper(), str(row.partition))
            identities_by_pair.setdefault(
                pair,
                await self._resolve_instrument_id(symbol=pair[0], partition=pair[1]),
            )

        total = 0
        for (symbol, partition), iid in identities_by_pair.items():
            identity_rows = [
                row
                for row in rows
                if str(row.symbol).strip().upper() == symbol
                and str(row.partition) == partition
            ]
            total += await self.upsert_crypto_rows_by_instrument_id(
                instrument_id=iid, rows=identity_rows
            )
        return total

    @staticmethod
    def _build_market_upsert(
        cfg: SyncTableConfig, *, with_adj_close: bool, update_adj_close: bool = True
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
        excluded_from_update = {"time", "symbol", cfg.partition_col}
        if with_adj_close and not update_adj_close:
            excluded_from_update.add("adj_close")
        update_cols = [c for c in cols if c not in excluded_from_update]
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
        if market == MarketKey.CRYPTO:
            # ROB-284: resolve instrument first; this avoids touching the
            # legacy (symbol, market) shape that the table no longer has.
            try:
                iid = await self._resolve_instrument_id(
                    symbol=symbol, partition=partition
                )
            except LookupError:
                return None
            sql = text(
                "SELECT MAX(time) AS latest FROM public.crypto_candles_1d "
                "WHERE instrument_id = :iid"
            )
            result = await self._session.execute(sql, {"iid": iid})
            row = result.first()
            if row is None or row.latest is None:
                return None
            return row.latest

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

    async def fetch_range(
        self,
        *,
        market: MarketKey,
        symbol: str,
        partition: str,
        start: datetime,
        end: datetime,
    ) -> list[DailyCandleRow]:
        """Fetch daily candles with ``start <= time <= end`` (ascending).

        Read-only window query used for deterministic forecast resolution
        (ROB-650): unlike ``fetch_recent`` (latest-N), this returns exactly the
        rows inside the resolution window regardless of how far in the past it
        sits, so the same forecast resolves to the same outcome whenever it is
        run.
        """
        if market == MarketKey.CRYPTO:
            try:
                iid = await self._resolve_instrument_id(
                    symbol=symbol, partition=partition
                )
            except LookupError:
                return []
            sql = text(
                """
                SELECT time, :symbol AS symbol, :partition AS partition,
                       open, high, low, close,
                       NULL::numeric AS adj_close,
                       base_volume AS volume, quote_volume AS value, source
                FROM public.crypto_candles_1d
                WHERE instrument_id = :iid AND time >= :start AND time <= :end
                ORDER BY time ASC
                """
            )
            result = await self._session.execute(
                sql,
                {
                    "iid": iid,
                    "symbol": symbol,
                    "partition": partition,
                    "start": start,
                    "end": end,
                },
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
                        adj_close=None,
                        volume=float(row["volume"])
                        if row["volume"] is not None
                        else 0.0,
                        value=float(row["value"]) if row["value"] is not None else 0.0,
                        source=row["source"],
                    )
                )
            return out

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
              AND time >= :start AND time <= :end
            ORDER BY time ASC
            """
        )
        result = await self._session.execute(
            sql,
            {"symbol": symbol, "partition": partition, "start": start, "end": end},
        )
        out = []
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
        return out

    async def fetch_recent(
        self, *, market: MarketKey, symbol: str, partition: str, count: int
    ) -> list[DailyCandleRow]:
        if market == MarketKey.CRYPTO:
            # ROB-284: JOIN crypto_instruments back to reconstruct the
            # legacy (symbol, partition) shape consumers expect.
            try:
                iid = await self._resolve_instrument_id(
                    symbol=symbol, partition=partition
                )
            except LookupError:
                return []
            sql = text(_CRYPTO_RECENT_SQL)
            result = await self._session.execute(
                sql,
                {
                    "iid": iid,
                    "symbol": symbol,
                    "partition": partition,
                    "count": int(count),
                    "time_floor": _recent_time_floor(int(count), now=datetime.now(UTC)),
                },
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
                        adj_close=None,
                        volume=float(row["volume"])
                        if row["volume"] is not None
                        else 0.0,
                        value=float(row["value"]) if row["value"] is not None else 0.0,
                        source=row["source"],
                    )
                )
            return list(reversed(out))

        cfg = self._config(market)
        adj_close_select = (
            "adj_close, " if self._supports_adj_close(market) else "NULL AS adj_close, "
        )
        sql = text(
            _build_kr_us_recent_sql(cfg.partition_col, adj_close_select).format(
                table_name=cfg.table_name
            )
        )
        result = await self._session.execute(
            sql,
            {
                "symbol": symbol,
                "partition": partition,
                "count": int(count),
                "time_floor": _recent_time_floor(int(count), now=datetime.now(UTC)),
            },
        )
        out = []
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
