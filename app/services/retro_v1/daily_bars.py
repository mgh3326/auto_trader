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
# The minute tables carry no provider column (kr_candles_1m documents that its
# venue is not provenance), so source is reported unknown for them.
_KR_MINUTE: Final = """
    SELECT time, symbol, venue AS venue, open, high, low, close, volume,
           NULL::text AS source
    FROM public.kr_candles_1m
    WHERE symbol IN :symbols AND time >= :lo AND time < :hi
    ORDER BY symbol, time, venue
    LIMIT :cap
"""
_US_MINUTE: Final = """
    SELECT time, symbol, exchange AS venue, open, high, low, close, volume,
           NULL::text AS source
    FROM public.us_candles_1m
    WHERE symbol IN :symbols AND time >= :lo AND time < :hi
    ORDER BY symbol, time, exchange
    LIMIT :cap
"""
_CRYPTO_MINUTE: Final = """
    SELECT c.time, ci.venue_symbol AS symbol, ci.venue AS venue,
           c.open, c.high, c.low, c.close, c.base_volume AS volume,
           NULL::text AS source
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
        )
        for row in result.mappings().all()
    ]
