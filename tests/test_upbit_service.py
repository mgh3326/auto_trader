import asyncio

import pytest

from app.services import upbit


@pytest.fixture(autouse=True)
def reset_upbit_ticker_cache_state():
    for attr_name in ("_ticker_price_cache", "_ticker_inflight_symbol_tasks"):
        state = getattr(upbit, attr_name, None)
        if isinstance(state, dict):
            state.clear()


@pytest.mark.asyncio
async def test_fetch_multiple_tickers_keeps_comma_unescaped(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_request_json(url: str, params=None):
        captured["url"] = url
        captured["params"] = params
        return []

    monkeypatch.setattr(upbit, "_request_json", fake_request_json)

    await upbit.fetch_multiple_tickers(["KRW-BTC", "KRW-ETH", "KRW-XRP"])

    assert captured["params"] is None
    assert "markets=KRW-BTC,KRW-ETH,KRW-XRP" in str(captured["url"])
    assert "%2C" not in str(captured["url"])


def test_get_upbit_rate_limit_candles_wildcard_is_fixed():
    rate, period = upbit._get_upbit_rate_limit(upbit.UPBIT_CANDLES_RATE_LIMIT_KEY)
    assert rate == 10
    assert period == 1.0


@pytest.mark.asyncio
async def test_request_json_uses_candles_wildcard_limiter_key(monkeypatch):
    captured: dict[str, object] = {}

    class DummyLimiter:
        async def acquire(self, blocking_callback=None):
            return None

    async def fake_get_limiter(
        provider: str,
        api_key: str,
        rate: int,
        period: float,
    ):
        captured["provider"] = provider
        captured["api_key"] = api_key
        captured["rate"] = rate
        captured["period"] = period
        return DummyLimiter()

    class DummyResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return []

    class DummyAsyncClient:
        def __init__(self, timeout: int):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str, params=None):
            captured["url"] = url
            captured["params"] = params
            return DummyResponse()

    monkeypatch.setattr(upbit, "get_limiter", fake_get_limiter)
    monkeypatch.setattr(upbit.httpx, "AsyncClient", DummyAsyncClient)

    await upbit._request_json(
        f"{upbit.UPBIT_REST}/candles/days",
        params={"market": "KRW-BTC", "count": 1},
    )

    assert captured["provider"] == "upbit"
    assert captured["api_key"] == upbit.UPBIT_CANDLES_RATE_LIMIT_KEY
    assert captured["rate"] == 10
    assert captured["period"] == 1.0


@pytest.mark.asyncio
async def test_fetch_multiple_current_prices_cache_hit_within_ttl(monkeypatch):
    raw_call_count = 0

    async def fake_fetch_multiple_tickers(market_codes: list[str]) -> list[dict]:
        nonlocal raw_call_count
        raw_call_count += 1
        return [
            {"market": market_code, "trade_price": 100.0 + raw_call_count}
            for market_code in market_codes
        ]

    monkeypatch.setattr(upbit, "fetch_multiple_tickers", fake_fetch_multiple_tickers)

    first = await upbit.fetch_multiple_current_prices(["KRW-BTC"])
    second = await upbit.fetch_multiple_current_prices(["KRW-BTC"])

    assert first == second
    assert raw_call_count == 1


@pytest.mark.asyncio
async def test_fetch_multiple_current_prices_partial_cache_hit(monkeypatch):
    requested_batches: list[list[str]] = []

    async def fake_fetch_multiple_tickers(market_codes: list[str]) -> list[dict]:
        requested_batches.append(list(market_codes))
        return [
            {"market": market_code, "trade_price": float(len(requested_batches))}
            for market_code in market_codes
        ]

    monkeypatch.setattr(upbit, "fetch_multiple_tickers", fake_fetch_multiple_tickers)

    await upbit.fetch_multiple_current_prices(["KRW-BTC"])
    result = await upbit.fetch_multiple_current_prices(["KRW-BTC", "KRW-ETH"])

    assert requested_batches == [["KRW-BTC"], ["KRW-ETH"]]
    assert set(result.keys()) == {"KRW-BTC", "KRW-ETH"}


@pytest.mark.asyncio
async def test_fetch_multiple_current_prices_inflight_dedupe(monkeypatch):
    raw_call_count = 0

    async def fake_fetch_multiple_tickers(market_codes: list[str]) -> list[dict]:
        nonlocal raw_call_count
        raw_call_count += 1
        await asyncio.sleep(0.05)
        return [
            {"market": market_code, "trade_price": 777.0}
            for market_code in market_codes
        ]

    monkeypatch.setattr(upbit, "fetch_multiple_tickers", fake_fetch_multiple_tickers)

    first, second, third = await asyncio.gather(
        upbit.fetch_multiple_current_prices(["KRW-BTC"]),
        upbit.fetch_multiple_current_prices(["KRW-BTC"]),
        upbit.fetch_multiple_current_prices(["KRW-BTC"]),
    )

    assert first == second == third == {"KRW-BTC": 777.0}
    assert raw_call_count == 1


@pytest.mark.asyncio
async def test_fetch_multiple_current_prices_inflight_dedupe_for_overlapping_batches(
    monkeypatch,
):
    requested_batches: list[list[str]] = []

    async def fake_fetch_multiple_tickers(market_codes: list[str]) -> list[dict]:
        requested_batches.append(list(market_codes))
        await asyncio.sleep(0.05)
        return [
            {"market": market_code, "trade_price": 123.0}
            for market_code in market_codes
        ]

    monkeypatch.setattr(upbit, "fetch_multiple_tickers", fake_fetch_multiple_tickers)

    full_batch, overlap_batch = await asyncio.gather(
        upbit.fetch_multiple_current_prices(["KRW-BTC", "KRW-ETH"]),
        upbit.fetch_multiple_current_prices(["KRW-BTC"]),
    )

    assert full_batch == {"KRW-BTC": 123.0, "KRW-ETH": 123.0}
    assert overlap_batch == {"KRW-BTC": 123.0}
    assert len(requested_batches) == 1
    assert requested_batches[0] == ["KRW-BTC", "KRW-ETH"]


@pytest.mark.asyncio
async def test_fetch_multiple_current_prices_bypass_cache(monkeypatch):
    raw_call_count = 0

    async def fake_fetch_multiple_tickers(market_codes: list[str]) -> list[dict]:
        nonlocal raw_call_count
        raw_call_count += 1
        return [
            {"market": market_code, "trade_price": 500.0 + raw_call_count}
            for market_code in market_codes
        ]

    monkeypatch.setattr(upbit, "fetch_multiple_tickers", fake_fetch_multiple_tickers)

    first = await upbit.fetch_multiple_current_prices(["KRW-BTC"])
    second = await upbit.fetch_multiple_current_prices(["KRW-BTC"], use_cache=False)

    assert first != second
    assert raw_call_count == 2
