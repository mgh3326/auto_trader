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
from app.services.brokers.toss.errors import TossApiResponseError


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
async def test_account_seq_resolved_once_then_cached_across_calls() -> None:
    accounts_hits = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal accounts_hits
        if request.url.path == "/api/v1/accounts":
            accounts_hits += 1
            return httpx.Response(
                200,
                json=_json([{"accountNo": "1", "accountSeq": 7, "accountType": "B"}]),
                request=request,
            )
        # `/api/v1/orders/{id}` (single-order detail) is parsed by `parse_order`,
        # which requires a FULL flat order row (orderId/symbol/side/orderType/
        # timeInForce/status/quantity/currency/orderedAt). Returning the list shape
        # `{"orders": []}` here would raise KeyError('orderId') inside parse_order
        # (parse_order delegates to parse_orders([raw])) — so branch on the path and
        # return a valid single-order body. The `/api/v1/orders` LIST path still
        # returns the `{"orders": []}` page shape parse_orders expects.
        if request.url.path.startswith("/api/v1/orders/"):
            return httpx.Response(
                200,
                json=_json(
                    {
                        "orderId": "ord-1",
                        "symbol": "034020",
                        "side": "BUY",
                        "orderType": "LIMIT",
                        "timeInForce": "DAY",
                        "status": "PENDING",
                        "quantity": "3",
                        "currency": "KRW",
                        "orderedAt": "2026-07-01T00:00:00Z",
                    }
                ),
                request=request,
            )
        return httpx.Response(200, json=_json({"orders": []}), request=request)

    # account_seq=None → resolution goes through /accounts; the instance caches it.
    client = TossReadClient(
        token_manager=_TokenManager(),
        account_seq=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.list_orders(status="OPEN")
        await client.list_orders(status="CLOSED")
        await client.get_order("ord-1")
    finally:
        await client.aclose()

    assert accounts_hits == 1  # ROB-687: one /accounts for the whole client lifetime


@pytest.mark.asyncio
async def test_account_seq_guard_rejects_multiple_accounts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(
                200,
                json=_json(
                    [
                        {"accountNo": "1", "accountSeq": 7, "accountType": "B"},
                        {"accountNo": "2", "accountSeq": 8, "accountType": "B"},
                    ]
                ),
                request=request,
            )
        return httpx.Response(200, json=_json({"orders": []}), request=request)

    client = TossReadClient(
        token_manager=_TokenManager(),
        account_seq=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="exactly one account"):
            await client.list_orders(status="OPEN")
    finally:
        await client.aclose()


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
async def test_get_order_retries_once_after_403_non_json_with_reissued_token() -> None:
    calls = 0
    token_calls: list[bool] = []
    failed_tokens: list[str | None] = []
    seen_authorizations: list[str] = []

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
        seen_authorizations.append(request.headers["Authorization"])
        if calls == 1:
            return httpx.Response(
                403,
                text="<html><body>Forbidden stale token</body></html>",
                headers={"cf-ray": "ray-403"},
                request=request,
            )
        return httpx.Response(
            200,
            json=_json(
                {
                    "orderId": "ord-403",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "FILLED",
                    "price": "190",
                    "quantity": "1",
                    "orderAmount": None,
                    "currency": "USD",
                    "orderedAt": "2026-06-15T00:00:00Z",
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
        order = await client.get_order("ord-403")
    finally:
        await client.aclose()

    assert order.order_id == "ord-403"
    assert calls == 2
    assert token_calls == [False, True]
    assert failed_tokens == [None, "token-1"]
    assert seen_authorizations == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.asyncio
async def test_place_order_does_not_retry_403_non_json_for_mutation() -> None:
    calls = 0
    token_calls: list[bool] = []

    class TokenManager(_TokenManager):
        async def get_access_token(
            self, *, force_reissue: bool = False, failed_token: str | None = None
        ) -> str:
            token_calls.append(force_reissue)
            return "token-2" if force_reissue else "token-1"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            text="<html><body>Forbidden mutation</body></html>",
            headers={"cf-ray": "ray-post-403"},
            request=request,
        )

    client = TossReadClient(
        token_manager=TokenManager(),
        account_seq=999,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TossApiResponseError) as exc_info:
            await client.place_order(
                {
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "quantity": "1",
                    "price": "150.0",
                    "clientOrderId": "cid-post-403",
                }
            )
    finally:
        await client.aclose()

    assert calls == 1
    assert token_calls == [False]
    assert "status=403 code='non-json-response'" in str(exc_info.value)


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
async def test_get_order_429_non_json_backs_off_without_token_reissue(
    monkeypatch,
) -> None:
    calls = 0
    sleeps: list[float] = []
    token_calls: list[bool] = []
    seen_authorizations: list[str] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class TokenManager(_TokenManager):
        async def get_access_token(
            self, *, force_reissue: bool = False, failed_token: str | None = None
        ) -> str:
            token_calls.append(force_reissue)
            return "token-2" if force_reissue else "token-1"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        seen_authorizations.append(request.headers["Authorization"])
        if calls == 1:
            return httpx.Response(
                429,
                text="<html><body>Too Many Requests</body></html>",
                headers={"Retry-After": "2"},
                request=request,
            )
        return httpx.Response(
            200,
            json=_json(
                {
                    "orderId": "ord-rate",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "PENDING",
                    "price": "190",
                    "quantity": "1",
                    "orderAmount": None,
                    "currency": "USD",
                    "orderedAt": "2026-06-15T00:00:00Z",
                    "canceledAt": None,
                    "execution": {"filledQuantity": "0"},
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
        order = await client.get_order("ord-rate")
    finally:
        await client.aclose()

    assert order.order_id == "ord-rate"
    assert calls == 2
    assert sleeps == [2.0]
    assert token_calls == [False]
    assert seen_authorizations == ["Bearer token-1", "Bearer token-1"]


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
