"""hk 6030 — ``get_ohlcv(end_date=...)`` is a hard cutoff on every market.

The recorded defect: ``_get_ohlcv_impl(symbol="KRW-BTC", count=30, period="day",
end_date="2026-10-31", market="crypto")`` returned 30 rows ending 2026-11-20 —
the OHLCV cache ignored ``end_date`` and served the latest window.  These tests
pin the contract for the cache path and the provider path of every market
(crypto / equity_kr / equity_us): no returned row is after the cutoff, ``count``
fills backwards from it and the cutoff day itself is included for date-only
input.  Indicator values must also be computed from rows up to the cutoff only.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
from app.core.config import settings
from app.mcp_server.tooling import market_data_quotes
from app.mcp_server.tooling.market_data_indicators import _calculate_rsi_series
from app.services import upbit_ohlcv_cache, yahoo_ohlcv_cache
from tests._mcp_tooling_support import _patch_runtime_attr, build_tools
from tests.ohlcv_cache_fakes import FakeRedis


def _frame(dates: list[date], *, close_base: float = 100.0) -> pd.DataFrame:
    rows = []
    for idx, candle_date in enumerate(dates):
        close = close_base + idx
        rows.append(
            {
                "date": candle_date,
                "open": close - 1.0,
                "high": close + 2.0,
                "low": close - 2.0,
                "close": close,
                "volume": 1000.0 + idx,
                "value": (1000.0 + idx) * close,
            }
        )
    return pd.DataFrame(rows)


def _date_range(start: date, end: date) -> list[date]:
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _row_dates(payload: dict) -> list[date]:
    return [date.fromisoformat(str(row["date"])) for row in payload["rows"]]


async def _mock_redis(monkeypatch, module, fake: FakeRedis) -> None:
    async def _get_redis_client() -> FakeRedis:
        return fake

    monkeypatch.setattr(module, "_get_redis_client", _get_redis_client)


@pytest.fixture(autouse=True)
def _disable_real_caches_by_default(monkeypatch):
    # Each test opts into the cache it exercises; never let a test reach a
    # real Redis/network path by accident.
    monkeypatch.setattr(settings, "upbit_ohlcv_cache_enabled", False, raising=False)
    monkeypatch.setattr(settings, "yahoo_ohlcv_cache_enabled", False, raising=False)
    monkeypatch.setattr(settings, "kis_ohlcv_cache_enabled", False, raising=False)


# ============================================================================
# crypto
# ============================================================================


@pytest.mark.asyncio
async def test_crypto_cached_path_fills_backwards_from_date_only_cutoff(monkeypatch):
    """Cache hit must serve the window ending at the cutoff, not the latest one."""
    tools = build_tools()
    monkeypatch.setattr(settings, "upbit_ohlcv_cache_enabled", True, raising=False)
    monkeypatch.setattr(settings, "upbit_ohlcv_cache_max_days", 400, raising=False)

    fake = FakeRedis()
    dates_key, rows_key, _, _ = upbit_ohlcv_cache._keys("KRW-BTC", "day")
    # The cache holds 60 closed days through 2026-11-20 (a "latest window").
    cached_dates = _date_range(date(2026, 9, 22), date(2026, 11, 20))
    await upbit_ohlcv_cache._upsert_rows(
        fake, dates_key, rows_key, _frame(cached_dates)
    )
    await _mock_redis(monkeypatch, upbit_ohlcv_cache, fake)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: date(2026, 11, 20),
    )
    raw_fetch = AsyncMock(side_effect=AssertionError("cache hit must not fetch"))
    monkeypatch.setattr(upbit_service, "_fetch_ohlcv_raw", raw_fetch)

    result = await tools["get_ohlcv"](
        "KRW-BTC",
        count=30,
        period="day",
        end_date="2026-10-31",
        market="crypto",
    )

    returned = _row_dates(result)
    assert len(returned) == 30
    assert returned[-1] == date(2026, 10, 31)  # cutoff day is included
    assert max(returned) == date(2026, 10, 31)
    assert returned[0] == date(2026, 10, 2)
    raw_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_crypto_cache_miss_backfills_from_the_cutoff_not_the_latest_window(
    monkeypatch,
):
    """An empty cache must fetch its window ending at the cutoff."""
    tools = build_tools()
    monkeypatch.setattr(settings, "upbit_ohlcv_cache_enabled", True, raising=False)
    monkeypatch.setattr(settings, "upbit_ohlcv_cache_max_days", 400, raising=False)

    fake = FakeRedis()
    await _mock_redis(monkeypatch, upbit_ohlcv_cache, fake)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: date(2026, 11, 20),
    )

    fetched_windows: list[date] = []

    async def _raw_fetch(market, days, period, end_date):
        assert market == "KRW-BTC"
        fetched_windows.append(end_date.date())
        return _frame(_date_range(date(2026, 10, 2), date(2026, 10, 31)))

    monkeypatch.setattr(upbit_service, "_fetch_ohlcv_raw", _raw_fetch)

    result = await tools["get_ohlcv"](
        "KRW-BTC",
        count=30,
        period="day",
        end_date="2026-10-31",
        market="crypto",
    )

    returned = _row_dates(result)
    assert len(returned) == 30
    assert returned[-1] == date(2026, 10, 31)
    assert max(returned) == date(2026, 10, 31)
    assert fetched_windows and set(fetched_windows) == {date(2026, 10, 31)}


@pytest.mark.asyncio
async def test_crypto_uncached_path_drops_rows_after_cutoff(monkeypatch):
    """The provider window is not trusted: rows after the cutoff are dropped."""
    tools = build_tools()
    provider_frame = _frame(_date_range(date(2026, 10, 27), date(2026, 11, 2)))
    fetch_mock = AsyncMock(return_value=provider_frame)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", fetch_mock)

    result = await tools["get_ohlcv"](
        "KRW-BTC",
        count=10,
        period="day",
        end_date="2026-10-31",
        market="crypto",
    )

    returned = _row_dates(result)
    assert returned == _date_range(date(2026, 10, 27), date(2026, 10, 31))
    assert max(returned) == date(2026, 10, 31)
    assert fetch_mock.await_args.kwargs["end_date"].date() == date(2026, 10, 31)


# ============================================================================
# equity_us
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_external_providers")
async def test_us_cached_path_fills_backwards_from_date_only_cutoff(monkeypatch):
    tools = build_tools()
    monkeypatch.setattr(settings, "yahoo_ohlcv_cache_enabled", True, raising=False)
    monkeypatch.setattr(settings, "yahoo_ohlcv_cache_max_days", 400, raising=False)

    fake = FakeRedis()
    dates_key, rows_key, _, _ = yahoo_ohlcv_cache._keys("AAPL", "day")
    cached_dates = _date_range(date(2026, 2, 27), date(2026, 6, 26))
    await yahoo_ohlcv_cache._upsert_rows(
        fake, dates_key, rows_key, _frame(cached_dates)
    )
    await _mock_redis(monkeypatch, yahoo_ohlcv_cache, fake)
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: date(2026, 6, 26),
    )
    raw_fetch = AsyncMock(side_effect=AssertionError("cache hit must not fetch"))
    monkeypatch.setattr(yahoo_service, "_fetch_ohlcv_raw", raw_fetch)

    result = await tools["get_ohlcv"](
        "AAPL",
        market="us",
        count=30,
        period="day",
        end_date="2026-05-15",
    )

    returned = _row_dates(result)
    assert len(returned) == 30
    assert returned[-1] == date(2026, 5, 15)  # cutoff day is included
    assert max(returned) == date(2026, 5, 15)
    raw_fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_external_providers")
async def test_us_uncached_path_drops_rows_after_cutoff(monkeypatch):
    tools = build_tools()
    provider_frame = _frame(_date_range(date(2026, 5, 11), date(2026, 5, 18)))
    fetch_mock = AsyncMock(return_value=provider_frame)
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", fetch_mock)

    result = await tools["get_ohlcv"](
        "AAPL",
        market="us",
        count=10,
        period="day",
        end_date="2026-05-15",
    )

    returned = _row_dates(result)
    assert returned == _date_range(date(2026, 5, 11), date(2026, 5, 15))
    assert max(returned) == date(2026, 5, 15)
    assert fetch_mock.await_args.kwargs["end_date"].date() == date(2026, 5, 15)


# ============================================================================
# equity_kr
# ============================================================================


@pytest.mark.asyncio
async def test_kr_day_drops_rows_after_cutoff_and_skips_cache(monkeypatch):
    """KR day already bypasses the Redis cache for historical windows; the
    provider rows themselves must still be cut at ``end_date``."""
    tools = build_tools()
    monkeypatch.setattr(settings, "kis_ohlcv_cache_enabled", True, raising=False)
    cache_mock = AsyncMock(
        side_effect=AssertionError("historical query must bypass cache")
    )
    monkeypatch.setattr(market_data_quotes.kis_ohlcv_cache, "get_candles", cache_mock)

    provider_frame = _frame(_date_range(date(2026, 10, 26), date(2026, 11, 2)))

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            assert code == "005930"
            assert pd.Timestamp(end_date).date() == date(2026, 10, 30)
            return provider_frame

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"](
        "005930",
        market="kr",
        count=10,
        period="day",
        end_date="2026-10-30",
    )

    returned = _row_dates(result)
    assert returned == _date_range(date(2026, 10, 26), date(2026, 10, 30))
    assert max(returned) == date(2026, 10, 30)
    cache_mock.assert_not_awaited()


# ============================================================================
# indicators
# ============================================================================


@pytest.mark.asyncio
async def test_indicators_are_computed_only_from_rows_up_to_the_cutoff(monkeypatch):
    tools = build_tools()
    trade_dates = _date_range(date(2026, 1, 1), date(2026, 1, 25))
    frame = _frame(trade_dates)
    # Alternate gains and losses so the Wilder RSI is defined (a monotonic
    # series is the zero-loss case and yields null).
    frame["close"] = [
        100.0 + (5.0 if idx % 2 == 0 else -4.0) + idx * 0.1 for idx in range(len(frame))
    ]
    frame.loc[frame["date"] > date(2026, 1, 20), ["close"]] = 10_000.0
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", AsyncMock(return_value=frame))

    enriched: list[pd.DataFrame] = []
    original_enrich = market_data_quotes._enrich_ohlcv_with_indicators

    def _spy_enrich(df, period, *, canonical_daily=None):
        enriched.append(df.copy())
        return original_enrich(df, period, canonical_daily=canonical_daily)

    monkeypatch.setattr(
        market_data_quotes, "_enrich_ohlcv_with_indicators", _spy_enrich
    )

    result = await tools["get_ohlcv"](
        "KRW-BTC",
        count=30,
        period="day",
        end_date="2026-01-20",
        market="crypto",
        include_indicators=True,
    )

    kept = frame.loc[frame["date"] <= date(2026, 1, 20)]
    expected_rsi = _calculate_rsi_series(
        pd.to_numeric(kept["close"], errors="coerce")
    ).round(2)
    returned = _row_dates(result)
    assert returned[-1] == date(2026, 1, 20)
    assert max(returned) == date(2026, 1, 20)
    # Indicator enrichment must receive only rows at or before the cutoff.
    assert len(enriched) == 1
    assert max(enriched[0]["date"]) == date(2026, 1, 20)
    assert result["rows"][-1]["rsi_14"] == pytest.approx(float(expected_rsi.iloc[-1]))
