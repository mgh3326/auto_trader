from __future__ import annotations

import httpx
import pytest

from app.services.brokers.upbit import public_trades


@pytest.mark.asyncio
async def test_fetch_recent_trades_returns_rows(monkeypatch):
    sample = [{"market": "KRW-BTC", "trade_price": 1.0}]

    async def fake_request_json(url, params=None):
        assert "trades/ticks" in url
        assert params == {"market": "KRW-BTC", "count": 50}
        return sample

    monkeypatch.setattr(public_trades, "_request_json", fake_request_json)
    assert await public_trades.fetch_recent_trades("KRW-BTC", count=50) == sample


@pytest.mark.asyncio
async def test_fetch_recent_trades_normalizes_market_and_bounds_count(monkeypatch):
    captured = {}

    async def fake_request_json(url, params=None):
        captured["params"] = params
        return []

    async def fake_market_by_coin(symbol):
        assert symbol == "BTC"
        return "KRW-BTC"

    monkeypatch.setattr(public_trades, "_request_json", fake_request_json)
    monkeypatch.setattr(public_trades, "get_upbit_market_by_coin", fake_market_by_coin)
    await public_trades.fetch_recent_trades("btc", count=10_000)
    assert captured["params"] == {"market": "KRW-BTC", "count": 500}


@pytest.mark.asyncio
async def test_fetch_recent_trades_propagates_http_error(monkeypatch):
    async def fake_request_json(url, params=None):
        raise httpx.HTTPStatusError(
            "boom", request=httpx.Request("GET", url), response=httpx.Response(429)
        )

    monkeypatch.setattr(public_trades, "_request_json", fake_request_json)
    with pytest.raises(httpx.HTTPStatusError):
        await public_trades.fetch_recent_trades("KRW-BTC")
