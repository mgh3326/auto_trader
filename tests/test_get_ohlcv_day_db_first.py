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

import exchange_calendars as xcals
import pandas as pd
import pytest

from app.core.timezone import KST
from app.services.daily_candles.read_service import (
    cache_first_kr,
    cache_first_us,
    cache_is_fresh_equity,
    write_back_kr,
    write_back_us,
)
from app.services.daily_candles.repository import DailyCandleRow
from app.services.market_data import service as market_data_service


def _krx_latest_and_prev_session() -> tuple[dt.date, dt.date]:
    """(latest XKRX session on-or-before today, the session before it)."""
    cal = xcals.get_calendar("XKRX")
    today = dt.datetime.now(tz=KST).date()
    latest = cal.date_to_session(pd.Timestamp(today), direction="previous")
    prev = cal.previous_session(latest)
    return pd.Timestamp(latest).date(), pd.Timestamp(prev).date()


def _xnys_latest_and_prev_session() -> tuple[dt.date, dt.date]:
    cal = xcals.get_calendar("XNYS")
    today = dt.datetime.now(tz=UTC).date()
    latest = cal.date_to_session(pd.Timestamp(today), direction="previous")
    prev = cal.previous_session(latest)
    return pd.Timestamp(latest).date(), pd.Timestamp(prev).date()


def _kst_at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=KST)


def _after_cutoff_now() -> dt.datetime:
    """16:00 KST on the latest XKRX session — past the 15:35 cutoff."""
    latest, _ = _krx_latest_and_prev_session()
    return _kst_at(latest, 16, 0)


class _FakeSession:
    """Minimal async-session stand-in for write_back tests (no real DB)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None


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

    candles = await market_data_service.get_ohlcv("005930", "kr", "day", count=5)

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

    candles = await market_data_service.get_ohlcv("MSFT", "us", "day", count=5)

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

    candles = await market_data_service.get_ohlcv("005930", "kr", "day", count=5)

    assert len(candles) == 1
    assert candles[0].source == "kis"
    write_back_mock.assert_awaited_once_with(kis_frame, symbol="005930")


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

    candles = await market_data_service.get_ohlcv("MSFT", "us", "day", count=5)

    assert len(candles) == 1
    assert candles[0].source == "yahoo"
    yahoo_mock.assert_awaited_once()
    write_back_mock.assert_awaited_once_with(yahoo_frame, symbol="MSFT", source="yahoo")


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

    candles = await market_data_service.get_ohlcv("MSFT", "us", "day", count=5)

    assert candles[0].source == "toss"
    toss_mock.assert_awaited_once()
    write_back_mock.assert_awaited_once_with(toss_frame, symbol="MSFT", source="toss")


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
    monkeypatch.setattr(market_data_service, "write_back_kr", AsyncMock(return_value=0))

    candles = await market_data_service.get_ohlcv("005930", "kr", "week", count=5)

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
    monkeypatch.setattr(market_data_service, "write_back_us", AsyncMock(return_value=0))

    candles = await market_data_service.get_ohlcv("MSFT", "us", "week", count=5)

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
        # now= after the 15:35 cutoff so the intraday live-passthrough gate
        # does not fire and the DB path is actually exercised.
        result = await cache_first_kr("005930", count=5, now=_after_cutoff_now())

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
        result = await cache_first_kr("005930", count=5, now=_after_cutoff_now())

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
        result = await cache_first_kr("005930", count=10, now=_after_cutoff_now())

    assert result is None


@pytest.mark.asyncio
async def test_cache_first_kr_returns_none_when_db_empty():
    """Empty DB → None."""
    with patch(
        "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
        new=AsyncMock(return_value=[]),
    ):
        result = await cache_first_kr("005930", count=5, now=_after_cutoff_now())
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


# ---------------------------------------------------------------------------
# KR intraday live-passthrough gate (ROB-639 review fix #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_first_kr_intraday_session_bypasses_db_even_when_fresh():
    """11:00 KST on a KRX session day → None (live serves the forming bar),
    even though the DB would be sufficient and fresh. Non-tautological: the
    repository is patched to return fresh rows and must not even be hit."""
    latest, _prev = _krx_latest_and_prev_session()
    intraday_now = _kst_at(latest, 11, 0)
    closed_utc = dt.datetime.combine(latest, dt.time(6, 31), tzinfo=UTC)
    rows = [
        _make_row("005930", "KRX", closed_utc - timedelta(days=i), 70000.0 + i)
        for i in range(5)
    ]

    fetch_mock = AsyncMock(return_value=list(reversed(rows)))
    with patch(
        "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
        new=fetch_mock,
    ):
        result = await cache_first_kr("005930", count=5, now=intraday_now)

    assert result is None
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_first_kr_after_cutoff_serves_db_covering_today():
    """After 15:35 KST on a session day, a DB whose newest row covers today's
    session is served (real freshness logic — latest_exchange_session is NOT
    patched)."""
    latest, _prev = _krx_latest_and_prev_session()
    after_cutoff = _kst_at(latest, 16, 0)
    # 06:31 UTC = 15:31 KST → timestamped on the latest session's date.
    newest = dt.datetime.combine(latest, dt.time(6, 31), tzinfo=UTC)
    rows = [
        _make_row("005930", "KRX", newest - timedelta(days=i), 70000.0 + i)
        for i in range(5)
    ]

    with patch(
        "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
        new=AsyncMock(return_value=list(reversed(rows))),
    ):
        result = await cache_first_kr("005930", count=5, now=after_cutoff)

    assert result is not None
    assert len(result) == 5
    assert result["date"].max() == latest


@pytest.mark.asyncio
async def test_cache_first_kr_after_cutoff_stale_db_returns_none():
    """After 15:35 KST on a session day, a DB whose newest row only covers the
    PREVIOUS session is stale → None (live path must refresh today's close)."""
    latest, prev = _krx_latest_and_prev_session()
    after_cutoff = _kst_at(latest, 16, 0)
    newest = dt.datetime.combine(prev, dt.time(6, 31), tzinfo=UTC)
    rows = [
        _make_row("005930", "KRX", newest - timedelta(days=i), 70000.0 + i)
        for i in range(5)
    ]

    with patch(
        "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
        new=AsyncMock(return_value=list(reversed(rows))),
    ):
        result = await cache_first_kr("005930", count=5, now=after_cutoff)

    assert result is None


# ---------------------------------------------------------------------------
# Fail-open on DB errors (ROB-639 review fix #1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_first_kr_returns_none_when_db_raises():
    """Any DB exception must degrade to None (live fallback), never raise."""
    with patch(
        "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        result = await cache_first_kr("005930", count=5, now=_after_cutoff_now())
    assert result is None


@pytest.mark.asyncio
async def test_cache_first_us_returns_none_when_db_raises_after_lookup_failure():
    """Poisoned-session regression (CI failure shape): the exchange lookup
    fails AND the subsequent fetch raises (e.g. InFailedSQLTransactionError).
    cache_first_us must swallow it and return None so live Yahoo serves."""
    with (
        patch(
            "app.services.us_symbol_universe_service.get_us_exchange_by_symbol",
            new=AsyncMock(side_effect=RuntimeError("relation does not exist")),
        ),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(side_effect=RuntimeError("current transaction is aborted")),
        ),
    ):
        result = await cache_first_us("ZZFAILOPEN", count=5)
    assert result is None


@pytest.mark.asyncio
async def test_cache_first_us_rolls_back_session_after_lookup_failure():
    """After a failed exchange lookup the session must be rolled back before
    it is reused for fetch_recent (the aborted-transaction fix)."""
    fake_session = _FakeSession()
    rollback_mock = AsyncMock()
    fake_session.rollback = rollback_mock  # type: ignore[method-assign]

    with (
        patch("app.core.db.AsyncSessionLocal", new=lambda: fake_session),
        patch(
            "app.services.us_symbol_universe_service.get_us_exchange_by_symbol",
            new=AsyncMock(side_effect=RuntimeError("lookup failed")),
        ),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await cache_first_us("ZZROLLBACK", count=5)

    assert result is None
    rollback_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Forming-bar write-back guard (ROB-639 review fix #3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_back_kr_drops_forming_bar_intraday():
    """At 11:00 KST on session S, a frame holding [prev session, S] must only
    persist the prev-session row — S's bar is still forming."""
    latest, prev = _krx_latest_and_prev_session()
    intraday_now = _kst_at(latest, 11, 0)
    frame = pd.DataFrame(
        [
            {
                "date": prev,
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
            },
            {
                "date": latest,  # forming intraday bar
                "open": 105.0,
                "high": 108.0,
                "low": 104.0,
                "close": 107.0,
                "volume": 500.0,
                "value": 53500.0,
            },
        ]
    )
    upsert_mock = AsyncMock(return_value=1)

    with (
        patch("app.core.db.AsyncSessionLocal", new=lambda: _FakeSession()),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.upsert_rows",
            new=upsert_mock,
        ),
    ):
        upserted = await write_back_kr(frame, symbol="ZZWBKR1", now=intraday_now)

    assert upserted == 1
    upsert_mock.assert_awaited_once()
    persisted = upsert_mock.call_args.kwargs["rows"]
    assert [r.time_utc.date() for r in persisted] == [prev]


@pytest.mark.asyncio
async def test_write_back_kr_all_forming_rows_writes_nothing():
    """A frame containing only today's forming bar persists nothing."""
    latest, _prev = _krx_latest_and_prev_session()
    intraday_now = _kst_at(latest, 11, 0)
    frame = pd.DataFrame(
        [
            {
                "date": latest,
                "open": 105.0,
                "high": 108.0,
                "low": 104.0,
                "close": 107.0,
                "volume": 500.0,
                "value": 53500.0,
            }
        ]
    )
    upsert_mock = AsyncMock(return_value=1)

    with (
        patch("app.core.db.AsyncSessionLocal", new=lambda: _FakeSession()),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.upsert_rows",
            new=upsert_mock,
        ),
    ):
        upserted = await write_back_kr(frame, symbol="ZZWBKR2", now=intraday_now)

    assert upserted == 0
    upsert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_back_kr_after_cutoff_keeps_todays_bar():
    """After 15:35 KST, today's bar is final and must be persisted."""
    latest, prev = _krx_latest_and_prev_session()
    after_cutoff = _kst_at(latest, 16, 0)
    frame = pd.DataFrame(
        [
            {
                "date": prev,
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
            },
            {
                "date": latest,
                "open": 105.0,
                "high": 108.0,
                "low": 104.0,
                "close": 107.0,
                "volume": 500.0,
                "value": 53500.0,
            },
        ]
    )
    upsert_mock = AsyncMock(return_value=2)

    with (
        patch("app.core.db.AsyncSessionLocal", new=lambda: _FakeSession()),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.upsert_rows",
            new=upsert_mock,
        ),
    ):
        upserted = await write_back_kr(frame, symbol="ZZWBKR3", now=after_cutoff)

    assert upserted == 2
    persisted = upsert_mock.call_args.kwargs["rows"]
    assert sorted(r.time_utc.date() for r in persisted) == sorted([prev, latest])


@pytest.mark.asyncio
async def test_write_back_us_drops_forming_bar_and_preserves_adj_close():
    """Mid-session US write-back drops the forming bar; a frame without an
    adj_close column must not update adj_close (yahoo_fallback guard)."""
    latest, prev = _xnys_latest_and_prev_session()
    # 15:00 UTC is mid-session on any XNYS session day (incl. half days).
    mid_session_now = dt.datetime.combine(latest, dt.time(15, 0), tzinfo=UTC)
    frame = pd.DataFrame(
        [
            {
                "date": prev,
                "open": 150.0,
                "high": 152.0,
                "low": 148.0,
                "close": 151.0,
                "volume": 1000.0,
                "value": 151000.0,
            },
            {
                "date": latest,  # forming intraday bar
                "open": 151.0,
                "high": 153.0,
                "low": 150.0,
                "close": 152.0,
                "volume": 500.0,
                "value": 76000.0,
            },
        ]
    )
    upsert_mock = AsyncMock(return_value=1)

    with (
        patch("app.core.db.AsyncSessionLocal", new=lambda: _FakeSession()),
        patch(
            "app.services.daily_candles.repository.DailyCandlesRepository.upsert_rows",
            new=upsert_mock,
        ),
    ):
        upserted = await write_back_us(
            frame,
            symbol="ZZWBUS1",
            partition="NASD",
            source="yahoo",
            now=mid_session_now,
        )

    assert upserted == 1
    upsert_mock.assert_awaited_once()
    kwargs = upsert_mock.call_args.kwargs
    assert [r.time_utc.date() for r in kwargs["rows"]] == [prev]
    assert kwargs["update_adj_close"] is False


def test_upsert_sql_excludes_adj_close_from_update_when_guarded():
    """update_adj_close=False keeps adj_close in the INSERT column list but
    out of the ON CONFLICT UPDATE SET."""
    from app.services.daily_candles.repository import (
        _TABLE_CONFIGS,
        DailyCandlesRepository,
        MarketKey,
    )

    cfg = _TABLE_CONFIGS[MarketKey.US]
    guarded = str(
        DailyCandlesRepository._build_market_upsert(
            cfg, with_adj_close=True, update_adj_close=False
        )
    )
    default = str(
        DailyCandlesRepository._build_market_upsert(
            cfg, with_adj_close=True, update_adj_close=True
        )
    )
    assert "adj_close" in guarded  # still inserted
    assert "adj_close=EXCLUDED.adj_close" not in guarded
    assert "adj_close=EXCLUDED.adj_close" in default
