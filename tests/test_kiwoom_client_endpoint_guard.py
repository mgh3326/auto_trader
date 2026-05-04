# tests/test_kiwoom_client_endpoint_guard.py
"""Guard tests: Kiwoom mock client must refuse live URL and incomplete config."""

from __future__ import annotations

import httpx
import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomEndpointError,
    KiwoomMockClient,
)


def test_constructor_rejects_live_base_url():
    with pytest.raises(KiwoomEndpointError, match="mockapi.kiwoom.com"):
        KiwoomMockClient(
            base_url="https://api.kiwoom.com",
            app_key="ak",
            app_secret="sk",
            account_no="123",
        )


def test_constructor_rejects_unrelated_base_url():
    with pytest.raises(KiwoomEndpointError):
        KiwoomMockClient(
            base_url="https://example.com",
            app_key="ak",
            app_secret="sk",
            account_no="123",
        )


def test_from_app_settings_fails_closed_when_disabled(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_enabled", False)
    with pytest.raises(KiwoomConfigurationError) as exc:
        KiwoomMockClient.from_app_settings()
    assert "KIWOOM_MOCK_ENABLED" in str(exc.value)


def test_from_app_settings_fails_closed_when_credentials_missing(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_key", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_secret", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_account_no", None)
    with pytest.raises(KiwoomConfigurationError) as exc:
        KiwoomMockClient.from_app_settings()
    msg = str(exc.value)
    assert "KIWOOM_MOCK_APP_KEY" in msg
    assert "KIWOOM_MOCK_APP_SECRET" in msg
    assert "KIWOOM_MOCK_ACCOUNT_NO" in msg


@pytest.mark.asyncio
async def test_post_api_sends_required_headers(monkeypatch):
    from app.services.brokers.kiwoom.client import KiwoomMockClient

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json={"return_code": 0, "return_msg": "정상", "rows": []},
            headers={"cont-yn": "N", "next-key": ""},
        )

    transport = httpx.MockTransport(handler)
    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="sk",
        account_no="123",
    )
    # Inject test-only token + transport (helper exposed for testing only).
    client.set_transport_for_test(transport, token="TKN-XYZ")

    result = await client.post_api(
        api_id=constants.ORDER_BUY_API_ID,
        path=constants.ORDER_PATH,
        body={"foo": "bar"},
        cont_yn="N",
        next_key="",
    )

    assert captured["headers"][constants.HEADER_AUTHORIZATION] == "Bearer TKN-XYZ"
    assert captured["headers"][constants.HEADER_API_ID] == constants.ORDER_BUY_API_ID
    assert captured["headers"][constants.HEADER_CONT_YN] == "N"
    assert captured["headers"][constants.HEADER_NEXT_KEY] == ""
    assert captured["url"].endswith(constants.ORDER_PATH)
    assert result["return_code"] == 0
    assert result["continuation"]["cont_yn"] == "N"
    assert result["continuation"]["next_key"] == ""


@pytest.mark.asyncio
async def test_post_api_normalizes_continuation_headers():
    from app.services.brokers.kiwoom.client import KiwoomMockClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"return_code": 0, "return_msg": "정상"},
            headers={"cont-yn": "Y", "next-key": "page-2"},
        )

    transport = httpx.MockTransport(handler)
    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="sk",
        account_no="123",
    )
    client.set_transport_for_test(transport, token="TKN")

    result = await client.post_api(
        api_id=constants.ACCOUNT_BALANCE_API_ID,
        path="/api/dostk/acnt",
        body={},
    )
    assert result["continuation"] == {"cont_yn": "Y", "next_key": "page-2"}


@pytest.mark.asyncio
async def test_post_api_rejects_non_mock_path_traversal():
    from app.services.brokers.kiwoom.client import KiwoomMockClient

    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="sk",
        account_no="123",
    )
    with pytest.raises(ValueError):
        await client.post_api(
            api_id=constants.ORDER_BUY_API_ID,
            path="https://api.kiwoom.com/api/dostk/ordr",
            body={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_path",
    [
        "//api.kiwoom.com/api/dostk/ordr",
        "//evil.example.com/api/dostk/ordr",
        "///api/dostk/ordr",
        "http://api.kiwoom.com/api/dostk/ordr",
        "https://api.kiwoom.com/api/dostk/ordr",
        "api/dostk/ordr",
        "",
        "/api/dostk\\ordr",
        "/api/dostk/ordr\nHost: evil.example.com",
    ],
)
async def test_post_api_rejects_unsafe_path_shapes(unsafe_path):
    """Network-path references and other non-relative shapes must be rejected.

    A leading ``//`` is dangerous because URL-join semantics treat it as a
    network-path reference and can re-target the request to a different host.
    """

    from app.services.brokers.kiwoom.client import KiwoomMockClient

    transport_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        transport_calls["count"] += 1
        return httpx.Response(200, json={"return_code": 0})

    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="sk",
        account_no="123",
    )
    client.set_transport_for_test(httpx.MockTransport(handler), token="TKN")

    with pytest.raises(ValueError):
        await client.post_api(
            api_id=constants.ORDER_BUY_API_ID,
            path=unsafe_path,
            body={},
        )
    assert transport_calls["count"] == 0


@pytest.mark.asyncio
async def test_post_api_rejects_request_resolved_to_non_mock_host():
    """Defense-in-depth: if the resolved request host ever stops being
    ``mockapi.kiwoom.com``, the send must be aborted before any I/O."""

    transport_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        transport_calls["count"] += 1
        return httpx.Response(200, json={"return_code": 0})

    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="sk",
        account_no="123",
    )
    client.set_transport_for_test(httpx.MockTransport(handler), token="TKN")

    # Simulate post-construction tampering: even if some future code mutates
    # the base URL away from mockapi, post_api must still refuse to send.
    client._base_url = "https://api.kiwoom.com"  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="non-mock host"):
        await client.post_api(
            api_id=constants.ORDER_BUY_API_ID,
            path="/api/dostk/ordr",
            body={},
        )
    assert transport_calls["count"] == 0
