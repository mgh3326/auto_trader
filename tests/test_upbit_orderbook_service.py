from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import upbit_orderbook


class DummyResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self._url = "https://api.upbit.com/v1/orderbook"

    def bind_url(self, url: str) -> None:
        self._url = url

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return

        request = httpx.Request("GET", self._url)
        response = httpx.Response(
            self.status_code,
            request=request,
            json=self._payload if isinstance(self._payload, (dict, list)) else None,
        )
        raise httpx.HTTPStatusError(
            f"HTTP {self.status_code}", request=request, response=response
        )

    def json(self):
        return self._payload


class DummyAsyncClient:
    def __init__(self, responses: list[DummyResponse], calls: list[str], timeout: int):
        self._responses = responses
        self._calls = calls
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str):
        self._calls.append(url)
        response = self._responses.pop(0)
        response.bind_url(url)
        return response


@pytest.mark.asyncio
async def test_fetch_orderbook_normalizes_symbol(monkeypatch):
    calls: list[str] = []
    responses = [
        DummyResponse(
            200,
            [
                {
                    "market": "KRW-BTC",
                    "timestamp": 123,
                    "total_ask_size": 1.0,
                    "total_bid_size": 2.0,
                    "orderbook_units": [],
                }
            ],
        )
    ]

    monkeypatch.setattr(
        upbit_orderbook,
        "upbit_pairs",
        SimpleNamespace(
            prime_upbit_constants=AsyncMock(),
            COIN_TO_PAIR={"BTC": "KRW-BTC"},
        ),
    )
    monkeypatch.setattr(
        upbit_orderbook.httpx,
        "AsyncClient",
        lambda timeout: DummyAsyncClient(responses, calls, timeout),
    )

    result = await upbit_orderbook.fetch_orderbook("btc")

    assert result["market"] == "KRW-BTC"
    assert calls == ["https://api.upbit.com/v1/orderbook?markets=KRW-BTC"]


@pytest.mark.asyncio
async def test_fetch_multiple_orderbooks_batches_and_deduplicates(monkeypatch):
    calls: list[str] = []
    responses = [
        DummyResponse(
            200,
            [
                {"market": "KRW-BTC", "orderbook_units": []},
                {"market": "KRW-ETH", "orderbook_units": []},
            ],
        ),
        DummyResponse(
            200,
            [
                {"market": "KRW-XRP", "orderbook_units": []},
            ],
        ),
    ]

    monkeypatch.setattr(
        upbit_orderbook,
        "upbit_pairs",
        SimpleNamespace(
            prime_upbit_constants=AsyncMock(),
            COIN_TO_PAIR={"BTC": "KRW-BTC", "ETH": "KRW-ETH", "XRP": "KRW-XRP"},
        ),
    )
    monkeypatch.setattr(upbit_orderbook, "MAX_MARKETS_PER_REQUEST", 2)
    monkeypatch.setattr(
        upbit_orderbook.httpx,
        "AsyncClient",
        lambda timeout: DummyAsyncClient(responses, calls, timeout),
    )

    result = await upbit_orderbook.fetch_multiple_orderbooks(
        ["btc", "KRW-ETH", "xrp", "BTC", "NOT-A-MARKET"]
    )

    assert len(calls) == 2
    assert "markets=KRW-BTC,KRW-ETH" in calls[0]
    assert "markets=KRW-XRP" in calls[1]
    assert set(result.keys()) == {"KRW-BTC", "KRW-ETH", "KRW-XRP"}


@pytest.mark.asyncio
async def test_fetch_multiple_orderbooks_returns_partial_on_429(monkeypatch):
    calls: list[str] = []
    responses = [
        DummyResponse(
            200,
            [
                {"market": "KRW-BTC", "orderbook_units": []},
                {"market": "KRW-ETH", "orderbook_units": []},
            ],
        ),
        DummyResponse(429, []),
    ]

    monkeypatch.setattr(
        upbit_orderbook,
        "upbit_pairs",
        SimpleNamespace(
            prime_upbit_constants=AsyncMock(),
            COIN_TO_PAIR={"BTC": "KRW-BTC", "ETH": "KRW-ETH", "XRP": "KRW-XRP"},
        ),
    )
    monkeypatch.setattr(upbit_orderbook, "MAX_MARKETS_PER_REQUEST", 2)
    monkeypatch.setattr(
        upbit_orderbook.httpx,
        "AsyncClient",
        lambda timeout: DummyAsyncClient(responses, calls, timeout),
    )

    result = await upbit_orderbook.fetch_multiple_orderbooks(["BTC", "ETH", "XRP"])

    assert len(calls) == 2
    assert set(result.keys()) == {"KRW-BTC", "KRW-ETH"}
