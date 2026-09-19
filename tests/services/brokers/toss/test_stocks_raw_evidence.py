"""ROB-1172 AC1: the Toss master raw GET used for metadata snapshot hashing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.client import TossReadClient

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _client() -> TossReadClient:
    return TossReadClient(
        token_manager=TossOAuthTokenManager(
            client_id="id", client_secret=SecretStr("secret")
        )
    )


async def test_stocks_raw_returns_the_unparsed_payload(monkeypatch) -> None:
    client = _client()
    payload = {
        "result": [
            {"symbol": "005930", "securityType": "STOCK", "unmappedField": "keep-me"}
        ]
    }
    request = AsyncMock(return_value=payload)
    monkeypatch.setattr(client, "_request", request)

    result = await client.stocks_raw(["005930"])

    assert result == payload
    call = request.await_args
    assert call is not None
    assert call.args[0] == "GET"
    assert call.args[1] == "/api/v1/stocks"
    assert call.kwargs["params"] == {"symbols": "005930"}


async def test_stocks_raw_enforces_the_batch_size_contract(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(client, "_request", AsyncMock(return_value={}))

    with pytest.raises(ValueError):
        await client.stocks_raw([])
    with pytest.raises(ValueError):
        await client.stocks_raw([f"{index:06d}" for index in range(201)])
