from __future__ import annotations

import pytest

from app.services.naver_finance.consensus import fetch_analyst_consensus

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_fetch_analyst_consensus_ok(httpx_mock):
    httpx_mock.add_response(
        url="https://m.stock.naver.com/api/stock/005930/integration",
        json={
            "consensusInfo": {
                "recommMean": 4.5,
                "priceTargetMean": 95000,
            }
        },
    )

    res = await fetch_analyst_consensus("005930")
    assert res["recomm_mean"] == 4.5
    assert res["price_target_mean"] == 95000
    assert not res["warnings"]


@pytest.mark.asyncio
async def test_fetch_analyst_consensus_fail_open(httpx_mock):
    httpx_mock.add_response(
        url="https://m.stock.naver.com/api/stock/005930/integration",
        json={
            "consensusInfo": {
                "recommMean": 3.8,
                # priceTargetMean is missing
            }
        },
    )

    res = await fetch_analyst_consensus("005930")
    assert res["recomm_mean"] == 3.8
    assert res["price_target_mean"] is None
    assert len(res["warnings"]) == 1
    assert "priceTargetMean" in res["warnings"][0]


@pytest.mark.asyncio
async def test_fetch_analyst_consensus_invalid_key_envelope(httpx_mock):
    httpx_mock.add_response(
        url="https://m.stock.naver.com/api/stock/005930/integration",
        json={
            # consensusInfo is completely missing or is null
            "consensusInfo": None
        },
    )

    res = await fetch_analyst_consensus("005930")
    assert res["recomm_mean"] is None
    assert res["price_target_mean"] is None
    assert (
        len(res["warnings"]) == 3
    )  # consensusInfo, recommMean, priceTargetMean warnings


@pytest.mark.asyncio
async def test_fetch_analyst_consensus_validation():
    with pytest.raises(ValueError, match="Invalid Korean stock code"):
        # Not KR code (non-digit)
        await fetch_analyst_consensus("AAPL")

    with pytest.raises(ValueError, match="Invalid Korean stock code"):
        # Length is wrong
        await fetch_analyst_consensus("12345")
