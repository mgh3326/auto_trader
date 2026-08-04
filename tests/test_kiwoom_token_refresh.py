"""Kiwoom 8005 recovery is read-only, single-resubmit, and fail-closed."""

from __future__ import annotations

import datetime as dt
from typing import Any

import fakeredis.aioredis
import httpx
import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.auth import KiwoomAuthClient, KiwoomToken
from app.services.brokers.kiwoom.client import KiwoomMockClient


class _RecordingAuth:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, str | None]] = []

    async def get_token(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str:
        self.calls.append((force_reissue, failed_token))
        return "fresh-token" if force_reissue else "stale-token"


def _client_with_transport(
    handler: Any,
) -> KiwoomMockClient:
    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="app-key",
        app_secret="app-secret",
        account_no="account-no",
    )
    client._transport = httpx.MockTransport(handler)
    return client


@pytest.mark.asyncio
async def test_read_8005_invalidates_cached_token_and_retries_once(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="app-key",
        app_secret="app-secret",
        redis_client=redis_client,
    )
    await auth._cache_token(
        KiwoomToken(
            access_token="stale-token",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        )
    )
    issue_calls = 0

    async def issue_token() -> KiwoomToken:
        nonlocal issue_calls
        issue_calls += 1
        return KiwoomToken(
            access_token="fresh-token",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        )

    monkeypatch.setattr(auth, "_issue_token", issue_token)
    dispatched_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers[constants.HEADER_AUTHORIZATION]
        dispatched_authorizations.append(authorization)
        if authorization == "Bearer stale-token":
            return httpx.Response(
                200,
                json={"return_code": 8005, "return_msg": "Token invalid"},
            )
        return httpx.Response(
            200,
            json={"return_code": 0, "return_msg": "OK", "result_list": []},
        )

    client = _client_with_transport(handler)
    client._auth = auth

    result = await client.post_api(
        api_id=constants.US_ACCOUNT_POSITIONS_API_ID,
        path=constants.US_ACCOUNT_PATH,
        body={},
    )

    assert result["return_code"] == 0
    assert dispatched_authorizations == [
        "Bearer stale-token",
        "Bearer fresh-token",
    ]
    assert issue_calls == 1
    assert await auth.get_token() == "fresh-token"


@pytest.mark.asyncio
async def test_repeated_read_8005_is_resubmitted_only_once():
    dispatch_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal dispatch_count
        dispatch_count += 1
        return httpx.Response(
            200,
            json={"return_code": "8005", "return_msg": "Token invalid"},
        )

    auth = _RecordingAuth()
    client = _client_with_transport(handler)
    client._auth = auth  # type: ignore[assignment]

    result = await client.post_api(
        api_id=constants.US_ACCOUNT_POSITIONS_API_ID,
        path=constants.US_ACCOUNT_PATH,
        body={},
    )

    assert result["return_code"] == "8005"
    assert dispatch_count == 2
    assert auth.calls == [
        (False, None),
        (True, "stale-token"),
    ]


@pytest.mark.asyncio
async def test_chart_observed_return_code_3_with_8005_message_refreshes_once():
    dispatched_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers[constants.HEADER_AUTHORIZATION]
        dispatched_authorizations.append(authorization)
        if authorization == "Bearer stale-token":
            return httpx.Response(
                200,
                json={
                    "return_code": 3,
                    "return_msg": "인증에 실패했습니다[8005:Token이 유효하지 않습니다]",
                },
            )
        return httpx.Response(
            200,
            json={
                "return_code": 0,
                "return_msg": "정상",
                "stk_min_pole_chart_qry": [{"cntr_tm": "20260724153000"}],
            },
        )

    auth = _RecordingAuth()
    client = _client_with_transport(handler)
    client._auth = auth  # type: ignore[assignment]

    result = await client.post_api(
        api_id=constants.CHART_MINUTE_API_ID,
        path=constants.CHART_PATH,
        body={"stk_cd": "196170", "tic_scope": "1"},
    )

    assert result["return_code"] == 0
    assert len(result["stk_min_pole_chart_qry"]) == 1
    assert dispatched_authorizations == [
        "Bearer stale-token",
        "Bearer fresh-token",
    ]
    assert auth.calls == [
        (False, None),
        (True, "stale-token"),
    ]


@pytest.mark.asyncio
async def test_order_mutation_observed_return_code_3_with_8005_is_not_resubmitted():
    dispatch_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal dispatch_count
        dispatch_count += 1
        return httpx.Response(
            200,
            json={
                "return_code": 3,
                "return_msg": "인증에 실패했습니다[8005:Token이 유효하지 않습니다]",
            },
        )

    auth = _RecordingAuth()
    client = _client_with_transport(handler)
    client._auth = auth  # type: ignore[assignment]

    result = await client.post_api(
        api_id=constants.ORDER_BUY_API_ID,
        path=constants.ORDER_PATH,
        body={},
    )

    assert result["return_code"] == 3
    assert dispatch_count == 1
    assert auth.calls == [(False, None)]


@pytest.mark.parametrize(
    ("api_id", "path"),
    [
        (constants.ORDER_BUY_API_ID, constants.ORDER_PATH),
        (constants.ORDER_SELL_API_ID, constants.ORDER_PATH),
        (constants.ORDER_MODIFY_API_ID, constants.ORDER_PATH),
        (constants.ORDER_CANCEL_API_ID, constants.ORDER_PATH),
        (constants.US_ORDER_BUY_API_ID, constants.US_ORDER_PATH),
        (constants.US_ORDER_SELL_API_ID, constants.US_ORDER_PATH),
        (constants.US_ORDER_MODIFY_API_ID, constants.US_ORDER_PATH),
        (constants.US_ORDER_CANCEL_API_ID, constants.US_ORDER_PATH),
    ],
)
@pytest.mark.asyncio
async def test_order_mutation_8005_is_not_resubmitted(api_id: str, path: str):
    dispatch_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal dispatch_count
        dispatch_count += 1
        return httpx.Response(
            200,
            json={"return_code": 8005, "return_msg": "Token invalid"},
        )

    auth = _RecordingAuth()
    client = _client_with_transport(handler)
    client._auth = auth  # type: ignore[assignment]

    result = await client.post_api(
        api_id=api_id,
        path=path,
        body={},
    )

    assert result["return_code"] == 8005
    assert dispatch_count == 1
    assert auth.calls == [(False, None)]
