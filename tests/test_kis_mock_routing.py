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


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_uses_mock_client(monkeypatch):
    from app.mcp_server.tooling import orders_modify_cancel

    instances: list[bool] = []

    class TrackedKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            instances.append(is_mock)
            self.is_mock = is_mock
            self.inquire_korea_orders = AsyncMock(
                return_value=[
                    {
                        "odno": "0001",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_unpr": "70000",
                        "ord_qty": "1",
                    }
                ]
            )
            self.cancel_korea_order = AsyncMock(return_value={"ord_tmd": "100000"})

    monkeypatch.setattr(orders_modify_cancel, "KISClient", TrackedKISClient)

    result = await orders_modify_cancel.cancel_order_impl(
        order_id="0001", symbol="005930", market="kr", is_mock=True
    )

    assert result["success"] is True
    assert all(flag is True for flag in instances), instances


@pytest.mark.asyncio
async def test_modify_order_kis_mock_uses_mock_client(monkeypatch):
    from app.mcp_server.tooling import orders_modify_cancel

    instances: list[bool] = []

    class TrackedKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            instances.append(is_mock)
            self.inquire_korea_orders = AsyncMock(
                return_value=[
                    {
                        "odno": "0001",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_unpr": "70000",
                        "ord_qty": "1",
                    }
                ]
            )
            self.modify_korea_order = AsyncMock(return_value={"odno": "0002"})

    monkeypatch.setattr(orders_modify_cancel, "KISClient", TrackedKISClient)

    result = await orders_modify_cancel.modify_order_impl(
        order_id="0001",
        symbol="005930",
        market="kr",
        new_price=70100.0,
        dry_run=False,
        is_mock=True,
    )

    assert result["success"] is True
    assert result["new_order_id"] == "0002"
    assert all(flag is True for flag in instances), instances


def test_modify_order_kis_mock_dry_run_does_not_instantiate_kis(monkeypatch):
    """Dry-run preview must not require any KIS client instantiation."""
    from app.mcp_server.tooling import orders_modify_cancel

    class BrokenKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            raise AssertionError("must not instantiate KIS in dry-run preview")

    monkeypatch.setattr(orders_modify_cancel, "KISClient", BrokenKISClient)
    # Dry-run should succeed without instantiating KIS client.
    import asyncio

    result = asyncio.run(
        orders_modify_cancel.modify_order_impl(
            order_id="0001",
            symbol="005930",
            market="kr",
            new_price=70100.0,
            dry_run=True,
            is_mock=True,
        )
    )
    assert result["success"] is True
    assert result["dry_run"] is True


@pytest.mark.asyncio
async def test_get_order_history_pending_us_mock_surfaces_unsupported(monkeypatch):
    """Mock pending US history must NOT silently return empty."""
    from app.mcp_server.tooling import orders_history

    class FakeKIS:
        def __init__(self, *, is_mock: bool = False) -> None:
            pass

        async def inquire_overseas_orders(self, exchange, *, is_mock=False):
            raise RuntimeError(
                "KIS overseas pending-orders inquiry (TTTS3018R) is not "
                "available in mock mode."
            )

    monkeypatch.setattr(orders_history, "KISClient", FakeKIS)
    result = await orders_history.get_order_history_impl(
        status="pending", market="us", is_mock=True
    )

    assert result["orders"] == []
    assert any(
        e.get("market") == "equity_us"
        and "mock" in (e.get("error") or "").lower()
        for e in result["errors"]
    )
