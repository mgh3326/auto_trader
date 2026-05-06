import asyncio
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

import app.services.brokers.upbit.client as upbit
from app.services import upbit_symbol_universe_service


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
    assert period == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("api_key", "expected_rate", "expected_period"),
    [
        ("GET /v1/accounts", 30, 1.0),
        ("GET /v1/ticker", 10, 1.0),
    ],
)
def test_get_upbit_rate_limit_for_seeded_keys(
    api_key: str,
    expected_rate: int,
    expected_period: float,
):
    rate, period = upbit._get_upbit_rate_limit(api_key)

    assert rate == expected_rate
    assert period == expected_period


@pytest.mark.asyncio
async def test_request_json_uses_candles_wildcard_limiter_key(monkeypatch):
    captured: dict[str, object] = {}

    class DummyLimiter:
        async def acquire(self, blocking_callback=None):
            _ = blocking_callback
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
    assert captured["period"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_fetch_multiple_current_prices_cache_hit_within_ttl(monkeypatch):
    raw_call_count = 0

    async def fake_fetch_multiple_tickers(
        market_codes: list[str],
    ) -> list[dict[str, object]]:
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

    async def fake_fetch_multiple_tickers(
        market_codes: list[str],
    ) -> list[dict[str, object]]:
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

    async def fake_fetch_multiple_tickers(
        market_codes: list[str],
    ) -> list[dict[str, object]]:
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

    async def fake_fetch_multiple_tickers(
        market_codes: list[str],
    ) -> list[dict[str, object]]:
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

    assert full_batch == pytest.approx({"KRW-BTC": 123.0, "KRW-ETH": 123.0})
    assert overlap_batch == pytest.approx({"KRW-BTC": 123.0})
    assert len(requested_batches) == 1
    assert requested_batches[0] == ["KRW-BTC", "KRW-ETH"]


@pytest.mark.asyncio
async def test_fetch_multiple_current_prices_bypass_cache(monkeypatch):
    raw_call_count = 0

    async def fake_fetch_multiple_tickers(
        market_codes: list[str],
    ) -> list[dict[str, object]]:
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


@pytest.mark.asyncio
async def test_fetch_top_traded_coins_uses_db_universe_markets(monkeypatch):
    get_active_markets = AsyncMock(return_value=["KRW-BTC", "KRW-ETH"])
    fetch_tickers = AsyncMock(
        return_value=[
            {"market": "KRW-ETH", "acc_trade_price_24h": 10.0},
            {"market": "KRW-BTC", "acc_trade_price_24h": 30.0},
        ]
    )

    monkeypatch.setattr(upbit, "get_active_upbit_markets", get_active_markets)
    monkeypatch.setattr(upbit, "fetch_multiple_tickers", fetch_tickers)

    result = await upbit.fetch_top_traded_coins("KRW")

    get_active_markets.assert_awaited_once_with(quote_currency="KRW")
    fetch_tickers.assert_awaited_once_with(["KRW-BTC", "KRW-ETH"])
    assert [item["market"] for item in result] == ["KRW-BTC", "KRW-ETH"]


@pytest.mark.asyncio
async def test_fetch_top_traded_coins_fail_fast_when_universe_missing(monkeypatch):
    get_active_markets = AsyncMock(
        side_effect=upbit_symbol_universe_service.UpbitSymbolUniverseEmptyError(
            "upbit_symbol_universe is empty"
        )
    )
    fetch_tickers = AsyncMock(return_value=[])

    monkeypatch.setattr(upbit, "get_active_upbit_markets", get_active_markets)
    monkeypatch.setattr(upbit, "fetch_multiple_tickers", fetch_tickers)

    with pytest.raises(upbit_symbol_universe_service.UpbitSymbolUniverseEmptyError):
        await upbit.fetch_top_traded_coins("KRW")

    fetch_tickers.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_multiple_tickers_batches_large_requests(monkeypatch):
    requested_batches: list[list[str]] = []
    normalized_codes = [f"KRW-COIN{index:03d}" for index in range(120)]
    market_codes = normalized_codes + [normalized_codes[0], normalized_codes[1]]

    async def fake_request_json(url: str, params=None):
        assert params is None
        batch_codes = parse_qs(urlparse(url).query)["markets"][0].split(",")
        requested_batches.append(batch_codes)
        return [
            {
                "market": market_code,
                "trade_price": float(index),
                "acc_trade_price_24h": float(index),
            }
            for index, market_code in enumerate(batch_codes)
        ]

    monkeypatch.setattr(upbit, "_request_json", fake_request_json)

    result = await upbit.fetch_multiple_tickers(market_codes)

    assert [len(batch) for batch in requested_batches] == [50, 50, 20]
    assert requested_batches[0] == normalized_codes[:50]
    assert requested_batches[1] == normalized_codes[50:100]
    assert requested_batches[2] == normalized_codes[100:120]
    assert [item["market"] for item in result] == normalized_codes


@pytest.mark.asyncio
async def test_fetch_top_traded_coins_sorts_across_batched_ticker_results(monkeypatch):
    requested_batches: list[list[str]] = []
    active_markets = [f"KRW-COIN{index:03d}" for index in range(119, -1, -1)]

    async def fake_request_json(url: str, params=None):
        assert params is None
        batch_codes = parse_qs(urlparse(url).query)["markets"][0].split(",")
        requested_batches.append(batch_codes)
        return [
            {
                "market": market_code,
                "acc_trade_price_24h": float(market_code.removeprefix("KRW-COIN")),
            }
            for market_code in batch_codes
        ]

    get_active_markets = AsyncMock(return_value=active_markets)

    monkeypatch.setattr(upbit, "get_active_upbit_markets", get_active_markets)
    monkeypatch.setattr(upbit, "_request_json", fake_request_json)

    result = await upbit.fetch_top_traded_coins("KRW")

    get_active_markets.assert_awaited_once_with(quote_currency="KRW")
    assert [len(batch) for batch in requested_batches] == [50, 50, 20]
    assert len(result) == 120
    assert result[0]["market"] == "KRW-COIN119"
    assert result[-1]["market"] == "KRW-COIN000"


@pytest.mark.asyncio
async def test_fetch_multiple_tickers_propagates_later_batch_errors(monkeypatch):
    requested_batches: list[list[str]] = []
    market_codes = [f"KRW-COIN{index:03d}" for index in range(120)]

    async def fake_request_json(url: str, params=None):
        assert params is None
        batch_codes = parse_qs(urlparse(url).query)["markets"][0].split(",")
        requested_batches.append(batch_codes)
        if len(requested_batches) == 2:
            raise RuntimeError("second batch failed")
        return [{"market": market_code} for market_code in batch_codes]

    monkeypatch.setattr(upbit, "_request_json", fake_request_json)

    with pytest.raises(RuntimeError, match="second batch failed"):
        await upbit.fetch_multiple_tickers(market_codes)

    assert [len(batch) for batch in requested_batches] == [50, 50]
