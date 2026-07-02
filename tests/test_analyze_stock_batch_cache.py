"""Tests for the Redis ephemeral cache on analyze_stock_batch (ROB-638)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core import analyze_cache
from app.mcp_server.tooling import analysis_tool_handlers as handlers


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


def _make_analysis(symbol: str, *, price: float = 100.0) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "market_type": "equity_kr",
        "source": "kis",
        "quote": {"price": price},
        "indicators": {"rsi": {"14": 55.0}},
        "support_resistance": {"supports": [90.0], "resistances": [110.0]},
        "opinions": {"consensus": "hold"},
        "recommendation": "HOLD",
    }


def _summary_formatter(sym, result, **kw):
    return handlers._summarize_analysis_result(sym, result)


@pytest.fixture(autouse=True)
def _reset_analyze_cache_singleton(monkeypatch):
    """Reset the module-level Redis client between tests."""
    monkeypatch.setattr(analyze_cache, "_REDIS_CLIENT", None)
    yield
    monkeypatch.setattr(analyze_cache, "_REDIS_CLIENT", None)


@pytest.fixture
def patch_redis(monkeypatch):
    """Force analyze_cache to use a fresh fake Redis client."""

    def make():
        fake = _FakeRedis()

        async def _get_client():
            return fake

        monkeypatch.setattr(analyze_cache, "_get_redis_client", _get_client)
        return fake

    return make


# ---------------------------------------------------------------------------
# Pure TTL / key-format unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cache_key_format():
    assert (
        analyze_cache._cache_key("kr", "005930", "2026-07-02")
        == "analyze_batch:kr:005930:2026-07-02"
    )


@pytest.mark.unit
def test_cache_key_uppercases_symbol():
    assert (
        analyze_cache._cache_key("us", "aapl", "2026-07-02")
        == "analyze_batch:us:AAPL:2026-07-02"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ("crypto", 3600),
        ("CRYPTO", 3600),
    ],
)
def test_cache_ttl_crypto_flat_1h(market, expected):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 7, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    assert analyze_cache._cache_ttl_seconds(market, now) == expected


@pytest.mark.unit
def test_cache_ttl_kr_before_session_close_targets_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    ttl = analyze_cache._cache_ttl_seconds("kr", now)
    # 10:00 -> 15:35 = 5h35m = 20100s
    assert ttl == 20100


@pytest.mark.unit
def test_cache_ttl_kr_just_before_close_is_near_zero():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
    now = datetime(2026, 7, 2, 15, 34, 59, tzinfo=KST)
    ttl = analyze_cache._cache_ttl_seconds("kr", now)
    assert ttl == 1


@pytest.mark.unit
def test_cache_ttl_kr_at_or_after_close_targets_midnight():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
    at_close = datetime(2026, 7, 2, 15, 35, 0, tzinfo=KST)
    assert analyze_cache._cache_ttl_seconds("kr", at_close) == 30300  # -> 00:00
    after_close = datetime(2026, 7, 2, 16, 0, tzinfo=KST)
    assert analyze_cache._cache_ttl_seconds("kr", after_close) == 28800


@pytest.mark.unit
def test_cache_ttl_us_before_close_targets_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    now = datetime(2026, 7, 2, 15, 0, tzinfo=ET)
    assert analyze_cache._cache_ttl_seconds("us", now) == 3600  # 15:00 -> 16:00


@pytest.mark.unit
def test_cache_ttl_us_after_close_targets_midnight_et():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    now = datetime(2026, 7, 2, 20, 0, tzinfo=ET)
    assert analyze_cache._cache_ttl_seconds("us", now) == 14400  # 20:00 -> 00:00


@pytest.mark.unit
def test_cache_ttl_unknown_market_falls_back_to_15min():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 7, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    assert analyze_cache._cache_ttl_seconds("unknown", now) == 900


@pytest.mark.unit
def test_resolve_market_for_symbol_detects_each_lane():
    assert analyze_cache._resolve_market_for_symbol("005930", None) == "kr"
    assert analyze_cache._resolve_market_for_symbol("AAPL", None) == "us"
    assert analyze_cache._resolve_market_for_symbol("KRW-BTC", None) == "crypto"


# ---------------------------------------------------------------------------
# get/set helper tests (with fake redis)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_cached_returns_none_on_miss():
    fake = _FakeRedis()
    assert await analyze_cache.get_cached_analyze_result(fake, "kr", "005930", "2026-07-02") is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_cached_returns_none_when_redis_is_none():
    assert await analyze_cache.get_cached_analyze_result(None, "kr", "005930", "2026-07-02") is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_set_then_get_roundtrip():
    fake = _FakeRedis()
    payload = {"symbol": "005930", "current_price": 70000}
    await analyze_cache.set_cached_analyze_result(
        fake, "kr", "005930", "2026-07-02", payload
    )
    cached = await analyze_cache.get_cached_analyze_result(
        fake, "kr", "005930", "2026-07-02"
    )
    assert cached == payload
    # TTL was set (KR during/after session -> positive)
    key = analyze_cache._cache_key("kr", "005930", "2026-07-02")
    assert fake.ttls[key] > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_set_is_noop_when_redis_is_none():
    await analyze_cache.set_cached_analyze_result(
        None, "kr", "005930", "2026-07-02", {"x": 1}
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_returns_none_on_malformed_payload():
    fake = _FakeRedis()
    key = analyze_cache._cache_key("kr", "005930", "2026-07-02")
    fake.store[key] = "not-json"
    assert await analyze_cache.get_cached_analyze_result(fake, "kr", "005930", "2026-07-02") is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_returns_none_on_non_dict_payload():
    fake = _FakeRedis()
    key = analyze_cache._cache_key("kr", "005930", "2026-07-02")
    fake.store[key] = json.dumps([1, 2, 3])
    assert await analyze_cache.get_cached_analyze_result(fake, "kr", "005930", "2026-07-02") is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_swallows_redis_errors():
    class _ExplodingRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

    assert (
        await analyze_cache.get_cached_analyze_result(
            _ExplodingRedis(), "kr", "005930", "2026-07-02"
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_set_swallows_redis_errors():
    class _ExplodingRedis:
        async def set(self, *args, **kwargs):
            raise RuntimeError("redis down")

    await analyze_cache.set_cached_analyze_result(
        _ExplodingRedis(), "kr", "005930", "2026-07-02", {"x": 1}
    )


# ---------------------------------------------------------------------------
# Integration: _run_batch_analysis with cache (patched pipeline + redis)
# ---------------------------------------------------------------------------


def _install_fake_pipeline(monkeypatch, results_by_symbol: dict[str, dict[str, Any]]):
    """Replace analysis_screening._analyze_stock_impl with a deterministic stub."""
    call_log: list[str] = []

    def _stub(sym, market, include_peers):
        call_log.append(sym)
        if sym not in results_by_symbol:
            raise ValueError(f"no stub for {sym}")
        return results_by_symbol[sym]

    monkeypatch.setattr(
        handlers.analysis_screening, "_analyze_stock_impl", _stub
    )
    return call_log


@pytest.mark.asyncio
@pytest.mark.unit
async def test_second_call_hits_cache_and_skips_pipeline(monkeypatch, patch_redis):
    patch_redis()
    call_log = _install_fake_pipeline(
        monkeypatch, {"005930": _make_analysis("005930", price=70000)}
    )

    formatter = _summary_formatter

    first = await handlers._run_batch_analysis(
        ["005930"], market="kr", include_peers=False, formatter=formatter
    )
    assert call_log == ["005930"]
    first_row = first["results"]["005930"]
    assert first_row["cache_hit"] is False
    assert first_row["derived_as_of"] is not None
    assert first_row["current_price"] == 70000

    second = await handlers._run_batch_analysis(
        ["005930"], market="kr", include_peers=False, formatter=formatter
    )
    assert call_log == ["005930"], "pipeline must not re-run on cache hit"
    second_row = second["results"]["005930"]
    assert second_row["cache_hit"] is True
    assert second_row["derived_as_of"] == first_row["derived_as_of"]
    assert second_row["current_price"] == 70000


@pytest.mark.asyncio
@pytest.mark.unit
async def test_different_symbol_misses_cache_and_runs_pipeline(monkeypatch, patch_redis):
    patch_redis()
    call_log = _install_fake_pipeline(
        monkeypatch,
        {
            "005930": _make_analysis("005930", price=70000),
            "000660": _make_analysis("000660", price=120000),
        },
    )
    formatter = _summary_formatter

    await handlers._run_batch_analysis(
        ["005930"], market="kr", include_peers=False, formatter=formatter
    )
    await handlers._run_batch_analysis(
        ["000660"], market="kr", include_peers=False, formatter=formatter
    )
    assert call_log == ["005930", "000660"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cache_keyed_by_symbol_within_same_batch(monkeypatch, patch_redis):
    """Two symbols in one batch each run the pipeline exactly once."""
    patch_redis()
    call_log = _install_fake_pipeline(
        monkeypatch,
        {
            "005930": _make_analysis("005930"),
            "000660": _make_analysis("000660", price=120000),
        },
    )
    formatter = _summary_formatter

    result = await handlers._run_batch_analysis(
        ["005930", "000660"], market="kr", include_peers=False, formatter=formatter
    )
    assert sorted(call_log) == ["000660", "005930"]
    for sym in ("005930", "000660"):
        assert result["results"][sym]["cache_hit"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_expired_entry_reruns_pipeline(monkeypatch, patch_redis):
    """Simulate TTL expiry by clearing the fake redis between calls."""
    fake_redis = patch_redis()
    call_log = _install_fake_pipeline(
        monkeypatch, {"005930": _make_analysis("005930")}
    )
    formatter = _summary_formatter

    await handlers._run_batch_analysis(
        ["005930"], market="kr", include_peers=False, formatter=formatter
    )
    assert call_log == ["005930"]

    fake_redis.store.clear()  # simulate expiry

    res = await handlers._run_batch_analysis(
        ["005930"], market="kr", include_peers=False, formatter=formatter
    )
    assert call_log == ["005930", "005930"]
    assert res["results"]["005930"]["cache_hit"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_redis_unavailable_falls_through_to_pipeline(monkeypatch):
    """When _get_redis_client returns None, the pipeline must still run."""
    monkeypatch.setattr(
        analyze_cache, "_REDIS_CLIENT", None
    )

    async def _none():
        return None

    monkeypatch.setattr(analyze_cache, "_get_redis_client", _none)
    call_log = _install_fake_pipeline(
        monkeypatch, {"005930": _make_analysis("005930")}
    )
    formatter = _summary_formatter

    res = await handlers._run_batch_analysis(
        ["005930"], market="kr", include_peers=False, formatter=formatter
    )
    assert call_log == ["005930"]
    row = res["results"]["005930"]
    assert row["cache_hit"] is False
    assert row["derived_as_of"] is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_error_row_carries_cache_metadata(monkeypatch, patch_redis):
    patch_redis()
    _install_fake_pipeline(monkeypatch, {})

    formatter = _summary_formatter

    res = await handlers._run_batch_analysis(
        ["BADSYM"], market="kr", include_peers=False, formatter=formatter
    )
    row = res["results"]["BADSYM"]
    assert "error" in row
    assert row["cache_hit"] is False
    assert row["derived_as_of"] is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_redis_set_called_with_market_specific_ttl(monkeypatch, patch_redis):
    fake_redis = patch_redis()
    _install_fake_pipeline(
        monkeypatch, {"KRW-BTC": _make_analysis("KRW-BTC")}
    )
    formatter = _summary_formatter

    await handlers._run_batch_analysis(
        ["KRW-BTC"], market="crypto", include_peers=False, formatter=formatter
    )
    key = analyze_cache._cache_key(
        "crypto", "KRW-BTC", analyze_cache._kst_date_for_key()
    )
    assert fake_redis.ttls[key] == 3600
