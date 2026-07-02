"""Unit tests for ROB-639: get_ohlcv(period='day') DB-first read-through.

Covers:
- DB hit: get_ohlcv returns DB rows without calling KIS/Yahoo
- DB miss/stale: get_ohlcv falls back to live API and writes back to DB
- end_date bypass: historical queries skip the cache
- cache_is_fresh_equity: 15:35 KRX cutoff boundary cases

The get_ohlcv-level tests mock cache_first_kr/us at the module level (same
pattern as read_kr_intraday_candles in test_market_data_service.py). The
read_service-level tests mock DailyCandlesRepository.fetch_recent and
cache_is_fresh_equity to exercise the freshness gate directly.
"""

from __future__ import annotations

import datetime as dt
from datetime import UTC, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.services.daily_candles.read_service import (
    cache_first_kr,
    cache_first_us,
    cache_is_fresh_equity,
)
from app.services.daily_candles.repository import DailyCandleRow
from app.services.market_data import service as market_data_service


def _make_row(
    symbol: str,
    partition: str,
    t: dt.datetime,
    close: float,
    source: str = "kis",
) -> DailyCandleRow:
    return DailyCandleRow(
        time_utc=t,
        symbol=symbol,
        partition=partition,
        open=close - 1.0,
        high=close + 0.5,
        low=close - 1.5,
        close=close,
        adj_close=None,
        volume=1000.0,
        value=close * 1000.0,
        source=source,
    )


def _kr_db_frame(n: int = 5) -> pd.DataFrame:
    today = dt.datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    return pd.DataFrame(
        [
            {
                "date": (today - timedelta(days=i)).date(),
                "open": 70000.0 + i,
                "high": 70500.0 + i,
                "low": 69500.0 + i,
                "close": 70200.0 + i,
                "volume": 100000.0,
                "value": 70200.0 * 100000.0,
            }
            for i in range(n)
        ]
    )


def _us_db_frame(n: int = 5) -> pd.DataFrame:
    today = dt.datetime.now(UTC).replace(hour=22, minute=0, second=0, microsecond=0)
    return pd.DataFrame(
        [
            {
                "date": (today - timedelta(days=i)).date(),
                "open": 150.0 + i,
                "high": 152.0 + i,
                "low": 148.0 + i,
                "close": 151.0 + i,
                "volume": 5_000_000.0,
                "value": 151.0 * 5_000_000.0,
            }
            for i in range(n)
        ]
    )


# ---------------------------------------------------------------------------
# get_ohlcv integration tests (mock cache_first_* at module level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kr_day_db_hit_returns_db_rows_without_kis(monkeypatch):
    """When DB has fresh rows, get_ohlcv returns them with source='db'."""
    db_frame = _kr_db_frame(n=5)
    monkeypatch.setattr(
        market_data_service, "cache_first_kr", AsyncMock(return_value=db_frame)
    )
    kis_instance = MagicMock()
    kis_instance.inquire_daily_itemchartprice = AsyncMock()
    monkeypatch.setattr(market_data_service, "KISClient", lambda: kis_instance)
    write_back_mock = AsyncMock(return_value=5)
    monkeypatch.setattr(market_data_service, "write_back_kr", write_back_mock)

    candles = await market_data_service.get_ohlcv(
        "005930", "kr", "day", count=5
    )

    assert len(candles) == 5
    assert all(c.source == "db" for c in candles)
    assert all(c.period == "day" for c in candles)
    kis_instance.inquire_daily_itemchartprice.assert_not_called()
    write_back_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_us_day_db_hit_returns_db_rows_without_yahoo(monkeypatch):
    """When DB has fresh US rows, get_ohlcv returns them with source='db'."""
    db_frame = _us_db_frame(n=5)
    monkeypatch.setattr(
        market_data_service, "cache_first_us", AsyncMock(return_value=db_frame)
    )
    yahoo_mock = AsyncMock()
    monkeypatch.setattr(market_data_service, "fetch_yahoo_ohlcv", yahoo_mock)
    write_back_mock = AsyncMock(return_value=5)
    monkeypatch.setattr(market_data_service, "write_back_us", write_back_mock)

    candles = await market_data_service.get_ohlcv(
        "MSFT", "us", "day", count=5
    )

    assert len(candles) == 5
    assert all(c.source == "db" for c in candles)
    yahoo_mock.assert_not_awaited()
    write_back_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_kr_day_db_miss_falls_back_to_kis_and_writes_back(monkeypatch):
    """When DB misses, get_ohlcv calls KIS, writes back, returns kis rows."""
    monkeypatch.setattr(
        market_data_service, "cache_first_kr", AsyncMock(return_value=None)
    )

    kis_frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-07-01 15:30:00"),
                "date": dt.date(2026, 7, 1),
                "open": 70000.0,
                "high": 70500.0,
                "low": 69500.0,
                "close": 70200.0,
                "volume": 100000.0,
                "value": 70200.0 * 100000.0,
            }
        ]
    )

    class _StubKIS:
        async def inquire_daily_itemchartprice(self, **kwargs):
            return kis_frame

    monkeypatch.setattr(market_data_service, "KISClient", lambda: _StubKIS())
    write_back_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(market_data_service, "write_back_kr", write_back_mock)

    candles = await market_data_service.get_ohlcv(
        "005930", "kr", "day", count=5
    )

    assert len(candles) == 1
    assert candles[0].source == "kis"
    write_back_mock.assert_awaited_once_with(
        kis_frame, symbol="005930"
    )


@pytest.mark.asyncio
async def test_us_day_db_miss_falls_back_to_yahoo_and_writes_back(monkeypatch):
    """When DB misses for US, get_ohlcv calls Yahoo, writes back, returns yahoo rows."""
    monkeypatch.setattr(
        market_data_service, "cache_first_us", AsyncMock(return_value=None)
    )

    yahoo_frame = pd.DataFrame(
        [
            {
                "date": dt.date(2026, 7, 1),
                "open": 150.0,
                "high": 152.0,
                "low": 148.0,
                "close": 151.0,
                "volume": 5_000_000.0,
                "value": 151.0 * 5_000_000.0,
            }
        ]
    )
    yahoo_mock = AsyncMock(return_value=yahoo_frame)
    monkeypatch.setattr(market_data_service, "fetch_yahoo_ohlcv", yahoo_mock)
    write_back_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(market_data_service, "write_back_us", write_back_mock)

    candles = await market_data_service.get_ohlcv(
        "MSFT", "us", "day", count=5
    )

    assert len(candles) == 1
    assert candles[0].source == "yahoo"
    yahoo_mock.assert_awaited_once()
    write_back_mock.assert_awaited_once_with(
        yahoo_frame, symbol="MSFT", source="yahoo"
    )


@pytest.mark.asyncio
async def test_us_day_db_miss_yahoo_failure_uses_toss_and_writes_back(monkeypatch):
    """When DB misses and Yahoo fails for US day, Toss fallback fires and write-back runs with source='toss'."""
    monkeypatch.setattr(
        market_data_service, "cache_first_us", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        market_data_service,
        "fetch_yahoo_ohlcv",
        AsyncMock(side_effect=RuntimeError("yahoo down")),
    )

    toss_frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-07-01 16:00:00"),
                "open": 150.0,
                "high": 152.0,
                "low": 148.0,
                "close": 151.0,
                "volume": 5_000_000.0,
                "value": 151.0 * 5_000_000.0,
            }
        ]
    )
    toss_mock = AsyncMock(return_value=toss_frame)
    monkeypatch.setattr(market_data_service, "fetch_daily_toss_frame", toss_mock)
    write_back_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(market_data_service, "write_back_us", write_back_mock)

    candles = await market_data_service.get_ohlcv(
        "MSFT", "us", "day", count=5
    )

    assert candles[0].source == "toss"
    toss_mock.assert_awaited_once()
    write_back_mock.assert_awaited_once_with(
        toss_frame, symbol="MSFT", source="toss"
    )


@pytest.mark.asyncio
async def test_kr_week_does_not_use_db_cache(monkeypatch):
    """week period must NOT hit the DB cache (v1 scope: day only)."""
    cache_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(market_data_service, "cache_first_kr", cache_mock)

    kis_frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-07-01"),
                "date": dt.date(2026, 7, 1),
                "open": 70000.0,
                "high": 70500.0,
                "low": 69500.0,
                "close": 70200.0,
                "volume": 100000.0,
                "value": 70200.0 * 100000.0,
            }
        ]
    )

    class _StubKIS:
        async def inquire_daily_itemchartprice(self, **kwargs):
            return kis_frame

    monkeypatch.setattr(market_data_service, "KISClient", lambda: _StubKIS())
    monkeypatch.setattr(
        market_data_service, "write_back_kr", AsyncMock(return_value=0)
    )

    candles = await market_data_service.get_ohlcv(
        "005930", "kr", "week", count=5
    )

    assert candles[0].source == "kis"
    cache_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_us_week_does_not_use_db_cache(monkeypatch):
    """week period must NOT hit the DB cache (v1 scope: day only)."""
    cache_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(market_data_service, "cache_first_us", cache_mock)

    yahoo_frame = pd.DataFrame(
        [
            {
                "date": dt.date(2026, 7, 1),
                "open": 150.0,
                "high": 152.0,
                "low": 148.0,
                "close": 151.0,
                "volume": 5_000_000.0,
                "value": 151.0 * 5_000_000.0,
            }
        ]
    )
    monkeypatch.setattr(
        market_data_service,
        "fetch_yahoo_ohlcv",
        AsyncMock(return_value=yahoo_frame),
    )
    monkeypatch.setattr(
        market_data_service, "write_back_us", AsyncMock(return_value=0)
    )

    candles = await market_data_service.get_ohlcv(
        "MSFT", "us", "week", count=5
    )

    assert candles[0].source == "yahoo"
    cache_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# read_service unit tests (mock repository + freshness gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_first_kr_returns_frame_when_fresh():
    """When DB has >= count rows AND freshness passes, returns a DataFrame."""
    today = dt.datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    rows = [
        _make_row("005930", "KRX", today - timedelta(days=i), 70000.0 + i)
        for i in range(5)
    ]

    with (
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(return_value=list(reversed(rows))),
        ),
        patch(
            "app.services.daily_candles.read_service.cache_is_fresh_equity",
            return_value=True,
        ),
    ):
        result = await cache_first_kr("005930", count=5)

    assert result is not None
    assert len(result) == 5
    assert "close" in result.columns


@pytest.mark.asyncio
async def test_cache_first_kr_returns_none_when_stale():
    """When DB rows fail the freshness check, returns None (caller falls back)."""
    today = dt.datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    rows = [
        _make_row("005930", "KRX", today - timedelta(days=i), 70000.0 + i)
        for i in range(5)
    ]

    with (
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(return_value=list(reversed(rows))),
        ),
        patch(
            "app.services.daily_candles.read_service.cache_is_fresh_equity",
            return_value=False,
        ),
    ):
        result = await cache_first_kr("005930", count=5)

    assert result is None


@pytest.mark.asyncio
async def test_cache_first_kr_returns_none_when_insufficient_rows():
    """When DB has fewer rows than count, returns None even if rows are fresh."""
    today = dt.datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    rows = [_make_row("005930", "KRX", today, 70000.0)]  # only 1 row

    with (
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(return_value=rows),
        ),
        patch(
            "app.services.daily_candles.read_service.cache_is_fresh_equity",
            return_value=True,
        ),
    ):
        result = await cache_first_kr("005930", count=10)

    assert result is None


@pytest.mark.asyncio
async def test_cache_first_kr_returns_none_when_db_empty():
    """Empty DB → None."""
    with patch(
        "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
        new=AsyncMock(return_value=[]),
    ):
        result = await cache_first_kr("005930", count=5)
    assert result is None


@pytest.mark.asyncio
async def test_cache_first_kr_bypasses_cache_when_end_is_provided():
    """Historical queries (end != None) must bypass the DB cache."""
    result = await cache_first_kr(
        "005930", count=5, end=dt.datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert result is None


@pytest.mark.asyncio
async def test_cache_first_us_bypasses_cache_when_end_is_provided():
    result = await cache_first_us(
        "MSFT", count=5, end=dt.datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert result is None


@pytest.mark.asyncio
async def test_cache_first_us_returns_none_when_symbol_not_resolved():
    """If get_us_exchange_by_symbol fails, defaults to NASD and proceeds;
    if DB is then empty, returns None."""
    with (
        patch(
            "app.services.us_symbol_universe_service.get_us_exchange_by_symbol",
            new=AsyncMock(side_effect=RuntimeError("lookup failed")),
        ),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await cache_first_us("UNKNOWN", count=5)
    assert result is None


# ---------------------------------------------------------------------------
# Freshness boundary tests (15:35 KRX cutoff semantics)
# ---------------------------------------------------------------------------


def test_cache_is_fresh_equity_true_when_row_matches_latest_session():
    """Row timestamped ON the latest session date → fresh."""
    today = dt.datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    rows = [_make_row("005930", "KRX", today, 70000.0)]

    with patch(
        "app.services.daily_candles.read_service.latest_exchange_session",
        return_value=today.date(),
    ):
        assert cache_is_fresh_equity(rows, "XKRX") is True


def test_cache_is_fresh_equity_false_when_row_is_older_than_latest_session():
    """Row from yesterday but latest session is today → stale."""
    today = dt.datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    rows = [_make_row("005930", "KRX", yesterday, 70000.0)]

    with patch(
        "app.services.daily_candles.read_service.latest_exchange_session",
        return_value=today.date(),
    ):
        assert cache_is_fresh_equity(rows, "XKRX") is False


def test_cache_is_fresh_equity_false_for_empty_rows():
    assert cache_is_fresh_equity([], "XKRX") is False


def test_cache_is_fresh_equity_uses_newest_row_when_multiple():
    """When multiple rows exist, freshness uses the newest (max time)."""
    today = dt.datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    old = today - timedelta(days=10)
    rows = [
        _make_row("005930", "KRX", old, 70000.0),
        _make_row("005930", "KRX", today, 71000.0),  # newest
    ]

    with patch(
        "app.services.daily_candles.read_service.latest_exchange_session",
        return_value=today.date(),
    ):
        assert cache_is_fresh_equity(rows, "XKRX") is True


def test_cache_is_fresh_equity_after_krx_close_same_session():
    """Simulates the 15:35 KST scenario: after the 15:30 close, a row
    timestamped with today's session is fresh (latest_session == today)."""
    # 06:30 UTC = 15:30 KST (XKRX close)
    after_close_utc = dt.datetime.now(UTC).replace(
        hour=7, minute=0, second=0, microsecond=0
    )
    rows = [_make_row("005930", "KRX", after_close_utc, 70000.0)]

    with patch(
        "app.services.daily_candles.read_service.latest_exchange_session",
        return_value=after_close_utc.date(),
    ):
        assert cache_is_fresh_equity(rows, "XKRX") is True


def test_cache_is_fresh_equity_before_krx_close_previous_session():
    """Simulates pre-open: latest_session is the previous trading day.
    A row from that previous session is fresh; today's row is not yet
    expected because the session hasn't closed."""
    today = dt.datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )  # 09:00 KST
    prev_session = today - timedelta(days=1)

    # DB has previous session's row → fresh
    rows_prev = [_make_row("005930", "KRX", prev_session, 70000.0)]
    with patch(
        "app.services.daily_candles.read_service.latest_exchange_session",
        return_value=prev_session.date(),
    ):
        assert cache_is_fresh_equity(rows_prev, "XKRX") is True

    # DB has a row from 2 days ago → stale (one session behind)
    rows_stale = [_make_row("005930", "KRX", prev_session - timedelta(days=1), 70000.0)]
    with patch(
        "app.services.daily_candles.read_service.latest_exchange_session",
        return_value=prev_session.date(),
    ):
        assert cache_is_fresh_equity(rows_stale, "XKRX") is False
