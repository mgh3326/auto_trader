"""ROB-659: real-path coverage for forecast_service._read_window_candles.

The forecast resolve tests in tests/test_forecast_service.py all monkeypatch
_read_window_candles with canned bars, because the daily-candle store
(kr_candles_1d) has no ORM model. The pytest schema bootstrap mirrors its
raw-SQL table contract in the run-owned PostgreSQL database, allowing this test
to exercise the real reader end-to-end in local and CI runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.trade_journal import forecast_service as svc

_TEST_SUFFIX = uuid.uuid4().hex[:8].upper()
_SYMBOL_KR = f"FCKR{_TEST_SUFFIX}"


def _kr_candle(day: int, high: float) -> DailyCandleRow:
    return DailyCandleRow(
        time_utc=datetime(2026, 6, day, tzinfo=UTC),
        symbol=_SYMBOL_KR,
        partition="KRX",
        open=high - 5,
        high=high,
        low=high - 10,
        close=high - 2,
        adj_close=None,
        volume=1000.0,
        value=high * 1000.0,
        source="kis",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_window_candles_inclusive_window_kr(db_session):
    repo = DailyCandlesRepository(session=db_session)
    # Days 1..7; the resolve window is [2026-06-02, 2026-06-05] inclusive.
    rows = [_kr_candle(d, 100.0 + d) for d in range(1, 8)]
    try:
        await repo.upsert_rows(market=MarketKey.KR, rows=rows)
        await db_session.commit()

        got = await svc._read_window_candles(
            db_session,
            symbol=_SYMBOL_KR,
            instrument_type="equity_kr",
            start_date=date(2026, 6, 2),
            review_date=date(2026, 6, 5),
        )
        assert got is not None
        got_days = sorted(r.time_utc.date().day for r in got)
        # Days 1, 6, 7 fall outside [2, 5] and must be filtered out despite the
        # ±2-day UTC fetch padding.
        assert got_days == [2, 3, 4, 5]
    except Exception:
        await db_session.rollback()
        raise
    finally:
        await db_session.rollback()
        await db_session.execute(
            text("DELETE FROM public.kr_candles_1d WHERE symbol = :symbol"),
            {"symbol": _SYMBOL_KR},
        )
        await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_window_candles_unknown_instrument_returns_none(db_session):
    got = await svc._read_window_candles(
        db_session,
        symbol=_SYMBOL_KR,
        instrument_type="bond",  # not in the auto-resolvable set
        start_date=date(2026, 6, 2),
        review_date=date(2026, 6, 5),
    )
    assert got is None
