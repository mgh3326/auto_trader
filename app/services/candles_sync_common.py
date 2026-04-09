"""Shared utilities for candles sync services (KR / US).

kr_candles_sync_service, us_candles_sync_service 가 공유하는 함수 모음.
ohlcv_cache_common.py 와 동일한 패턴으로 사용.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class SyncTableConfig:
    """Market-specific table metadata for candle sync SQL generation."""

    table_name: str  # e.g. "kr_candles_1m", "us_candles_1m"
    partition_col: str  # e.g. "venue", "exchange"


def normalize_mode(mode: str) -> Literal["incremental", "backfill"]:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"incremental", "backfill"}:
        raise ValueError("mode must be 'incremental' or 'backfill'")
    return cast(Literal["incremental", "backfill"], normalized)


def parse_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None


def build_cursor_sql(cfg: SyncTableConfig) -> TextClause:
    return text(
        f"""
    SELECT MAX(time)
    FROM public.{cfg.table_name}
    WHERE symbol = :symbol
      AND {cfg.partition_col} = :{cfg.partition_col}
    """
    )


def build_upsert_sql(cfg: SyncTableConfig) -> TextClause:
    t = cfg.table_name
    p = cfg.partition_col
    return text(
        f"""
    INSERT INTO public.{t}
        (time, symbol, {p}, open, high, low, close, volume, value)
    VALUES
        (:time, :symbol, :{p}, :open, :high, :low, :close, :volume, :value)
    ON CONFLICT (time, symbol, {p})
    DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        value = EXCLUDED.value
    WHERE
        {t}.open IS DISTINCT FROM EXCLUDED.open
        OR {t}.high IS DISTINCT FROM EXCLUDED.high
        OR {t}.low IS DISTINCT FROM EXCLUDED.low
        OR {t}.close IS DISTINCT FROM EXCLUDED.close
        OR {t}.volume IS DISTINCT FROM EXCLUDED.volume
        OR {t}.value IS DISTINCT FROM EXCLUDED.value
    """
    )


def build_symbol_union(
    kis_holdings: Sequence[object],
    manual_holdings: Sequence[object],
    *,
    holdings_field: str,
    normalize_fn: Callable[[object], str | None],
) -> set[str]:
    symbols: set[str] = set()

    for item in kis_holdings:
        raw = (
            cast(dict[str, object], item).get(holdings_field)
            if isinstance(item, dict)
            else getattr(item, holdings_field, None)
        )
        symbol = normalize_fn(raw)
        if symbol is not None:
            symbols.add(symbol)

    for holding in manual_holdings:
        symbol = normalize_fn(getattr(holding, "ticker", None))
        if symbol is not None:
            symbols.add(symbol)

    return symbols


async def read_cursor_utc(
    session: AsyncSession,
    cursor_sql: object,
    params: dict[str, object],
) -> datetime | None:
    result = await session.execute(cursor_sql, params)
    value = result.scalar_one_or_none()
    return value if isinstance(value, datetime) else None
