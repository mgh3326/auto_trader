import datetime as dt
from decimal import Decimal

import httpx
import pytest

from app.services.external.crypto_insights import (
    CryptoInsightProviderResult,
    fetch_alternative_me_fear_greed,
    fetch_binance_funding_rates,
    fetch_binance_settlement_funding_prints,
    fetch_coingecko_global,
    fetch_coinglass_open_interest_poc,
    fetch_defillama_reference,
    fetch_tokenomist_unlocks_poc,
    fetch_tradingview_crypto_breadth_reference,
    fetch_upbit_binance_kimchi_premium,
)

pytestmark = pytest.mark.asyncio


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_alternative_me_adapter_maps_fear_greed_metric():
    def handler(request):
        assert request.url.host == "api.alternative.me"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "value": "72",
                        "value_classification": "Greed",
                        "timestamp": "1778620000",
                        "time_until_update": "3600",
                    },
                    {
                        "value": "65",
                        "value_classification": "Greed",
                        "timestamp": "1778533600",
                    },
                ]
            },
        )

    async with _client(handler) as client:
        result = await fetch_alternative_me_fear_greed(
            client, now=dt.datetime(2026, 5, 13, tzinfo=dt.UTC)
        )

    assert result.warnings == ()
    assert len(result.metrics) == 1
    metric = result.metrics[0]
    assert metric.metric == "fear_greed"
    assert metric.provider == "alternative_me"
    assert metric.value == Decimal("72")
    assert metric.label == "Greed"
    expected_age = int(
        (
            dt.datetime(2026, 5, 13, tzinfo=dt.UTC)
            - dt.datetime.fromtimestamp(1778620000, tz=dt.UTC)
        ).total_seconds()
    )
    assert metric.freshness_seconds == expected_age + 3600


async def test_coingecko_adapter_maps_global_metrics():
    def handler(request):
        assert request.url.host == "api.coingecko.com"
        return httpx.Response(
            200,
            json={
                "data": {
                    "market_cap_percentage": {"btc": 58.12},
                    "market_cap_change_percentage_24h_usd": -1.7,
                }
            },
        )

    async with _client(handler) as client:
        result = await fetch_coingecko_global(client)

    assert {metric.metric for metric in result.metrics} == {
        "btc_dominance",
        "global_market_cap_change_24h",
    }


async def test_binance_adapter_maps_public_funding_rate():
    def handler(request):
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "lastFundingRate": "0.0001",
                "time": 1778620000000,
            },
        )

    async with _client(handler) as client:
        result = await fetch_binance_funding_rates(["BTCUSDT"], client)

    assert result.warnings == ()
    assert result.metrics[0].metric == "funding_rate"
    assert result.metrics[0].label == "longs pay shorts"


async def test_defillama_adapter_maps_tvl_and_stablecoin_metrics():
    def handler(request):
        if request.url.host == "api.llama.fi":
            assert request.url.path == "/v2/chains"
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Ethereum",
                        "tokenSymbol": "ETH",
                        "gecko_id": "ethereum",
                        "tvl": 123,
                    },
                    {
                        "name": "Solana",
                        "tokenSymbol": "SOL",
                        "gecko_id": "solana",
                        "tvl": 789,
                    },
                ],
            )
        return httpx.Response(
            200,
            json={
                "chains": [
                    {"name": "Ethereum", "totalCirculatingUSD": {"peggedUSD": 456}},
                    {"name": "Solana", "totalCirculatingUSD": {"peggedUSD": 44}},
                ]
            },
        )

    async with _client(handler) as client:
        result = await fetch_defillama_reference(client, protocol_slugs=["ethereum"])

    metrics = {metric.metric: metric for metric in result.metrics}
    assert set(metrics) == {"tvl", "stablecoin_supply"}
    assert metrics["tvl"].symbol == "ETH"
    assert metrics["stablecoin_supply"].value == Decimal("500")


async def test_optional_poc_adapters_are_non_fatal_without_credentials():
    assert (await fetch_coinglass_open_interest_poc()).warnings == (
        "coinglass: disabled (missing API key)",
    )
    assert (await fetch_tokenomist_unlocks_poc()).warnings == (
        "tokenomist: disabled (missing API key)",
    )
    tv_result = await fetch_tradingview_crypto_breadth_reference()
    assert isinstance(tv_result, CryptoInsightProviderResult)
    assert tv_result.metrics[0].provider == "tradingview"
    assert tv_result.metrics[0].raw_payload["replace_tvscreener"] is False


# ---- T3 regime-gate inputs (hk 5889): kimchi premium + settlement funding prints ----

_NOW = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)


def _kimchi_handler(
    *,
    upbit=None,
    binance=None,
    fx=None,
):
    """Route the three kimchi legs; a None leg is served as a 500 (unreadable)."""

    def handler(request):
        host = request.url.host
        if host == "api.upbit.com":
            assert request.url.params["markets"] == "KRW-BTC"
            body = upbit if upbit is not None else None
        elif host == "api.binance.com":
            assert request.url.params["symbol"] == "BTCUSDT"
            body = binance
        elif host == "open.er-api.com":
            body = fx
        else:  # pragma: no cover - the test must not reach any other host
            raise AssertionError(host)
        if body is None:
            return httpx.Response(500, json={"error": "unavailable"})
        return httpx.Response(200, json=body)

    return handler


async def test_kimchi_premium_is_upbit_over_binance_at_usd_krw():
    handler = _kimchi_handler(
        upbit=[{"market": "KRW-BTC", "trade_price": 80000000, "trade_timestamp": 1}],
        binance={"symbol": "BTCUSDT", "price": "60000"},
        fx={"result": "success", "rates": {"KRW": 1300}},
    )
    async with _client(handler) as client:
        result = await fetch_upbit_binance_kimchi_premium(client, now=_NOW)

    assert result.warnings == ()
    assert len(result.metrics) == 1
    metric = result.metrics[0]
    assert metric.metric == "kimchi_premium"
    assert metric.provider == "upbit_binance"
    assert metric.symbol == "BTC"
    assert metric.unit == "%"
    # (80,000,000 - 60,000 * 1,300) / (60,000 * 1,300) * 100 = 2.5641025...
    assert metric.value == Decimal("2.5641")
    assert metric.observed_at == _NOW
    assert metric.freshness_seconds == 0
    assert metric.raw_payload["usd_krw"] == "1300"


async def test_kimchi_premium_missing_fx_is_unknown_not_zero():
    handler = _kimchi_handler(
        upbit=[{"market": "KRW-BTC", "trade_price": 80000000}],
        binance={"symbol": "BTCUSDT", "price": "60000"},
        fx={"result": "success", "rates": {}},
    )
    async with _client(handler) as client:
        result = await fetch_upbit_binance_kimchi_premium(client, now=_NOW)

    assert result.metrics == ()
    assert result.warnings == ("upbit_binance: usd/krw rate unreadable",)


async def test_kimchi_premium_one_unreadable_leg_writes_nothing():
    # Upbit leg is a 500: no partial premium from the two remaining legs.
    handler = _kimchi_handler(
        upbit=None,
        binance={"symbol": "BTCUSDT", "price": "60000"},
        fx={"rates": {"KRW": 1300}},
    )
    async with _client(handler) as client:
        result = await fetch_upbit_binance_kimchi_premium(client, now=_NOW)

    assert result.metrics == ()
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("upbit_binance: ")


@pytest.mark.parametrize(
    "upbit_body",
    [
        [],  # empty ticker
        [{"market": "KRW-ETH", "trade_price": 1}],  # wrong market
        [{"market": "KRW-BTC", "trade_price": "nan"}],  # non-finite
        [{"market": "KRW-BTC", "trade_price": 0}],  # non-positive
        [{"market": "KRW-BTC", "trade_price": True}],  # bool is not a price
    ],
)
async def test_kimchi_premium_rejects_malformed_upbit_ticker(upbit_body):
    handler = _kimchi_handler(
        upbit=upbit_body,
        binance={"symbol": "BTCUSDT", "price": "60000"},
        fx={"rates": {"KRW": 1300}},
    )
    async with _client(handler) as client:
        result = await fetch_upbit_binance_kimchi_premium(client, now=_NOW)

    assert result.metrics == ()
    assert result.warnings and result.warnings[0].startswith("upbit_binance: ")


def _funding_row(settle_ms: int, rate: str) -> dict:
    return {
        "symbol": "BTCUSDT",
        "fundingTime": settle_ms,
        "fundingRate": rate,
        "markPrice": "61830.0",
        "rateType": "Regular",
    }


_T0 = 1795161600000  # a settlement time (ms); 8-hour spacing below
_PRINTS = [
    _funding_row(_T0, "0.00000706"),
    _funding_row(_T0 + 8 * 3600 * 1000, "0.00002551"),
    _funding_row(_T0 + 16 * 3600 * 1000, "-0.00006069"),
]


async def test_binance_settlement_prints_one_row_per_settlement_time():
    seen: dict = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_PRINTS)

    async with _client(handler) as client:
        result = await fetch_binance_settlement_funding_prints(client, now=_NOW)

    assert seen["params"] == {"symbol": "BTCUSDT", "limit": "3"}
    assert result.warnings == ()
    assert [m.metric for m in result.metrics] == ["funding_settlement_rate"] * 3
    assert [m.observed_at for m in result.metrics] == [
        dt.datetime.fromtimestamp(_T0 / 1000, tz=dt.UTC),
        dt.datetime.fromtimestamp((_T0 + 8 * 3600 * 1000) / 1000, tz=dt.UTC),
        dt.datetime.fromtimestamp((_T0 + 16 * 3600 * 1000) / 1000, tz=dt.UTC),
    ]
    assert [m.value for m in result.metrics] == [
        Decimal("0.00000706"),
        Decimal("0.00002551"),
        Decimal("-0.00006069"),
    ]
    assert [m.label for m in result.metrics] == [
        "longs pay shorts",
        "longs pay shorts",
        "shorts pay longs",
    ]
    assert all(m.provider == "binance_settlement" for m in result.metrics)
    assert all(m.raw_payload["observed_at"] == _NOW.isoformat() for m in result.metrics)


@pytest.mark.parametrize(
    "body",
    [
        _PRINTS[:2],  # only two prints: a 3-print average would be wrong
        _PRINTS + [_funding_row(_T0 + 24 * 3600 * 1000, "0.0001")],  # four
        [_PRINTS[0], _PRINTS[0], _PRINTS[1]],  # duplicate settlement time
        [_PRINTS[0], _PRINTS[1], {**_PRINTS[2], "fundingRate": "nan"}],
        [_PRINTS[0], _PRINTS[1], {**_PRINTS[2], "fundingTime": "1795"}],
        [_PRINTS[0], _PRINTS[1], {**_PRINTS[2], "symbol": "ETHUSDT"}],
        {"code": -1121, "msg": "Invalid symbol."},  # error object, not a list
        [],
    ],
)
async def test_binance_settlement_is_all_or_nothing(body):
    async with _client(lambda request: httpx.Response(200, json=body)) as client:
        result = await fetch_binance_settlement_funding_prints(client, now=_NOW)

    assert result.metrics == ()
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("binance_settlement:BTCUSDT: ")


async def test_binance_settlement_http_failure_is_a_warning_not_a_row():
    async with _client(lambda request: httpx.Response(503)) as client:
        result = await fetch_binance_settlement_funding_prints(client, now=_NOW)

    assert result.metrics == ()
    assert result.warnings and result.warnings[0].startswith("binance_settlement:")
