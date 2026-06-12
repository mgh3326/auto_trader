from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.client import TossReadClient


class _TokenManager(TossOAuthTokenManager):
    def __init__(self) -> None:
        pass

    async def get_access_token(self, *, force_reissue: bool = False) -> str:
        del force_reissue
        return "token-1"


def _json(payload):
    return {"result": payload}


@pytest.mark.asyncio
async def test_prices_sends_comma_symbols_and_authorization() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["symbols"] = request.url.params["symbols"]
        return httpx.Response(
            200,
            json=_json(
                [
                    {
                        "symbol": "AAPL",
                        "timestamp": "2026-06-12T00:00:00Z",
                        "lastPrice": "190.12",
                        "currency": "USD",
                    }
                ]
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
    )
    try:
        prices = await client.prices(["AAPL", "BRK.B"])
    finally:
        await client.aclose()

    assert seen == {"authorization": "Bearer token-1", "symbols": "AAPL,BRK.B"}
    assert prices[0].last_price == Decimal("190.12")


@pytest.mark.asyncio
async def test_holdings_auto_resolves_single_account_header() -> None:
    seen_headers = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(
                200,
                json=_json(
                    [
                        {
                            "accountNo": "12345678",
                            "accountSeq": 1,
                            "accountType": "BROKERAGE",
                        }
                    ]
                ),
                request=request,
            )
        seen_headers.append(request.headers["X-Tossinvest-Account"])
        return httpx.Response(200, json=_json({"items": []}), request=request)

    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
    )
    try:
        holdings = await client.holdings()
    finally:
        await client.aclose()

    assert seen_headers == ["1"]
    assert holdings.items == []


@pytest.mark.asyncio
async def test_prices_rejects_more_than_200_symbols() -> None:
    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        ),
    )
    try:
        with pytest.raises(ValueError, match="1..200"):
            await client.prices([f"S{i}" for i in range(201)])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_order_retries_once_after_invalid_token() -> None:
    calls = 0
    token_calls: list[bool] = []

    class TokenManager(_TokenManager):
        async def get_access_token(self, *, force_reissue: bool = False) -> str:
            token_calls.append(force_reissue)
            return "token-2" if force_reissue else "token-1"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "requestId": "req",
                        "code": "invalid-token",
                        "message": "",
                        "data": None,
                    }
                },
                request=request,
            )
        return httpx.Response(
            200,
            json=_json(
                {
                    "orderId": "ord-1",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "FILLED",
                    "price": "190",
                    "quantity": "1",
                    "orderAmount": None,
                    "currency": "USD",
                    "orderedAt": "2026-06-12T00:00:00Z",
                    "canceledAt": None,
                    "execution": {"filledQuantity": "1"},
                }
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=TokenManager(),
        account_seq=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        order = await client.get_order("ord-1")
    finally:
        await client.aclose()

    assert order.order_id == "ord-1"
    assert token_calls == [False, True]


@pytest.mark.asyncio
async def test_prices_retries_once_after_429_retry_after(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "requestId": "rate-1",
                        "code": "too-many-requests",
                        "message": "slow down",
                        "data": {"retryAfterSeconds": "2"},
                    }
                },
                headers={"Retry-After": "2"},
                request=request,
            )
        return httpx.Response(
            200,
            json=_json(
                [
                    {
                        "symbol": "AAPL",
                        "timestamp": "2026-06-12T00:00:00Z",
                        "lastPrice": "190.12",
                        "currency": "USD",
                    }
                ]
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
    )
    try:
        prices = await client.prices(["AAPL"])
    finally:
        await client.aclose()

    assert calls == 2
    assert sleeps == [2.0]
    assert prices[0].last_price == Decimal("190.12")


@pytest.mark.asyncio
async def test_candles_returns_typed_page_and_sends_query_params() -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["symbol"] = request.url.params["symbol"]
        seen["interval"] = request.url.params["interval"]
        seen["count"] = request.url.params["count"]
        seen["adjusted"] = request.url.params["adjusted"]
        return httpx.Response(
            200,
            json=_json(
                {
                    "candles": [
                        {
                            "timestamp": "2026-06-12T00:00:00.000+09:00",
                            "openPrice": "313000",
                            "highPrice": "330000",
                            "lowPrice": "313000",
                            "closePrice": "326000",
                            "volume": "11414585",
                            "currency": "KRW",
                        }
                    ],
                    "nextBefore": "2026-06-11T00:00:00.000+09:00",
                }
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
    )
    try:
        page = await client.candles("005930", interval="1d", count=1, adjusted=True)
    finally:
        await client.aclose()

    assert seen == {
        "symbol": "005930",
        "interval": "1d",
        "count": "1",
        "adjusted": "true",
    }
    assert page.next_before == "2026-06-11T00:00:00.000+09:00"
    assert page.candles[0].close_price == Decimal("326000")
