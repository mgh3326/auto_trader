"""Exact-decimal bar reads from the stored candle tables (SELECT only).

``DailyCandlesRepository.fetch_recent_batch`` converts to ``float``, which would
break the contract's exact-decimal rule, so the retro readers select the stored
``numeric`` columns themselves. The tables are the same ones the repository
reads; this module only reads them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.retro_v1.market import MarketInfo

_KR_DAILY: Final = """
    SELECT time, symbol, venue AS venue, open, high, low, close, volume, source
    FROM public.kr_candles_1d
    WHERE symbol IN :symbols AND time >= :lo AND time < :hi
    ORDER BY symbol, time, venue
    LIMIT :cap
"""
_US_DAILY: Final = """
    SELECT time, symbol, exchange AS venue, open, high, low, close, volume, source
    FROM public.us_candles_1d
    WHERE symbol IN :symbols AND time >= :lo AND time < :hi
    ORDER BY symbol, time, exchange
    LIMIT :cap
"""
_CRYPTO_DAILY: Final = """
    SELECT c.time, ci.venue_symbol AS symbol, ci.venue AS venue,
           c.open, c.high, c.low, c.close, c.base_volume AS volume, c.source
    FROM public.crypto_candles_1d AS c
    JOIN public.crypto_instruments AS ci ON ci.id = c.instrument_id
    WHERE ci.venue_symbol IN :symbols AND c.time >= :lo AND c.time < :hi
      AND c.is_closed = TRUE
    ORDER BY ci.venue_symbol, c.time, ci.venue
    LIMIT :cap
"""
# kr/us 1m bars come from the hk 6145 research tables (the public serving cache
# has no writer any more). The research tables store the provider (``source``),
# the session label, the per-bar value (KR: NULL unless proven) and the first
# receipt time, so those are selected too. One row per (time, symbol, venue):
# KRX and NTX bars are never merged.
_KR_MINUTE: Final = """
    SELECT b.time_utc AS time, b.symbol, b.venue,
           COALESCE(r.open, b.open) AS open, COALESCE(r.high, b.high) AS high,
           COALESCE(r.low, b.low) AS low, COALESCE(r.close, b.close) AS close,
           COALESCE(r.volume, b.volume) AS volume,
           CASE WHEN r.revised_at IS NULL THEN b.source ELSE 'KIS' END AS source,
           b.session_segment,
           CASE WHEN r.revised_at IS NULL THEN b.value ELSE r.value END AS value,
           b.retrieved_at, b.batch_id, r.revised_at,
           CASE WHEN (b.time_utc AT TIME ZONE 'Asia/Seoul')::time = TIME '15:30'
                THEN r.close END AS krx_official_close
    FROM research.kr_candles_1m AS b
    LEFT JOIN research.kr_auction_minute_revisions AS r USING (time_utc, symbol, venue)
    WHERE b.symbol IN :symbols AND b.time_utc >= :lo AND b.time_utc < :hi
    ORDER BY b.symbol, b.time_utc, b.venue
    LIMIT :cap
"""
_US_MINUTE: Final = """
    SELECT time_utc AS time, symbol, venue AS venue, open, high, low, close,
           volume, source, session_segment, value, retrieved_at, batch_id
    FROM research.us_candles_1m
    WHERE symbol IN :symbols AND time_utc >= :lo AND time_utc < :hi
    ORDER BY symbol, time_utc, venue
    LIMIT :cap
"""
_CRYPTO_MINUTE: Final = """
    SELECT c.time, ci.venue_symbol AS symbol, ci.venue AS venue,
           c.open, c.high, c.low, c.close, c.base_volume AS volume, c.source
    FROM public.crypto_candles_1m AS c
    JOIN public.crypto_instruments AS ci ON ci.id = c.instrument_id
    WHERE ci.venue_symbol IN :symbols AND c.time >= :lo AND c.time < :hi
      AND c.is_closed = TRUE
    ORDER BY ci.venue_symbol, c.time, ci.venue
    LIMIT :cap
"""

_SQL: Final = {
    ("kr", "1d"): _KR_DAILY,
    ("us", "1d"): _US_DAILY,
    ("crypto", "1d"): _CRYPTO_DAILY,
    ("kr", "1m"): _KR_MINUTE,
    ("us", "1m"): _US_MINUTE,
    ("crypto", "1m"): _CRYPTO_MINUTE,
}
SUPPORTED_TIMEFRAMES: Final = ("1m", "1d")

#: Earliest stored bar of a symbol in the research minute table (any venue).
_EARLIEST_MINUTE: Final = {
    "kr": (
        "SELECT MIN(time_utc) AS earliest FROM research.kr_candles_1m "
        "WHERE symbol = :symbol"
    ),
    "us": (
        "SELECT MIN(time_utc) AS earliest FROM research.us_candles_1m "
        "WHERE symbol = :symbol"
    ),
}

#: Copy of ``research_candles.session_coverage.is_backfill_batch`` (retro readers
#: may not import that package); ``test_other_readers`` pins the two together.
_BACKFILL_MARKERS: Final = ("backfill",)
_PROMOTE_PREFIX: Final = "promote-"


def is_first_receipt_batch(batch_id: str | None) -> bool:
    """A row's ``retrieved_at`` is a first receipt unless its batch is a backfill."""
    if batch_id is None:
        return False
    lowered = batch_id.lower()
    return not (
        any(marker in lowered for marker in _BACKFILL_MARKERS)
        or lowered.startswith(_PROMOTE_PREFIX)
    )


def has_minute_extras(market: MarketInfo, tf: str) -> bool:
    """kr/us 1m bars come from the research tables and carry the extra fields."""
    return tf == "1m" and market.key in _EARLIEST_MINUTE


@dataclass(frozen=True, slots=True)
class MinuteExtras:
    """Research-table fields of a kr/us 1m bar (absent for daily and crypto)."""

    session_segment: str | None
    value: Decimal | None
    #: ``retrieved_at`` only when the row's batch is a first receipt, else None.
    first_received_at: datetime | None
    revised_at: datetime | None = None
    krx_official_close: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    time: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    venue: str | None
    source: str | None
    minute: MinuteExtras | None = None


def supports(market: MarketInfo, tf: str) -> bool:
    return (market.key, tf) in _SQL


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, float):
        return None if value is None else Decimal(repr(value))
    return value if isinstance(value, Decimal) else Decimal(str(value))


async def load_bars(
    session: AsyncSession,
    market: MarketInfo,
    tf: str,
    symbols: Sequence[str],
    *,
    lo: datetime,
    hi: datetime,
    cap: int,
) -> list[Bar]:
    """Bars in ``[lo, hi)`` for ``symbols``; at most ``cap`` rows (one SELECT)."""
    if not symbols:
        return []
    statement = text(_SQL[(market.key, tf)]).bindparams(
        bindparam("symbols", expanding=True)
    )
    result = await session.execute(
        statement, {"symbols": list(symbols), "lo": lo, "hi": hi, "cap": cap}
    )
    extras = has_minute_extras(market, tf)
    return [
        Bar(
            symbol=str(row["symbol"]),
            time=row["time"],
            open=_decimal(row["open"]),
            high=_decimal(row["high"]),
            low=_decimal(row["low"]),
            close=_decimal(row["close"]),
            volume=_decimal(row["volume"]),
            venue=row["venue"],
            source=row["source"],
            minute=_minute_extras(row) if extras else None,
        )
        for row in result.mappings().all()
    ]


def _minute_extras(row: Any) -> MinuteExtras:
    retrieved = row["retrieved_at"]
    first = retrieved if is_first_receipt_batch(row["batch_id"]) else None
    return MinuteExtras(
        session_segment=row["session_segment"],
        value=_decimal(row["value"]),
        first_received_at=first,
        revised_at=row.get("revised_at"),
        krx_official_close=_decimal(row.get("krx_official_close")),
    )


async def earliest_minute_time(
    session: AsyncSession, market: MarketInfo, symbol: str
) -> datetime | None:
    """First stored research 1m bar of ``symbol`` (any venue), or None if none."""
    result = await session.execute(
        text(_EARLIEST_MINUTE[market.key]), {"symbol": symbol}
    )
    row = result.mappings().first()
    return row["earliest"] if row is not None else None
