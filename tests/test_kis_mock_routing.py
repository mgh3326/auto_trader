from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def test_kis_mock_settings_view_uses_mock_base_url(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_base_url",
        "https://live.example.invalid",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_mock_base_url",
        "https://mock.example.invalid",
        raising=False,
    )

    client = KISClient(is_mock=True)

    assert client._settings.kis_base_url == "https://mock.example.invalid"
    assert client._kis_url("/uapi/test") == "https://mock.example.invalid/uapi/test"


@pytest.mark.asyncio
async def test_kis_mock_fetch_token_posts_to_mock_base_url(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_mock_base_url",
        "https://mock.example.invalid",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_mock_app_key",
        "mock-key",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.client.settings.kis_mock_app_secret",
        "mock-secret",
        raising=False,
    )

    response = MagicMock()
    response.json.return_value = {"access_token": "token", "expires_in": 1234}
    http_client = AsyncMock()
    http_client.post.return_value = response

    client = KISClient(is_mock=True)
    monkeypatch.setattr(client, "_ensure_client", AsyncMock(return_value=http_client))

    token, expires_in = await client._fetch_token()

    assert token == "token"
    assert expires_in == 1234
    assert http_client.post.await_args.args[0] == (
        "https://mock.example.invalid/oauth2/token"
    )


def test_order_execution_mock_client_factory_does_not_fallback_to_live(monkeypatch):
    from app.mcp_server.tooling import order_execution

    class BrokenKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            if is_mock:
                raise TypeError("is_mock unsupported")

    monkeypatch.setattr(order_execution, "KISClient", BrokenKISClient)

    with pytest.raises(TypeError, match="is_mock unsupported"):
        order_execution._create_kis_client(is_mock=True)


def test_orders_history_mock_client_factory_does_not_fallback_to_live(monkeypatch):
    from app.mcp_server.tooling import orders_history

    class BrokenKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            if is_mock:
                raise TypeError("is_mock unsupported")

    monkeypatch.setattr(orders_history, "KISClient", BrokenKISClient)

    with pytest.raises(TypeError, match="is_mock unsupported"):
        orders_history._create_kis_client(is_mock=True)


def test_portfolio_cash_mock_client_factory_does_not_fallback_to_live(monkeypatch):
    from app.mcp_server.tooling import portfolio_cash

    class BrokenKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            if is_mock:
                raise TypeError("is_mock unsupported")

    monkeypatch.setattr(portfolio_cash, "KISClient", BrokenKISClient)

    with pytest.raises(TypeError, match="is_mock unsupported"):
        portfolio_cash._create_kis_client(is_mock=True)


def test_order_validation_mock_client_factory_does_not_fallback_to_live(monkeypatch):
    from app.mcp_server.tooling import order_validation

    class BrokenKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            if is_mock:
                raise TypeError("is_mock unsupported")

    monkeypatch.setattr(order_validation, "KISClient", BrokenKISClient)

    with pytest.raises(TypeError, match="is_mock unsupported"):
        order_validation._create_kis_client(is_mock=True)


@pytest.mark.asyncio
async def test_portfolio_holdings_mock_collection_does_not_fallback_to_live(
    monkeypatch,
):
    from app.mcp_server.tooling import portfolio_holdings

    class BrokenKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            if is_mock:
                raise TypeError("is_mock unsupported")

    monkeypatch.setattr(portfolio_holdings, "KISClient", BrokenKISClient)

    with pytest.raises(TypeError, match="is_mock unsupported"):
        await portfolio_holdings._collect_kis_positions(None, is_mock=True)
