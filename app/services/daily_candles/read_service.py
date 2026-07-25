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
* Fail-open: ``cache_first_*`` return ``None`` (never raise) on ANY DB/
  calendar failure — DB trouble must degrade to the live path, not hard-fail
  ``get_ohlcv``. ``write_back_*`` are likewise best-effort and return 0.

Freshness rule for equity caches (KR/US): the newest DB row must cover the
most-recent closed exchange session (``XKRX`` for KR, ``XNYS`` for US).

KR intraday rule (ROB-639 review): while the KRX daily bar for *today* may
still be forming (session day, before the 15:35 KST cutoff shared with
``kis_ohlcv_cache.KRX_DAILY_CACHE_CUTOFF``), ``cache_first_kr`` returns
``None`` so the live KIS path serves — the old live path included today's
forming bar and DB-first must not silently drop it. After the cutoff and on
non-session days the DB-first path proceeds. The US path needs no such gate:
the Yahoo daily path is already closed-bucket only.
"""

from __future__ import annotations

import datetime
import logging
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

from app.core.timezone import KST, now_kst
from app.services.daily_candles.repository import DailyCandleRow
from app.services.kis_ohlcv_cache import KRX_DAILY_CACHE_CUTOFF

logger = logging.getLogger(__name__)

# Canonical OHLCV column ordering produced by ``rows_to_frame``. Kept here so
# consumers (``market_data_indicators`` and ``get_ohlcv``) share one source of
# truth for the frame shape.
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]


@lru_cache(maxsize=4)
def get_calendar(exchange: str):
    """Return (and cache) an ``exchange_calendars`` calendar by name."""
    return xcals.get_calendar(exchange)


def _coerce_kst(now: datetime.datetime | None) -> datetime.datetime:
    """Coerce an optional (possibly naive) datetime to a KST-aware datetime."""
    current = now or now_kst()
    if current.tzinfo is None:
        return current.replace(tzinfo=KST)
    return current.astimezone(KST)


def _coerce_utc_timestamp(now: datetime.datetime | None) -> pd.Timestamp:
    """Coerce an optional (possibly naive) datetime to a UTC pd.Timestamp."""
    ts = (
        pd.Timestamp(now)
        if now is not None
        else pd.Timestamp(datetime.datetime.now(datetime.UTC))
    )
    if ts.tzinfo is None:
        return ts.tz_localize(datetime.UTC)
    return ts.tz_convert(datetime.UTC)


def latest_exchange_session(
    exchange: str, now: datetime.datetime | None = None
) -> datetime.date | None:
    """Return the most-recent *closed* session date for the given exchange.

    ``exchange`` accepts any calendar name supported by exchange_calendars
    (e.g., ``'XKRX'`` for KRX, ``'XNYS'`` for NYSE). ``now`` is injectable for
    tests; defaults to the real clock. Returns ``None`` if the library raises
    (rare, but defensive for early/late session edges).

    ``minute_to_past_session`` excludes an in-progress session, so during
    trading hours this returns the *previous* session.
    """
    cal = get_calendar(exchange)
    try:
        session = cal.minute_to_past_session(_coerce_utc_timestamp(now), count=1)
    except Exception:
        return None
    return pd.Timestamp(session).date()


def kr_daily_bar_may_be_forming(now: datetime.datetime | None = None) -> bool:
    """True while today's KRX daily bar may still be forming.

    That is: today (KST) is an XKRX session day AND the current KST time is
    before the shared ``KRX_DAILY_CACHE_CUTOFF`` (15:35 — session close plus
    settling buffer, same semantics as ``kis_ohlcv_cache``).
    """
    current = _coerce_kst(now)
    try:
        cal = get_calendar("XKRX")
        if not bool(cal.is_session(pd.Timestamp(current.date()))):
            return False
    except Exception:
        # Calendar failure: don't block the DB path here — the freshness
        # check downstream is the authority and also fails closed to live.
        return False
    return current.time() < KRX_DAILY_CACHE_CUTOFF


def last_final_session_kr(now: datetime.datetime | None = None) -> datetime.date | None:
    """Most recent XKRX session whose daily bar is final (15:35 KST cutoff).

    Today counts only after ``KRX_DAILY_CACHE_CUTOFF``; otherwise the latest
    session strictly before today. Returns ``None`` on calendar failure.
    """
    current = _coerce_kst(now)
    today = current.date()
    try:
        cal = get_calendar("XKRX")
        ts_today = pd.Timestamp(today)
        if bool(cal.is_session(ts_today)) and current.time() >= KRX_DAILY_CACHE_CUTOFF:
            return today
        prev = cal.date_to_session(
            ts_today - pd.Timedelta(days=1), direction="previous"
        )
        return pd.Timestamp(prev).date()
    except Exception:
        return None


def last_final_session_us(now: datetime.datetime | None = None) -> datetime.date | None:
    """Most recent *closed* XNYS session (the US daily bar is final at close)."""
    return latest_exchange_session("XNYS", now=now)


def drop_forming_daily_rows(
    frame: pd.DataFrame, *, market: str, now: datetime.datetime | None = None
) -> pd.DataFrame:
    """Drop rows whose session is not yet closed (forming intraday bars).

    Used by the write-back paths so a partial intraday bar fetched live is
    never persisted into ``kr/us_candles_1d`` as an authoritative daily row.
    ``market`` is ``'kr'`` or ``'us'``. If the last final session cannot be
    determined, the frame is returned unchanged (best-effort write-back).
    """
    if frame is None or frame.empty:
        return frame
    last_final = (
        last_final_session_kr(now) if market == "kr" else last_final_session_us(now)
    )
    if last_final is None:
        return frame
    if "date" in frame.columns:
        dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    elif "datetime" in frame.columns:
        dates = pd.to_datetime(frame["datetime"], errors="coerce").dt.date
    else:
        return frame
    mask = pd.Series(
        [d is not pd.NaT and d is not None and d <= last_final for d in dates],
        index=frame.index,
    )
    if mask.all():
        return frame
    dropped = int((~mask).sum())
    logger.debug(
        "drop_forming_daily_rows market=%s dropped=%d last_final=%s",
        market,
        dropped,
        last_final,
    )
    return frame.loc[mask]


def cache_is_fresh_equity(
    rows: list[DailyCandleRow],
    exchange: str,
    now: datetime.datetime | None = None,
) -> bool:
    """Cache is fresh if the newest row covers the latest closed exchange session.

    For KR this encodes the 15:30 KST (06:30 UTC) session close of ``XKRX`` —
    a row timestamped after the latest session's close satisfies the rule.
    ``now`` is injectable for tests; defaults to the real clock.
    """
    if not rows:
        return False
    latest_session = latest_exchange_session(exchange, now=now)
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
    *,
    now: datetime.datetime | None = None,
) -> pd.DataFrame | None:
    """DB-first read for KR daily candles. Returns ``None`` on miss/stale/error.

    Reads the latest ``count`` rows from ``kr_candles_1d`` (venue ``KRX``)
    via ``DailyCandlesRepository.fetch_recent``. If the DB has at least
    ``count`` rows AND the newest row covers the most-recent closed ``XKRX``
    session, returns a sorted OHLCV DataFrame. Otherwise returns ``None``
    so the caller can fall back to a live API (KIS).

    While today's KRX daily bar may still be forming (session day, before
    the 15:35 KST cutoff) this always returns ``None`` — the live KIS path
    includes today's forming bar, which the DB does not carry.

    Fail-open: any DB/calendar exception is logged and swallowed, returning
    ``None`` so the caller's live path serves.

    ``end`` is currently a forward-compatibility placeholder: when non-None
    (historical query), the cache is bypassed and ``None`` is returned, so
    the caller's live path can apply the historical end-date filter.
    ``now`` is injectable for tests; defaults to the real clock.
    """
    if end is not None:
        # Historical queries cannot be served by the latest-N cache.
        return None

    try:
        if kr_daily_bar_may_be_forming(now):
            # ROB-639: intraday KRX — serve live so today's forming bar is
            # included (parity with the pre-DB-first live path).
            return None

        from app.core.db import AsyncSessionLocal
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )

        partition = "KRX"
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            cached = await repo.fetch_recent(
                market=MarketKey.KR, symbol=symbol, partition=partition, count=count
            )
            if len(cached) >= count and cache_is_fresh_equity(cached, "XKRX", now=now):
                logger.debug(
                    "daily_candles cache hit market=kr symbol=%s rows=%d",
                    symbol,
                    len(cached),
                )
                return rows_to_frame(cached)
        return None
    except Exception:
        logger.warning(
            "cache_first_kr failed symbol=%s; falling back to live path",
            symbol,
            exc_info=True,
        )
        return None


async def cache_first_us(
    symbol: str,
    count: int,
    end: datetime.datetime | None = None,
    *,
    now: datetime.datetime | None = None,
) -> pd.DataFrame | None:
    """DB-first read for US daily candles. Returns ``None`` on miss/stale/error.

    Resolves the symbol's exchange (``NASDAQ`` / ``NYSE`` / ``AMEX``) via
    ``get_us_exchange_by_symbol`` and reads the latest ``count`` rows from
    ``us_candles_1d``. Freshness uses the ``XNYS`` calendar (which covers
    all US sessions including AMEX). Returns ``None`` when the DB is
    insufficient or stale so the caller can fall back to a live API
    (Yahoo → Toss).

    Fail-open: any DB/calendar exception is logged and swallowed, returning
    ``None`` so the caller's live path serves. ``now`` is injectable for
    tests; defaults to the real clock.
    """
    if end is not None:
        return None

    try:
        from app.core.db import AsyncSessionLocal
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        async with AsyncSessionLocal() as session:
            try:
                partition = await get_us_exchange_by_symbol(symbol, db=session)
            except Exception:
                logger.warning(
                    "Could not resolve US exchange for symbol=%s; defaulting to NASD",
                    symbol,
                )
                # The failed lookup may have aborted the transaction; roll it
                # back before reusing this session, otherwise the next query
                # raises InFailedSQLTransactionError (poisoned session).
                await session.rollback()
                partition = "NASD"

            repo = DailyCandlesRepository(session=session)
            cached = await repo.fetch_recent(
                market=MarketKey.US, symbol=symbol, partition=partition, count=count
            )
            if len(cached) >= count and cache_is_fresh_equity(cached, "XNYS", now=now):
                logger.debug(
                    "daily_candles cache hit market=us symbol=%s rows=%d",
                    symbol,
                    len(cached),
                )
                return rows_to_frame(cached)
        return None
    except Exception:
        logger.warning(
            "cache_first_us failed symbol=%s; falling back to live path",
            symbol,
            exc_info=True,
        )
        return None


async def write_back_kr(
    frame: pd.DataFrame,
    *,
    symbol: str,
    partition: str = "KRX",
    source: str = "kis",
    now: datetime.datetime | None = None,
) -> int:
    """Write a freshly fetched KR daily frame back into ``kr_candles_1d``.

    Rows whose session is not yet final (today's forming intraday bar) are
    dropped before the upsert so a partial bar is never persisted as an
    authoritative daily row.

    Best-effort: any error is swallowed and logged so that write-back
    failures never propagate to the caller's read path. Returns the number
    of rows upserted (0 on failure or empty frame).
    """
    if frame is None or frame.empty:
        return 0
    try:
        from app.core.db import AsyncSessionLocal
        from app.services.daily_candles.converters import frame_to_rows
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )

        frame = drop_forming_daily_rows(frame, market="kr", now=now)
        repo_rows = frame_to_rows(
            frame, symbol=symbol, partition=partition, source=source
        )
        if not repo_rows:
            return 0
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            upserted = await repo.upsert_rows(market=MarketKey.KR, rows=repo_rows)
            await session.commit()
            return upserted
    except Exception:
        logger.exception(
            "write_back_kr failed symbol=%s partition=%s", symbol, partition
        )
        return 0


async def write_back_us(
    frame: pd.DataFrame,
    *,
    symbol: str,
    partition: str | None = None,
    source: str = "yahoo",
    now: datetime.datetime | None = None,
) -> int:
    """Write a freshly fetched US daily frame back into ``us_candles_1d``.

    ``source`` defaults to ``'yahoo'`` because the most common write-back
    caller in ``get_ohlcv`` is the US Yahoo path. Pass ``'toss'`` if the
    frame came from the Toss fallback.

    If ``partition`` is ``None`` the exchange is resolved via
    ``get_us_exchange_by_symbol`` (defaulting to ``'NASD'`` on failure).

    Rows whose session is not yet closed (a forming intraday bar) are
    dropped before the upsert. When the frame lacks an ``adj_close`` column
    the upsert leaves existing ``adj_close`` values untouched (so a plain
    Yahoo/Toss frame does not null out ``yahoo_fallback`` adjusted closes).

    Best-effort: any error is swallowed and logged; returns 0 on failure.
    """
    if frame is None or frame.empty:
        return 0
    try:
        from app.core.db import AsyncSessionLocal
        from app.services.daily_candles.converters import frame_to_rows
        from app.services.daily_candles.repository import (
            DailyCandlesRepository,
            MarketKey,
        )

        frame = drop_forming_daily_rows(frame, market="us", now=now)
        if frame.empty:
            return 0

        if partition is None:
            from app.services.us_symbol_universe_service import (
                get_us_exchange_by_symbol,
            )

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

        repo_rows = frame_to_rows(
            frame, symbol=symbol, partition=partition, source=source
        )
        if not repo_rows:
            return 0
        update_adj_close = "adj_close" in frame.columns
        async with AsyncSessionLocal() as session:
            repo = DailyCandlesRepository(session=session)
            upserted = await repo.upsert_rows(
                market=MarketKey.US,
                rows=repo_rows,
                update_adj_close=update_adj_close,
            )
            await session.commit()
            return upserted
    except Exception:
        logger.exception(
            "write_back_us failed symbol=%s partition=%s", symbol, partition
        )
        return 0


__all__ = [
    "OHLCV_COLUMNS",
    "cache_first_kr",
    "cache_first_us",
    "cache_is_fresh_crypto",
    "cache_is_fresh_equity",
    "drop_forming_daily_rows",
    "get_calendar",
    "kr_daily_bar_may_be_forming",
    "last_final_session_kr",
    "last_final_session_us",
    "latest_exchange_session",
    "rows_to_frame",
    "write_back_kr",
    "write_back_us",
]
