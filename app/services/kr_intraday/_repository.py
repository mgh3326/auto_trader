from __future__ import annotations

import asyncio
import datetime
import logging
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.kr_intraday._types import (
    VenueType,
    _IntradayPeriodConfig,
    _KST,
    _MinuteRow,
    _UniverseError,
    _UniverseRow,
    _kr_universe_sync_hint,
)
from app.services.kr_intraday._utils import (
    _convert_kis_datetime_to_utc,
    _resolve_window_minute_time,
    _store_minute_row,
    _to_float,
    _to_kst_naive,
    _to_venue,
)

logger = logging.getLogger(__name__)


def _async_session() -> AsyncSession:
    return cast(AsyncSession, cast(object, AsyncSessionLocal()))


_KR_UNIVERSE_HAS_ANY_ROWS_SQL = text(
    """
    SELECT symbol
    FROM public.kr_symbol_universe
    LIMIT 1
    """
)

_KR_UNIVERSE_ROW_SQL = text(
    """
    SELECT symbol, nxt_eligible, is_active
    FROM public.kr_symbol_universe
    WHERE symbol = :symbol
    """
)

_KR_HOURLY_SQL = text(
    """
    SELECT bucket, open, high, low, close, volume, value, venues
    FROM public.kr_candles_1h
    WHERE symbol = :symbol
      AND bucket <= :end_time
    ORDER BY bucket DESC
    LIMIT :limit
    """
)

_KR_MINUTE_SQL = text(
    """
    SELECT time, venue, open, high, low, close, volume, value
    FROM public.kr_candles_1m
    WHERE symbol = :symbol
      AND time >= :start_time
      AND time < :end_time
    """
)

_KR_MINUTE_HISTORY_SQL = text(
    """
    SELECT time, venue, open, high, low, close, volume, value
    FROM public.kr_candles_1m
    WHERE symbol = :symbol
      AND time <= :end_time
    ORDER BY time DESC
    LIMIT :limit
    """
)

_UPSERT_SQL = text(
    """
    INSERT INTO public.kr_candles_1m (symbol, time, venue, open, high, low, close, volume, value)
    VALUES (:symbol, :time, :venue, :open, :high, :low, :close, :volume, :value)
    ON CONFLICT (time, symbol, venue)
    DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        value = EXCLUDED.value
    """
)


async def _resolve_universe_row(
    symbol: str,
) -> _UniverseRow | _UniverseError:
    """
    Resolve symbol from kr_symbol_universe table.

    Returns _UniverseRow if found and active, _UniverseError otherwise.
    Never raises ValueError - uses graceful degradation instead.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    async with _async_session() as session:
        has_any_rows = (
            await session.execute(_KR_UNIVERSE_HAS_ANY_ROWS_SQL)
        ).scalar_one_or_none()
        result = await session.execute(
            _KR_UNIVERSE_ROW_SQL,
            {"symbol": normalized_symbol},
        )
        rows = list(result.mappings().all())

    if not rows:
        if has_any_rows is None:
            logger.warning(
                "kr_symbol_universe is empty. %s",
                _kr_universe_sync_hint(),
            )
            return _UniverseError(
                reason=f"kr_symbol_universe is empty. {_kr_universe_sync_hint()}"
            )
        logger.warning(
            "KR symbol '%s' is not registered in kr_symbol_universe. %s",
            normalized_symbol,
            _kr_universe_sync_hint(),
        )
        return _UniverseError(
            reason=f"KR symbol '{normalized_symbol}' is not registered in kr_symbol_universe. "
            f"{_kr_universe_sync_hint()}"
        )

    row = rows[0]
    is_active = bool(row.get("is_active"))
    if not is_active:
        logger.warning(
            "KR symbol '%s' is inactive in kr_symbol_universe. %s",
            normalized_symbol,
            _kr_universe_sync_hint(),
        )
        return _UniverseError(
            reason=f"KR symbol '{normalized_symbol}' is inactive in kr_symbol_universe. "
            f"{_kr_universe_sync_hint()}"
        )

    return _UniverseRow(
        symbol=normalized_symbol,
        nxt_eligible=bool(row.get("nxt_eligible")),
        is_active=is_active,
    )


async def _fetch_hour_rows(
    *,
    symbol: str,
    end_time_kst: datetime.datetime,
    limit: int,
) -> list[dict[str, object]]:
    async with _async_session() as session:
        result = await session.execute(
            _KR_HOURLY_SQL,
            {
                "symbol": symbol,
                "end_time": end_time_kst,
                "limit": int(limit),
            },
        )
        return [{str(k): v for k, v in row.items()} for row in result.mappings().all()]


async def _fetch_minute_rows(
    *,
    symbol: str,
    start_time_kst: datetime.datetime,
    end_time_kst: datetime.datetime,
) -> list[dict[str, object]]:
    async with _async_session() as session:
        result = await session.execute(
            _KR_MINUTE_SQL,
            {
                "symbol": symbol,
                "start_time": start_time_kst,
                "end_time": end_time_kst,
            },
        )
        return [{str(k): v for k, v in row.items()} for row in result.mappings().all()]


def _load_db_minute_rows_into_map(
    *,
    rows: list[dict[str, object]],
    start_naive: datetime.datetime,
    end_naive: datetime.datetime,
) -> dict[tuple[datetime.datetime, VenueType], _MinuteRow]:
    minute_by_key: dict[tuple[datetime.datetime, VenueType], _MinuteRow] = {}
    for row in rows:
        venue = _to_venue(row.get("venue"))
        minute_time = _resolve_window_minute_time(row.get("time"))
        if venue is None or minute_time is None:
            continue
        if not (start_naive <= minute_time < end_naive):
            continue
        _store_minute_row(
            minute_by_key,
            minute_time=minute_time,
            venue=venue,
            source=row,
        )
    return minute_by_key


async def _fetch_minute_history_rows(
    *,
    symbol: str,
    end_time_kst: datetime.datetime,
    limit: int,
) -> list[dict[str, object]]:
    async with _async_session() as session:
        result = await session.execute(
            _KR_MINUTE_HISTORY_SQL,
            {
                "symbol": symbol,
                "end_time": end_time_kst,
                "limit": int(limit),
            },
        )
        return [{str(k): v for k, v in row.items()} for row in result.mappings().all()]


async def _fetch_intraday_history_rows(
    *,
    config: _IntradayPeriodConfig,
    symbol: str,
    end_time_kst: datetime.datetime,
    limit: int,
) -> list[dict[str, object]]:
    if config.period == "1m":
        return await _fetch_minute_history_rows(
            symbol=symbol,
            end_time_kst=end_time_kst,
            limit=max(limit * 4, limit),
        )

    query = text(
        f"""
        SELECT bucket, open, high, low, close, volume, value, venues
        FROM {config.history_table}
        WHERE symbol = :symbol
          AND bucket <= :end_time
        ORDER BY bucket DESC
        LIMIT :limit
        """
    )
    async with _async_session() as session:
        result = await session.execute(
            query,
            {
                "symbol": symbol,
                "end_time": end_time_kst,
                "limit": int(limit),
            },
        )
        return [{str(k): v for k, v in row.items()} for row in result.mappings().all()]


def _log_task_exception(task: asyncio.Task[None]) -> None:
    """Callback to log exceptions from background storage tasks."""
    try:
        task.result()
    except Exception:
        logger.exception("Background minute candle storage task crashed")


async def _store_minute_candles_background(
    *,
    symbol: str,
    minute_rows: list[dict[str, object]],
) -> None:
    """
    Store minute candles to the database in the background (fire-and-forget).

    This function performs an upsert operation on the kr_candles_1m table.
    It is designed to be called as a background task using asyncio.create_task().

    Parameters
    ----------
    symbol : str
        Stock symbol (e.g., "005930" for Samsung Electronics)
    minute_rows : list[dict[str, object]]
        List of minute candle rows to upsert. Each row should contain:
        - time: datetime (KST naive)
        - venue: str ("KRX" or "NTX")
        - open, high, low, close: float
        - volume, value: float

    Notes
    -----
    - Uses ON CONFLICT DO UPDATE to handle duplicates gracefully
    - Errors are logged but not raised (fire-and-forget pattern)
    - Commits changes before returning to ensure data persistence
    """
    if not minute_rows:
        return

    try:
        async with _async_session() as session:
            for row in minute_rows:
                time_val = row.get("time")
                if not isinstance(time_val, datetime.datetime):
                    continue

                # Ensure time is KST naive (as stored in DB)
                time_naive = _to_kst_naive(time_val)

                await session.execute(
                    _UPSERT_SQL,
                    {
                        "symbol": symbol,
                        "time": time_naive,
                        "venue": str(row.get("venue", "KRX")),
                        "open": _to_float(row.get("open")),
                        "high": _to_float(row.get("high")),
                        "low": _to_float(row.get("low")),
                        "close": _to_float(row.get("close")),
                        "volume": _to_float(row.get("volume")),
                        "value": _to_float(row.get("value")),
                    },
                )

            await session.commit()
            logger.debug(
                "Stored %d minute candles for symbol '%s' in background",
                len(minute_rows),
                symbol,
            )

    except Exception as e:
        logger.error(
            "Failed to store minute candles for symbol '%s' in background: %s",
            symbol,
            e,
            exc_info=True,
        )


def _schedule_background_minute_storage(
    *,
    symbol: str,
    minute_rows: list[_MinuteRow],
) -> None:
    if not minute_rows:
        return

    task = asyncio.create_task(
        _store_minute_candles_background(
            symbol=symbol,
            minute_rows=[
                {
                    "time": _convert_kis_datetime_to_utc(row.minute_time),
                    "venue": row.venue,
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                    "volume": row.volume,
                    "value": row.value,
                }
                for row in minute_rows
            ],
        )
    )
    task.add_done_callback(_log_task_exception)
    logger.info(
        "Background task created to store %d minute candles for symbol '%s'",
        len(minute_rows),
        symbol,
    )
