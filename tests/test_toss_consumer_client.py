from __future__ import annotations

import pytest

from app.services.toss_consumer.client import TossConsumerClient, _to_product_code

pytestmark = [pytest.mark.unit]


def test_to_product_code_conversion():
    assert _to_product_code("005930") == "A005930"
    assert _to_product_code("A005930") == "A005930"
    with pytest.raises(ValueError, match="Invalid Korean equity symbol"):
        _to_product_code("AAPL")
    with pytest.raises(ValueError, match="Invalid Korean equity symbol"):
        _to_product_code("00593")  # too short
    with pytest.raises(ValueError, match="Invalid Korean equity symbol"):
        _to_product_code("A00593")  # too short A pattern


@pytest.mark.asyncio
async def test_fetch_buy_balance_flat_json(httpx_mock):
    client = TossConsumerClient()
    httpx_mock.add_response(
        url="https://wts-info-api.tossinvest.com/api/v1/stock-infos/trade/trend/trading-trend?productCode=A005930",
        json={
            "buyBalanceRate": 0.65,
            "sellBalanceRate": 0.35,
            "foreignerRatio": 0.52,
        },
    )

    res = await client.fetch_buy_balance("A005930")
    assert res["buyBalanceRate"] == 0.65
    assert res["sellBalanceRate"] == 0.35
    assert res["foreignerRatio"] == 0.52
    assert not res["warnings"]


@pytest.mark.asyncio
async def test_fetch_buy_balance_wrapped_result(httpx_mock):
    client = TossConsumerClient()
    httpx_mock.add_response(
        url="https://wts-info-api.tossinvest.com/api/v1/stock-infos/trade/trend/trading-trend?productCode=A005930",
        json={
            "result": {
                "buyBalanceRate": 0.70,
                "sellBalanceRate": 0.30,
                "foreignerRatio": 0.50,
            }
        },
    )

    res = await client.fetch_buy_balance("A005930")
    assert res["buyBalanceRate"] == 0.70
    assert res["sellBalanceRate"] == 0.30
    assert res["foreignerRatio"] == 0.50
    assert not res["warnings"]


@pytest.mark.asyncio
async def test_fetch_buy_balance_fail_open_keys(httpx_mock):
    client = TossConsumerClient()
    httpx_mock.add_response(
        url="https://wts-info-api.tossinvest.com/api/v1/stock-infos/trade/trend/trading-trend?productCode=A005930",
        json={
            # buyBalanceRate missing
            "sellBalanceRate": 0.30,
            "foreignerRatio": 0.50,
        },
    )

    res = await client.fetch_buy_balance("A005930")
    assert res["buyBalanceRate"] is None
    assert res["sellBalanceRate"] == 0.30
    assert res["foreignerRatio"] == 0.50
    assert len(res["warnings"]) == 1
    assert "buyBalanceRate" in res["warnings"][0]


@pytest.mark.asyncio
async def test_fetch_ai_signal_flat_json(httpx_mock):
    client = TossConsumerClient()
    httpx_mock.add_response(
        url="https://wts-info-api.tossinvest.com/api/v1/dashboard/wts/overview/ai-signals/detail?productCode=A005930&productType=STOCKS",
        json={
            "signalDirection": "BUY",
            "reasoning": "Strong earnings forecast",
            "relatedReasoning": "Consistent market leadership",
        },
    )

    res = await client.fetch_ai_signal("A005930")
    assert res["signalDirection"] == "BUY"
    assert res["reasoning"] == "Strong earnings forecast"
    assert res["relatedReasoning"] == "Consistent market leadership"
    assert not res["warnings"]


@pytest.mark.asyncio
async def test_fetch_ai_signal_wrapped_data(httpx_mock):
    client = TossConsumerClient()
    httpx_mock.add_response(
        url="https://wts-info-api.tossinvest.com/api/v1/dashboard/wts/overview/ai-signals/detail?productCode=A005930&productType=STOCKS",
        json={
            "data": {
                "signalDirection": "SELL",
                "reasoning": "Overbought condition",
                "relatedReasoning": "RSI indicator crossover",
            }
        },
    )

    res = await client.fetch_ai_signal("A005930")
    assert res["signalDirection"] == "SELL"
    assert res["reasoning"] == "Overbought condition"
    assert res["relatedReasoning"] == "RSI indicator crossover"
    assert not res["warnings"]


@pytest.mark.asyncio
async def test_fetch_ai_signal_fail_open_keys(httpx_mock):
    client = TossConsumerClient()
    httpx_mock.add_response(
        url="https://wts-info-api.tossinvest.com/api/v1/dashboard/wts/overview/ai-signals/detail?productCode=A005930&productType=STOCKS",
        json={
            # signalDirection and reasoning missing
            "relatedReasoning": "Technical breakout",
        },
    )

    res = await client.fetch_ai_signal("A005930")
    assert res["signalDirection"] is None
    assert res["reasoning"] is None
    assert res["relatedReasoning"] == "Technical breakout"
    assert len(res["warnings"]) == 2
    assert "signalDirection" in res["warnings"][0]
    assert "reasoning" in res["warnings"][1]
