from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_coverage import CoverageState


async def _candles_freshness(
    db: AsyncSession,
    *,
    table_name: str,
    trading_day: dt.date,
    expected_symbols: int | None = None,
) -> tuple[CoverageState, dt.datetime | None, dt.date | None, int, int]:
    try:
        row = (
            await db.execute(
                sa.text(
                    f"""
                    SELECT MAX(time) AS latest_time,
                           MAX(time::date) AS latest_date,
                           COUNT(DISTINCT symbol) FILTER (
                             WHERE time::date >= :trading_day
                           ) AS fresh_symbols,
                           COUNT(DISTINCT symbol) FILTER (
                             WHERE time::date < :trading_day
                           ) AS stale_symbols
                    FROM public.{table_name}
                    """
                ),
                {"trading_day": trading_day},
            )
        ).one()
    except Exception:  # noqa: BLE001 - coverage must fail closed/read-only
        await db.rollback()
        return "missing", None, None, 0, 0

    fresh = int(row.fresh_symbols or 0)
    stale = int(row.stale_symbols or 0)
    if fresh + stale == 0:
        return ("missing", None, None, 0, 0)
    if expected_symbols and fresh < expected_symbols:
        return (
            "partial" if fresh > 0 else "stale",
            row.latest_time,
            row.latest_date,
            fresh,
            stale,
        )
    if stale > 0 and fresh == 0:
        return ("stale", row.latest_time, row.latest_date, fresh, stale)
    if stale > 0 and fresh > 0:
        return ("partial", row.latest_time, row.latest_date, fresh, stale)
    return ("fresh", row.latest_time, row.latest_date, fresh, stale)


async def kr_candles_freshness(
    db: AsyncSession,
    *,
    trading_day: dt.date,
    expected_symbols: int | None = None,
) -> tuple[CoverageState, dt.datetime | None, dt.date | None, int, int]:
    return await _candles_freshness(
        db,
        table_name="kr_candles_1m",
        trading_day=trading_day,
        expected_symbols=expected_symbols,
    )


async def us_candles_freshness(
    db: AsyncSession,
    *,
    trading_day: dt.date,
    expected_symbols: int | None = None,
) -> tuple[CoverageState, dt.datetime | None, dt.date | None, int, int]:
    return await _candles_freshness(
        db,
        table_name="us_candles_1m",
        trading_day=trading_day,
        expected_symbols=expected_symbols,
    )
