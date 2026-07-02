"""Tests for the fetch-layer Redis cache behind analyze_stock (ROB-638).

Design under test: ONLY slowly-changing provider fetch outputs are cached
(KR naver snapshot, US yfinance valuation/opinions bundle, US finnhub profile).
Quote/price, indicators (RSI), support/resistance + intraday re-sign, and the
recommendation recompute on EVERY call. Crypto never touches the cache.

All tests are hermetic: tests/conftest.py forces ANALYZE_FETCH_CACHE_ENABLED
off, so the real client factory returns None; cache behaviour is exercised by
patching ``analyze_cache._get_redis_client`` with an in-memory fake.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.core import analyze_cache
from app.mcp_server.tooling import analysis_analyze
from app.mcp_server.tooling import analysis_tool_handlers as handlers

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


class _FakeRedis:
    """Minimal async in-memory Redis for string get/set with TTL tracking."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.get_call_count = 0
        self.set_call_count = 0

    async def get(self, key: str) -> str | None:
        self.get_call_count += 1
        return self.store.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        self.set_call_count += 1
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True


@pytest.fixture(autouse=True)
def _reset_analyze_cache_singleton(monkeypatch):
    """Reset the module-level Redis client between tests."""
    monkeypatch.setattr(analyze_cache, "_REDIS_CLIENT", None)
    yield
    monkeypatch.setattr(analyze_cache, "_REDIS_CLIENT", None)


@pytest.fixture
def patch_redis(monkeypatch):
    """Force analyze_cache to use a fresh fake Redis client."""

    def make(fake: Any | None = None):
        if fake is None:
            fake = _FakeRedis()

        async def _get_client():
            return fake

        monkeypatch.setattr(analyze_cache, "_get_redis_client", _get_client)
        return fake

    return make


# ---------------------------------------------------------------------------
# Pure key / TTL unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_cache_key_naver_uses_kst_date():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    assert (
        analyze_cache._fetch_cache_key("naver", "005930", now)
        == "analyze_fetch:naver:005930:2026-07-02"
    )


@pytest.mark.unit
def test_fetch_cache_key_us_providers_use_et_date():
    # 2026-07-02 10:00 KST == 2026-07-01 21:00 ET: the US key date must be
    # ET-based so it rolls at ET midnight together with the TTL clock.
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    assert (
        analyze_cache._fetch_cache_key("yfinance", "aapl", now)
        == "analyze_fetch:yfinance:AAPL:2026-07-01"
    )
    assert (
        analyze_cache._fetch_cache_key("finnhub_profile", "AAPL", now)
        == "analyze_fetch:finnhub_profile:AAPL:2026-07-01"
    )


@pytest.mark.unit
def test_fetch_cache_key_uppercases_symbol():
    now = datetime(2026, 7, 2, 12, 0, tzinfo=ET)
    key = analyze_cache._fetch_cache_key("yfinance", "brk.b", now)
    assert ":BRK.B:" in key


@pytest.mark.unit
def test_ttl_naver_before_session_close_targets_close():
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    # 10:00 -> 15:35 KST = 5h35m = 20100s
    assert analyze_cache._fetch_cache_ttl_seconds("naver", now) == 20100


@pytest.mark.unit
def test_ttl_naver_after_close_targets_midnight_kst():
    now = datetime(2026, 7, 2, 16, 0, tzinfo=KST)
    assert analyze_cache._fetch_cache_ttl_seconds("naver", now) == 28800


@pytest.mark.unit
def test_ttl_naver_just_before_close_is_near_zero():
    now = datetime(2026, 7, 2, 15, 34, 59, tzinfo=KST)
    assert analyze_cache._fetch_cache_ttl_seconds("naver", now) == 1


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["yfinance", "finnhub_profile"])
def test_ttl_us_providers_before_close_targets_close_et(provider):
    now = datetime(2026, 7, 2, 15, 0, tzinfo=ET)
    assert analyze_cache._fetch_cache_ttl_seconds(provider, now) == 3600


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["yfinance", "finnhub_profile"])
def test_ttl_us_providers_after_close_targets_midnight_et(provider):
    now = datetime(2026, 7, 2, 20, 0, tzinfo=ET)
    assert analyze_cache._fetch_cache_ttl_seconds(provider, now) == 14400


@pytest.mark.unit
def test_ttl_unknown_provider_falls_back_to_15min():
    now = datetime(2026, 7, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    assert analyze_cache._fetch_cache_ttl_seconds("unknown", now) == 900


# ---------------------------------------------------------------------------
# Hermetic guard: settings flag gates client creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_redis_client_disabled_in_test_env(monkeypatch):
    """conftest forces ANALYZE_FETCH_CACHE_ENABLED=false: no client is ever
    created — the factory must not even attempt a connection."""

    def _explode():
        raise AssertionError("create_redis_client must not be called in tests")

    monkeypatch.setattr(analyze_cache, "create_redis_client", _explode)
    assert await analyze_cache._get_redis_client() is None
    assert analyze_cache._REDIS_CLIENT is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_redis_client_enabled_flag_creates_client(monkeypatch):
    from app.core.config import settings

    fake = _FakeRedis()

    async def _create():
        return fake

    monkeypatch.setattr(settings, "analyze_fetch_cache_enabled", True)
    monkeypatch.setattr(analyze_cache, "create_redis_client", _create)
    assert await analyze_cache._get_redis_client() is fake


# ---------------------------------------------------------------------------
# get/set helper tests (with fake redis)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_cached_returns_none_on_miss():
    fake = _FakeRedis()
    payload, fetched_at = await analyze_cache.get_cached_fetch_payload(
        fake, "naver", "005930"
    )
    assert payload is None and fetched_at is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_cached_returns_none_when_redis_is_none():
    payload, fetched_at = await analyze_cache.get_cached_fetch_payload(
        None, "naver", "005930"
    )
    assert payload is None and fetched_at is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_set_then_get_roundtrip_with_envelope():
    fake = _FakeRedis()
    payload = {"valuation": {"per": 12.3}, "opinions": {"consensus": {"rows_used": 3}}}
    await analyze_cache.set_cached_fetch_payload(
        fake, "naver", "005930", payload, fetched_at="2026-07-02T10:00:00+09:00"
    )
    cached, fetched_at = await analyze_cache.get_cached_fetch_payload(
        fake, "naver", "005930"
    )
    assert cached == payload
    assert fetched_at == "2026-07-02T10:00:00+09:00"
    key = analyze_cache._fetch_cache_key("naver", "005930")
    assert fake.ttls[key] > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_set_defaults_fetched_at_to_now_kst():
    fake = _FakeRedis()
    await analyze_cache.set_cached_fetch_payload(fake, "naver", "005930", {"x": 1})
    _, fetched_at = await analyze_cache.get_cached_fetch_payload(
        fake, "naver", "005930"
    )
    assert isinstance(fetched_at, str) and fetched_at


@pytest.mark.asyncio
@pytest.mark.unit
async def test_set_is_noop_when_redis_is_none():
    await analyze_cache.set_cached_fetch_payload(None, "naver", "005930", {"x": 1})


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps([1, 2, 3]),
        json.dumps({"payload": "not-a-dict", "fetched_at": "t"}),
        json.dumps({"payload": {"x": 1}}),  # missing fetched_at
        json.dumps({"fetched_at": "t"}),  # missing payload
    ],
)
async def test_get_returns_none_on_malformed_envelope(raw):
    fake = _FakeRedis()
    key = analyze_cache._fetch_cache_key("naver", "005930")
    fake.store[key] = raw
    payload, fetched_at = await analyze_cache.get_cached_fetch_payload(
        fake, "naver", "005930"
    )
    assert payload is None and fetched_at is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_swallows_redis_errors():
    class _ExplodingRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

    payload, fetched_at = await analyze_cache.get_cached_fetch_payload(
        _ExplodingRedis(), "naver", "005930"
    )
    assert payload is None and fetched_at is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_set_swallows_redis_errors():
    class _ExplodingRedis:
        async def set(self, *args, **kwargs):
            raise RuntimeError("redis down")

    await analyze_cache.set_cached_fetch_payload(
        _ExplodingRedis(), "naver", "005930", {"x": 1}
    )


# ---------------------------------------------------------------------------
# KR pipeline: cached consensus, fresh price/RSI/S&R
# ---------------------------------------------------------------------------


def _ohlcv_df() -> pd.DataFrame:
    idx = pd.date_range("2026-06-01", periods=5, freq="D")
    return pd.DataFrame(
        {
            "open": [100.0] * 5,
            "high": [110.0] * 5,
            "low": [90.0] * 5,
            "close": [105.0] * 5,
            "volume": [1000] * 5,
        },
        index=idx,
    )


@pytest.fixture
def kr_pipeline(monkeypatch):
    """Stub every non-cached surface of the KR analyze pipeline with counters."""
    state = {
        "price": 70000.0,
        "naver_calls": 0,
        "quote_calls": 0,
        "indicator_calls": 0,
        "sr_calls": 0,
        "naver_payload": {
            "valuation": {
                "instrument_type": "equity_kr",
                "source": "naver",
                "per": 12.3,
            },
            "news": {"symbol": "005930", "count": 0, "news": []},
            "opinions": {
                "instrument_type": "equity_kr",
                "source": "naver",
                "consensus": {"rows_used": 3, "avg_target_price": 90000},
            },
        },
    }
    df = _ohlcv_df()

    async def fake_ohlcv(symbol, market_type, count=250):
        return df

    async def fake_quote(symbol, ohlcv_df):
        state["quote_calls"] += 1
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": state["price"],
            "price_as_of": "2026-07-02T10:00:00+09:00",
            "is_stale_price": False,
        }

    async def fake_indicators(symbol, indicators, market=None, preloaded_df=None):
        state["indicator_calls"] += 1
        return {"symbol": symbol, "indicators": {"rsi": {"14": 55.0}}}

    async def fake_sr(symbol, market=None, preloaded_df=None):
        state["sr_calls"] += 1
        return {
            "supports": [{"price": 65000.0, "distance_pct": -7.1}],
            "resistances": [{"price": 78000.0, "distance_pct": 11.4}],
            "current_price": 70000.0,
        }

    async def fake_naver(symbol, news_limit, opinions_limit):
        state["naver_calls"] += 1
        return copy.deepcopy(state["naver_payload"])

    monkeypatch.setattr(analysis_analyze, "_fetch_ohlcv_for_indicators", fake_ohlcv)
    monkeypatch.setattr(analysis_analyze, "_resolve_kr_quote", fake_quote)
    monkeypatch.setattr(analysis_analyze, "_get_indicators_impl", fake_indicators)
    monkeypatch.setattr(analysis_analyze, "_get_support_resistance_impl", fake_sr)
    monkeypatch.setattr(analysis_analyze, "_fetch_analysis_snapshot_naver", fake_naver)
    return state


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kr_hit_serves_cached_consensus_but_recomputes_live(
    kr_pipeline, patch_redis
):
    fake_redis = patch_redis()

    first = await analysis_analyze.analyze_stock_impl("005930", "kr")
    assert kr_pipeline["naver_calls"] == 1
    assert first["cache_hit"] is False
    assert first["derived_as_of"]
    assert first["quote"]["price"] == 70000.0
    assert fake_redis.set_call_count == 1

    # Live price moves between calls.
    kr_pipeline["price"] = 72000.0

    second = await analysis_analyze.analyze_stock_impl("005930", "kr")
    # Provider fetch was served from cache — naver NOT re-fetched.
    assert kr_pipeline["naver_calls"] == 1
    assert second["cache_hit"] is True
    assert second["derived_as_of"] == first["derived_as_of"]
    # Consensus / valuation come from the cache.
    assert second["opinions"]["consensus"]["avg_target_price"] == 90000
    assert second["valuation"]["per"] == 12.3
    # Live surfaces recomputed EVERY call.
    assert kr_pipeline["quote_calls"] == 2
    assert kr_pipeline["indicator_calls"] == 2
    assert kr_pipeline["sr_calls"] == 2
    assert second["quote"]["price"] == 72000.0
    # ROB-541 intraday S/R re-sign ran against the NEW live price.
    assert second["support_resistance"]["distance_basis_price"] == 72000.0
    # Recommendation was rebuilt fresh (not served from any cache).
    assert second["recommendation"]["action"] in {"buy", "hold", "sell"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kr_refresh_bypasses_cache_read_but_rewrites(kr_pipeline, patch_redis):
    fake_redis = patch_redis()

    await analysis_analyze.analyze_stock_impl("005930", "kr")
    assert kr_pipeline["naver_calls"] == 1
    assert fake_redis.set_call_count == 1

    result = await analysis_analyze.analyze_stock_impl("005930", "kr", refresh=True)
    # Read bypassed — provider re-fetched; fresh value written back.
    assert kr_pipeline["naver_calls"] == 2
    assert result["cache_hit"] is False
    assert fake_redis.set_call_count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kr_degraded_snapshot_is_not_cached(
    kr_pipeline, patch_redis, monkeypatch
):
    fake_redis = patch_redis()

    async def empty_naver(symbol, news_limit, opinions_limit):
        kr_pipeline["naver_calls"] += 1
        return {}

    monkeypatch.setattr(analysis_analyze, "_fetch_analysis_snapshot_naver", empty_naver)

    first = await analysis_analyze.analyze_stock_impl("005930", "kr")
    assert fake_redis.set_call_count == 0
    assert first["cache_hit"] is False
    assert "valuation" not in first

    second = await analysis_analyze.analyze_stock_impl("005930", "kr")
    # No cached entry -> provider fetched again.
    assert kr_pipeline["naver_calls"] == 2
    assert second["cache_hit"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kr_provider_exception_is_not_cached(
    kr_pipeline, patch_redis, monkeypatch
):
    fake_redis = patch_redis()

    async def broken_naver(symbol, news_limit, opinions_limit):
        kr_pipeline["naver_calls"] += 1
        raise RuntimeError("naver down")

    monkeypatch.setattr(
        analysis_analyze, "_fetch_analysis_snapshot_naver", broken_naver
    )

    result = await analysis_analyze.analyze_stock_impl("005930", "kr")
    # Fetch failure is swallowed by the gather (degraded analysis), never cached.
    assert fake_redis.set_call_count == 0
    assert result["cache_hit"] is False
    assert "valuation" not in result
    # Live surfaces still computed.
    assert result["quote"]["price"] == 70000.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kr_redis_error_fails_open_to_fresh_fetch(kr_pipeline, patch_redis):
    class _ExplodingRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

        async def set(self, *args, **kwargs):
            raise RuntimeError("redis down")

    patch_redis(_ExplodingRedis())

    first = await analysis_analyze.analyze_stock_impl("005930", "kr")
    second = await analysis_analyze.analyze_stock_impl("005930", "kr")
    assert kr_pipeline["naver_calls"] == 2
    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert second["opinions"]["consensus"]["avg_target_price"] == 90000


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kr_redis_unavailable_falls_through(kr_pipeline, monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(analyze_cache, "_get_redis_client", _none)

    result = await analysis_analyze.analyze_stock_impl("005930", "kr")
    assert kr_pipeline["naver_calls"] == 1
    assert result["cache_hit"] is False
    assert result["derived_as_of"]


# ---------------------------------------------------------------------------
# Crypto: never touches the cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_crypto_pipeline_never_touches_cache(monkeypatch):
    cache_client_calls = {"count": 0}

    async def counting_client():
        cache_client_calls["count"] += 1
        return _FakeRedis()

    monkeypatch.setattr(analyze_cache, "_get_redis_client", counting_client)

    df = _ohlcv_df()

    async def fake_ohlcv(symbol, market_type, count=250):
        return df

    async def fake_quote(symbol, market_type):
        return {"symbol": symbol, "price": 95000000.0}

    async def fake_indicators(symbol, indicators, market=None, preloaded_df=None):
        return {"symbol": symbol, "indicators": {"rsi": {"14": 61.2}}}

    async def fake_sr(symbol, market=None, preloaded_df=None):
        return {"supports": [], "resistances": []}

    async def fake_news(symbol, market, limit):
        return {"symbol": symbol, "news": []}

    monkeypatch.setattr(analysis_analyze, "_fetch_ohlcv_for_indicators", fake_ohlcv)
    monkeypatch.setattr(analysis_analyze, "_get_quote_impl", fake_quote)
    monkeypatch.setattr(analysis_analyze, "_get_indicators_impl", fake_indicators)
    monkeypatch.setattr(analysis_analyze, "_get_support_resistance_impl", fake_sr)
    monkeypatch.setattr(analysis_analyze, "_fetch_news_finnhub", fake_news)

    result = await analysis_analyze.analyze_stock_impl("KRW-BTC", "crypto")

    assert cache_client_calls["count"] == 0, "crypto must never touch the cache"
    assert result["cache_hit"] is False
    assert result["derived_as_of"]  # fresh fetch timestamp
    assert result["quote"]["price"] == 95000000.0


# ---------------------------------------------------------------------------
# US pipeline: yfinance bundle + finnhub profile cached with ET-dated keys
# ---------------------------------------------------------------------------


@pytest.fixture
def us_pipeline(monkeypatch):
    from app.mcp_server.tooling.fundamentals_sources_yfinance import _YFinanceSnapshot

    state = {
        "price": 200.0,
        "quote_calls": 0,
        "snapshot_calls": 0,
        "profile_calls": 0,
        "news_calls": 0,
        "session_builds": 0,
        "valuation_calls": 0,
        "opinions_calls": 0,
    }
    df = _ohlcv_df()

    async def fake_ohlcv(symbol, market_type, count=250):
        return df

    async def fake_quote(symbol, market_type):
        state["quote_calls"] += 1
        return {"symbol": symbol, "price": state["price"]}

    async def fake_indicators(symbol, indicators, market=None, preloaded_df=None):
        return {"symbol": symbol, "indicators": {"rsi": {"14": 48.0}}}

    async def fake_sr(symbol, market=None, preloaded_df=None):
        return {"supports": [], "resistances": []}

    def fake_snapshot(ticker):
        state["snapshot_calls"] += 1
        return _YFinanceSnapshot(info={"currentPrice": 200.0})

    async def fake_valuation(symbol, snapshot=None, session=None):
        state["valuation_calls"] += 1
        return {
            "instrument_type": "equity_us",
            "source": "yfinance",
            "symbol": symbol.upper(),
            "per": 30.5,
        }

    async def fake_opinions(symbol, limit, snapshot=None, session=None):
        state["opinions_calls"] += 1
        return {
            "instrument_type": "equity_us",
            "source": "yfinance",
            "symbol": symbol.upper(),
            "count": 0,
            "opinions": [],
            "consensus": {"total_count": 12, "avg_target_price": 220.0},
        }

    async def fake_profile(symbol):
        state["profile_calls"] += 1
        return {"symbol": symbol, "source": "finnhub", "name": "Apple Inc."}

    async def fake_news(symbol, market, limit):
        state["news_calls"] += 1
        return {"symbol": symbol, "news": []}

    def fake_build_session():
        state["session_builds"] += 1
        return object()

    monkeypatch.setattr(analysis_analyze, "_fetch_ohlcv_for_indicators", fake_ohlcv)
    monkeypatch.setattr(analysis_analyze, "_get_quote_impl", fake_quote)
    monkeypatch.setattr(analysis_analyze, "_get_indicators_impl", fake_indicators)
    monkeypatch.setattr(analysis_analyze, "_get_support_resistance_impl", fake_sr)
    monkeypatch.setattr(analysis_analyze, "_collect_yfinance_snapshot", fake_snapshot)
    monkeypatch.setattr(analysis_analyze, "_fetch_valuation_yfinance", fake_valuation)
    monkeypatch.setattr(
        analysis_analyze, "_fetch_investment_opinions_yfinance", fake_opinions
    )
    monkeypatch.setattr(
        analysis_analyze, "_fetch_company_profile_finnhub", fake_profile
    )
    monkeypatch.setattr(analysis_analyze, "_fetch_news_finnhub", fake_news)
    monkeypatch.setattr(
        analysis_analyze, "build_yfinance_tracing_session", fake_build_session
    )
    monkeypatch.setattr(
        analysis_analyze, "close_yfinance_session", lambda session: None
    )
    monkeypatch.setattr(
        analysis_analyze.yf, "Ticker", lambda symbol, session=None: object()
    )
    return state


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_bundle_and_profile_cached_news_and_quote_fresh(
    us_pipeline, patch_redis
):
    fake_redis = patch_redis()

    first = await analysis_analyze.analyze_stock_impl("AAPL", "us")
    assert first["cache_hit"] is False
    assert us_pipeline["snapshot_calls"] == 1
    assert us_pipeline["profile_calls"] == 1
    assert us_pipeline["session_builds"] == 1
    # Both providers stored with ET-based key dates.
    et_date = datetime.now(ET).date().isoformat()
    assert f"analyze_fetch:yfinance:AAPL:{et_date}" in fake_redis.store
    assert f"analyze_fetch:finnhub_profile:AAPL:{et_date}" in fake_redis.store

    us_pipeline["price"] = 210.0
    second = await analysis_analyze.analyze_stock_impl("AAPL", "us")
    assert second["cache_hit"] is True
    assert second["derived_as_of"] == first["derived_as_of"]
    # Cached providers NOT re-fetched; no new yfinance session built.
    assert us_pipeline["snapshot_calls"] == 1
    assert us_pipeline["valuation_calls"] == 1
    assert us_pipeline["opinions_calls"] == 1
    assert us_pipeline["profile_calls"] == 1
    assert us_pipeline["session_builds"] == 1
    # Cached payloads served.
    assert second["valuation"]["per"] == 30.5
    assert second["opinions"]["consensus"]["avg_target_price"] == 220.0
    assert second["profile"]["name"] == "Apple Inc."
    # Live surfaces fresh EVERY call.
    assert us_pipeline["quote_calls"] == 2
    assert us_pipeline["news_calls"] == 2
    assert second["quote"]["price"] == 210.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_refresh_bypasses_both_provider_caches(us_pipeline, patch_redis):
    fake_redis = patch_redis()

    await analysis_analyze.analyze_stock_impl("AAPL", "us")
    result = await analysis_analyze.analyze_stock_impl("AAPL", "us", refresh=True)

    assert result["cache_hit"] is False
    assert us_pipeline["snapshot_calls"] == 2
    assert us_pipeline["profile_calls"] == 2
    # Fresh values written back on refresh (2 providers x 2 calls).
    assert fake_redis.set_call_count == 4


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_degraded_snapshot_bundle_not_cached(
    us_pipeline, patch_redis, monkeypatch
):
    from app.mcp_server.tooling.fundamentals_sources_yfinance import _YFinanceSnapshot

    fake_redis = patch_redis()

    def degraded_snapshot(ticker):
        us_pipeline["snapshot_calls"] += 1
        return _YFinanceSnapshot()  # every sub-fetch failed -> all None

    monkeypatch.setattr(
        analysis_analyze, "_collect_yfinance_snapshot", degraded_snapshot
    )

    result = await analysis_analyze.analyze_stock_impl("AAPL", "us")
    et_date = datetime.now(ET).date().isoformat()
    assert f"analyze_fetch:yfinance:AAPL:{et_date}" not in fake_redis.store
    # Profile is healthy and independently cached.
    assert f"analyze_fetch:finnhub_profile:AAPL:{et_date}" in fake_redis.store
    assert result["cache_hit"] is False


# ---------------------------------------------------------------------------
# Batch handler: refresh threading + metadata propagation
# ---------------------------------------------------------------------------


def _summary_formatter(sym, result, **kw):
    return handlers._summarize_analysis_result(sym, result)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_batch_threads_refresh_to_pipeline(monkeypatch):
    seen: list[bool] = []

    async def stub(sym, market, include_peers, refresh=False):
        seen.append(refresh)
        return {"symbol": sym, "market_type": "equity_kr", "source": "kis"}

    monkeypatch.setattr(handlers.analysis_screening, "_analyze_stock_impl", stub)

    await handlers.analyze_stock_batch_impl(
        ["005930"], market="kr", include_position=False, refresh=True
    )
    assert seen == [True]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_screening_alias_forwards_refresh_only_when_set(monkeypatch):
    from app.mcp_server.tooling import analysis_screening

    seen: list[dict[str, Any]] = []

    async def fake_impl(symbol, market=None, include_peers=False, **kwargs):
        seen.append({"symbol": symbol, **kwargs})
        return {"symbol": symbol}

    monkeypatch.setattr(
        analysis_screening.analysis_analyze, "analyze_stock_impl", fake_impl
    )

    await analysis_screening._analyze_stock_impl("005930", "kr", False)
    await analysis_screening._analyze_stock_impl("005930", "kr", False, refresh=True)
    assert seen == [
        {"symbol": "005930"},
        {"symbol": "005930", "refresh": True},
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_batch_default_keeps_legacy_three_arg_stub_contract(monkeypatch):
    """refresh defaults False and is NOT passed positionally — legacy 3-arg
    monkeypatched stubs of _analyze_stock_impl must keep working."""

    def stub(sym, market, include_peers):
        return {"symbol": sym, "market_type": "equity_kr", "source": "kis"}

    monkeypatch.setattr(handlers.analysis_screening, "_analyze_stock_impl", stub)

    result = await handlers.analyze_stock_batch_impl(
        ["005930"], market="kr", include_position=False
    )
    assert result["summary"]["successful"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_batch_propagates_fetch_cache_metadata_to_summary(monkeypatch):
    async def stub(sym, market, include_peers):
        return {
            "symbol": sym,
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 70000},
            "cache_hit": True,
            "derived_as_of": "2026-07-02T10:00:00+09:00",
        }

    monkeypatch.setattr(handlers.analysis_screening, "_analyze_stock_impl", stub)

    result = await handlers._run_batch_analysis(
        ["005930"], market="kr", include_peers=False, formatter=_summary_formatter
    )
    row = result["results"]["005930"]
    assert row["cache_hit"] is True
    assert row["derived_as_of"] == "2026-07-02T10:00:00+09:00"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_batch_error_row_carries_cache_metadata(monkeypatch):
    async def stub(sym, market, include_peers):
        raise ValueError("boom")

    monkeypatch.setattr(handlers.analysis_screening, "_analyze_stock_impl", stub)

    result = await handlers._run_batch_analysis(
        ["BADSYM"], market="kr", include_peers=False, formatter=_summary_formatter
    )
    row = result["results"]["BADSYM"]
    assert "error" in row
    assert row["cache_hit"] is False
    assert row["derived_as_of"] is None
