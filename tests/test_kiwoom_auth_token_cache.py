# tests/test_kiwoom_auth_token_cache.py
"""Verify Kiwoom OAuth token cache: refresh on expiry, never log secret/token."""

from __future__ import annotations

import datetime as dt
import logging

import httpx
import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.auth import KiwoomAuthClient


@pytest.fixture
def mock_token_transport() -> httpx.MockTransport:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        assert request.method == "POST"
        assert request.url.path == constants.OAUTH_PATH
        body = request.read()
        # Body must NOT echo as raw secret in log; presence is fine.
        assert b"client_credentials" in body
        return httpx.Response(
            200,
            json={
                "token": f"tkn-{calls['count']}",
                "expires_dt": (
                    dt.datetime.now(dt.UTC) + dt.timedelta(seconds=300)
                ).strftime("%Y%m%d%H%M%S"),
                "token_type": "Bearer",
                "return_code": 0,
                "return_msg": "정상",
            },
        )

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


@pytest.mark.asyncio
async def test_token_is_cached_until_near_expiry(mock_token_transport):
    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="SECRET-VAL",
        transport=mock_token_transport,
    )
    t1 = await auth.get_token()
    t2 = await auth.get_token()
    assert t1 == t2
    assert mock_token_transport.calls["count"] == 1


@pytest.mark.asyncio
async def test_token_refreshed_when_expired(monkeypatch, mock_token_transport):
    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="SECRET-VAL",
        transport=mock_token_transport,
    )
    await auth.get_token()
    # Force expiry.
    auth._cached_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    await auth.get_token()
    assert mock_token_transport.calls["count"] == 2


@pytest.mark.asyncio
async def test_logs_never_contain_secret_or_token(caplog, mock_token_transport):
    caplog.set_level(logging.DEBUG, logger="app.services.brokers.kiwoom")
    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="SECRET-VAL",
        transport=mock_token_transport,
    )
    token = await auth.get_token()
    rendered = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "SECRET-VAL" not in rendered
    assert token not in rendered
