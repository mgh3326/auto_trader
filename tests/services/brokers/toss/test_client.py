from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.services.brokers.toss import rate_limiter as rate_limiter_module
from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.client import TossReadClient


@dataclass
class _ClientSettings:
    toss_api_enabled: bool = True
    toss_api_client_id: str | None = "client-id"
    toss_api_client_secret: SecretStr | None = SecretStr("client-secret")
    toss_api_base_url: str | None = "https://openapi.tossinvest.com"
    toss_api_account_seq: int | None = 1


def test_from_settings_shares_process_global_rate_limiter() -> None:
    """ROB-547: client and its token manager must share the one process-global
    limiter so group TPS holds across concurrent call sites."""
    rate_limiter_module.reset_shared_rate_limiter()
    shared = rate_limiter_module.get_shared_rate_limiter()

    client = TossReadClient.from_settings(_ClientSettings())

    assert client._rate_limiter is shared
    assert client._token_manager._rate_limiter is shared


class _TokenManager(TossOAuthTokenManager):
    def __init__(self) -> None:
        pass

    async def get_access_token(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str:
        del force_reissue, failed_token
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
    failed_tokens: list[str | None] = []

    class TokenManager(_TokenManager):
        async def get_access_token(
            self, *, force_reissue: bool = False, failed_token: str | None = None
        ) -> str:
            token_calls.append(force_reissue)
            failed_tokens.append(failed_token)
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
    # ROB-547: the reissue must carry the failed token so a peer's fresher
    # token can be reused instead of force-churning a new one.
    assert failed_tokens == [None, "token-1"]


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


@pytest.mark.asyncio
async def test_place_order_posts_json_with_account_header_and_client_order_id() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json=_json(
                {
                    "orderId": "new-ord-123",
                    "clientOrderId": "abc123",
                }
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        account_seq=999,
        transport=httpx.MockTransport(handler),
    )
    try:
        payload = {
            "symbol": "AAPL",
            "side": "BUY",
            "orderType": "LIMIT",
            "quantity": "10",
            "price": "150.0",
            "clientOrderId": "abc123",
        }
        res = await client.place_order(payload)
    finally:
        await client.aclose()

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/orders"
    assert seen["headers"]["x-tossinvest-account"] == "999"
    assert json.loads(seen["body"]) == payload
    assert res.order_id == "new-ord-123"
    assert res.client_order_id == "abc123"


@pytest.mark.asyncio
async def test_modify_order_posts_to_modify_path_and_parses_new_order_id() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json=_json({"orderId": "mod-ord-456"}),
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        account_seq=999,
        transport=httpx.MockTransport(handler),
    )
    try:
        payload = {
            "orderType": "LIMIT",
            "price": "155.0",
            "quantity": "12",
        }
        res = await client.modify_order("orig-ord-123", payload)
    finally:
        await client.aclose()

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/orders/orig-ord-123/modify"
    assert json.loads(seen["body"]) == payload
    assert res.order_id == "mod-ord-456"


@pytest.mark.asyncio
async def test_cancel_order_posts_to_cancel_path_and_parses_new_order_id() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json=_json({"orderId": "can-ord-789"}),
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        account_seq=999,
        transport=httpx.MockTransport(handler),
    )
    try:
        res = await client.cancel_order("orig-ord-123")
    finally:
        await client.aclose()

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/orders/orig-ord-123/cancel"
    assert json.loads(seen["body"]) == {}
    assert res.order_id == "can-ord-789"


@pytest.mark.asyncio
async def test_warnings_fetches_and_parses() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json=_json(
                [
                    {
                        "warningType": "LIQUIDATION_TRADING",
                        "exchange": "KRX",
                        "startDate": "2026-06-12",
                        "endDate": None,
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
        warnings = await client.warnings("005930")
    finally:
        await client.aclose()

    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/stocks/005930/warnings"
    assert len(warnings) == 1
    assert warnings[0].warning_type == "LIQUIDATION_TRADING"
    assert warnings[0].exchange == "KRX"
    assert warnings[0].start_date == "2026-06-12"
    assert warnings[0].end_date is None
