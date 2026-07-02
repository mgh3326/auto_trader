"""DB-first read-through service for daily OHLCV (ROB-639).

Extracted from ``market_data_indicators._cache_first_*`` so that
``get_ohlcv(period='day')`` can read KR/US daily candles from the DB before
falling back to live upstream APIs.

Design:

* Public helpers (``cache_is_fresh_equity``, ``rows_to_frame``...) are shared
  with ``market_data_indicators`` to avoid drift in freshness semantics.
* ``cache_first_kr`` / ``cache_first_us`` perform the READ-ONLY DB-first
  lookup and return ``None`` on miss/stale data so the caller can run its
  own fallback (KIS, Yahoo, Toss...) and optional write-back.

Freshness rule for equity caches (KR/US): the newest DB row must cover the
most-recent closed exchange session (``XKRX`` for KR, ``XNYS`` for US).
The calendar lookup uses ``exchange_calendars``; the KRX 15:35 KST close is
captured implicitly because ``XKRX`` sessions end at 15:30 KST (06:30 UTC).
"""

from __future__ import annotations

import datetime
import logging
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

from app.services.daily_candles.repository import DailyCandleRow

logger = logging.getLogger(__name__)

# Canonical OHLCV column ordering produced by ``rows_to_frame``. Kept here so
# consumers (``market_data_indicators`` and ``get_ohlcv``) share one source of
# truth for the frame shape.
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]


@lru_cache(maxsize=4)
def get_calendar(exchange: str):
    """Return (and cache) an ``exchange_calendars`` calendar by name."""
    return xcals.get_calendar(exchange)


def latest_exchange_session(exchange: str) -> datetime.date | None:
    """Return the most-recent past session date for the given exchange.

    ``exchange`` accepts any calendar name supported by exchange_calendars
    (e.g., ``'XKRX'`` for KRX, ``'XNYS'`` for NYSE). Returns ``None`` if the
    library raises (rare, but defensive for early/late session edges).
    """
    cal = get_calendar(exchange)
    now = datetime.datetime.now(datetime.UTC)
    try:
        session = cal.minute_to_past_session(pd.Timestamp(now), count=1)
    except Exception:
        return None
    return pd.Timestamp(session).date()


def cache_is_fresh_equity(rows: list[DailyCandleRow], exchange: str) -> bool:
    """Cache is fresh if the newest row covers the latest closed exchange session.

    For KR this encodes the 15:30 KST (06:30 UTC) session close of ``XKRX`` —
    a row timestamped after the latest session's close satisfies the rule.
    """
    if not rows:
        return False
    latest_session = latest_exchange_session(exchange)
    if latest_session is None:
        return False
    latest_row = max(r.time_utc for r in rows)
    return pd.Timestamp(latest_row).date() >= latest_session


def cache_is_fresh_crypto(rows: list[DailyCandleRow]) -> bool:
    """Return True if the newest row's timestamp is within the last 24 hours."""
    if not rows:
        return False
    newest = max(r.time_utc for r in rows)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=datetime.UTC)
    return datetime.datetime.now(datetime.UTC) - newest < datetime.timedelta(hours=24)


def rows_to_frame(rows: list[DailyCandleRow]) -> pd.DataFrame:
    """Convert a list of ``DailyCandleRow`` to a canonical OHLCV DataFrame.

    Returns an empty DataFrame (with the standard column set) when ``rows``
    is empty. Output is sorted ascending by date and reset-indexed.
    """
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    records = []
    for row in rows:
        ts = row.time_utc
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.UTC)
        records.append(
            {
                "date": ts.date(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "value": row.value,
            }
        )
    df = pd.DataFrame(records, columns=OHLCV_COLUMNS)
    return df.sort_values("date").reset_index(drop=True)


async def cache_first_kr(
    symbol: str,
    count: int,
    end: datetime.datetime | None = None,
) -> pd.DataFrame | None:
    """DB-first read for KR daily candles. Returns ``None`` on miss/stale.

    Reads the latest ``count`` rows from ``kr_candles_1d`` (venue ``KRX``)
    via ``DailyCandlesRepository.fetch_recent``. If the DB has at least
    ``count`` rows AND the newest row covers the most-recent closed ``XKRX``
    session, returns a sorted OHLCV DataFrame. Otherwise returns ``None``
    so the caller can fall back to a live API (KIS).

    ``end`` is currently a forward-compatibility placeholder: when non-None
    (historical query), the cache is bypassed and ``None`` is returned, so
    the caller's live path can apply the historical end-date filter.
    """
    if end is not None:
        # Historical queries cannot be served by the latest-N cache.
        return None

    from app.core.db import AsyncSessionLocal
    from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey

    partition = "KRX"
    async with AsyncSessionLocal() as session:
        repo = DailyCandlesRepository(session=session)
        cached = await repo.fetch_recent(
            market=MarketKey.KR, symbol=symbol, partition=partition, count=count
        )
        if len(cached) >= count and cache_is_fresh_equity(cached, "XKRX"):
            logger.debug(
                "daily_candles cache hit market=kr symbol=%s rows=%d",
                symbol,
                len(cached),
            )
            return rows_to_frame(cached)
    return None


async def cache_first_us(
    symbol: str,
    count: int,
    end: datetime.datetime | None = None,
) -> pd.DataFrame | None:
    """DB-first read for US daily candles. Returns ``None`` on miss/stale.

    Resolves the symbol's exchange (``NASDAQ`` / ``NYSE`` / ``AMEX``) via
    ``get_us_exchange_by_symbol`` and reads the latest ``count`` rows from
    ``us_candles_1d``. Freshness uses the ``XNYS`` calendar (which covers
    all US sessions including AMEX). Returns ``None`` when the DB is
    insufficient or stale so the caller can fall back to a live API
    (Yahoo → Toss).
    """
    if end is not None:
        return None

    from app.core.db import AsyncSessionLocal
    from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
    from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

    async with AsyncSessionLocal() as session:
        try:
            partition = await get_us_exchange_by_symbol(symbol, db=session)
        except Exception:
            logger.warning(
                "Could not resolve US exchange for symbol=%s; defaulting to NASD",
                symbol,
            )
            partition = "NASD"

        repo = DailyCandlesRepository(session=session)
        cached = await repo.fetch_recent(
            market=MarketKey.US, symbol=symbol, partition=partition, count=count
        )
        if len(cached) >= count and cache_is_fresh_equity(cached, "XNYS"):
            logger.debug(
                "daily_candles cache hit market=us symbol=%s rows=%d",
                symbol,
                len(cached),
            )
            return rows_to_frame(cached)
    return None


async def write_back_kr(
    frame: pd.DataFrame, *, symbol: str, partition: str = "KRX", source: str = "kis"
) -> int:
    """Write a freshly fetched KR daily frame back into ``kr_candles_1d``.

    Best-effort: any error is swallowed and logged so that write-back
    failures never propagate to the caller's read path. Returns the number
    of rows upserted (0 on failure or empty frame).
    """
    if frame is None or frame.empty:
        return 0
    from app.core.db import AsyncSessionLocal
    from app.services.daily_candles.converters import frame_to_rows
    from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey

    repo_rows = frame_to_rows(frame, symbol=symbol, partition=partition, source=source)
    if not repo_rows:
        return 0
    try:
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            upserted = await repo.upsert_rows(market=MarketKey.KR, rows=repo_rows)
            await session.commit()
            return upserted
    except Exception:
        logger.exception(
            "write_back_kr failed symbol=%s partition=%s rows=%d",
            symbol,
            partition,
            len(repo_rows),
        )
        return 0


async def write_back_us(
    frame: pd.DataFrame,
    *,
    symbol: str,
    partition: str | None = None,
    source: str = "yahoo",
) -> int:
    """Write a freshly fetched US daily frame back into ``us_candles_1d``.

    ``source`` defaults to ``'yahoo'`` because the most common write-back
    caller in ``get_ohlcv`` is the US Yahoo path. Pass ``'toss'`` if the
    frame came from the Toss fallback.

    If ``partition`` is ``None`` the exchange is resolved via
    ``get_us_exchange_by_symbol`` (defaulting to ``'NASD'`` on failure).
    """
    if frame is None or frame.empty:
        return 0
    from app.core.db import AsyncSessionLocal
    from app.services.daily_candles.converters import frame_to_rows
    from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
    from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

    if partition is None:
        async with AsyncSessionLocal() as session:
            try:
                partition = await get_us_exchange_by_symbol(symbol, db=session)
            except Exception:
                logger.warning(
                    "write_back_us: could not resolve exchange for symbol=%s; "
                    "defaulting to NASD",
                    symbol,
                )
                partition = "NASD"

    repo_rows = frame_to_rows(frame, symbol=symbol, partition=partition, source=source)
    if not repo_rows:
        return 0
    try:
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            upserted = await repo.upsert_rows(market=MarketKey.US, rows=repo_rows)
            await session.commit()
            return upserted
    except Exception:
        logger.exception(
            "write_back_us failed symbol=%s partition=%s rows=%d",
            symbol,
            partition,
            len(repo_rows),
        )
        return 0


__all__ = [
    "OHLCV_COLUMNS",
    "cache_first_kr",
    "cache_first_us",
    "cache_is_fresh_crypto",
    "cache_is_fresh_equity",
    "get_calendar",
    "latest_exchange_session",
    "rows_to_frame",
    "write_back_kr",
    "write_back_us",
]
