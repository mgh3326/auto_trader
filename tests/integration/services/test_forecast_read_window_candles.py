"""ROB-659: real-path coverage for forecast_service._read_window_candles.

The forecast resolve tests in tests/test_forecast_service.py all monkeypatch
_read_window_candles with canned bars, because the daily-candle store
(kr_candles_1d) is a Timescale hypertable with no ORM model and is absent from
the create_all test DB. This test exercises the actual reader end-to-end against
the dev Timescale container (port 5434) — the market/partition mapping, the
DailyCandlesRepository.fetch_range call, and the inclusive-window [start, review]
filter — so that code path is no longer test-blind.

Mirrors the dev-DB harness in
tests/integration/services/daily_candles/test_full_cycle.py.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.trade_journal import forecast_service as svc

_TEST_SUFFIX = uuid.uuid4().hex[:8].upper()
_SYMBOL_KR = f"FCKR{_TEST_SUFFIX}"


def _find_dotenv_path() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            candidate = parent / ".env"
            return candidate if candidate.is_file() else None
    return None


def _read_dev_database_url() -> str | None:
    env_path = _find_dotenv_path()
    if env_path is None:
        return None
    with env_path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "DATABASE_URL":
                return value.strip()
    return None


_DEV_DB_URL = _read_dev_database_url() or os.environ.get("DEV_DATABASE_URL")


@pytest_asyncio.fixture
async def dev_session():
    if not _DEV_DB_URL:
        pytest.skip(
            "DEV_DATABASE_URL or local .env DATABASE_URL is required for live "
            "Timescale integration tests"
        )
    engine = create_async_engine(_DEV_DB_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


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
async def test_read_window_candles_inclusive_window_kr(dev_session):
    repo = DailyCandlesRepository(session=dev_session)
    # Days 1..7; the resolve window is [2026-06-02, 2026-06-05] inclusive.
    rows = [_kr_candle(d, 100.0 + d) for d in range(1, 8)]
    try:
        await repo.upsert_rows(market=MarketKey.KR, rows=rows)
        await dev_session.commit()

        got = await svc._read_window_candles(
            dev_session,
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
        await dev_session.rollback()
        raise
    finally:
        await dev_session.rollback()
        await dev_session.execute(
            text("DELETE FROM public.kr_candles_1d WHERE symbol = :symbol"),
            {"symbol": _SYMBOL_KR},
        )
        await dev_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_window_candles_unknown_instrument_returns_none(dev_session):
    got = await svc._read_window_candles(
        dev_session,
        symbol=_SYMBOL_KR,
        instrument_type="bond",  # not in the auto-resolvable set
        start_date=date(2026, 6, 2),
        review_date=date(2026, 6, 5),
    )
    assert got is None
