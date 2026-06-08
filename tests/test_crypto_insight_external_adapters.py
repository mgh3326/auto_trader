import datetime as dt
from decimal import Decimal

import httpx
import pytest

from app.services.external.crypto_insights import (
    CryptoInsightProviderResult,
    fetch_alternative_me_fear_greed,
    fetch_binance_funding_rates,
    fetch_coingecko_global,
    fetch_coinglass_open_interest_poc,
    fetch_defillama_reference,
    fetch_tokenomist_unlocks_poc,
    fetch_tradingview_crypto_breadth_reference,
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
