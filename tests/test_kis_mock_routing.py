from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

KOREA_PENDING_MOCK_UNSUPPORTED = (
    "KIS domestic pending-orders inquiry (TTTC8036R) is not available in mock mode."
)
OVERSEAS_PENDING_MOCK_UNSUPPORTED = (
    "KIS overseas pending-orders inquiry (TTTS3018R) is not available in mock mode."
)


class KoreaPendingMockUnsupportedKIS:
    def __init__(self, *, is_mock: bool = False) -> None:
        self.is_mock = is_mock

    async def inquire_korea_orders(self, *, is_mock=False):
        raise RuntimeError(KOREA_PENDING_MOCK_UNSUPPORTED)

    cancel_korea_order = AsyncMock(
        side_effect=AssertionError("must not call cancel under mock-unsupported")
    )
    modify_korea_order = AsyncMock(
        side_effect=AssertionError("must not call modify under mock-unsupported")
    )


class OverseasPendingMockUnsupportedKIS:
    def __init__(self, *, is_mock: bool = False) -> None:
        self.is_mock = is_mock

    async def inquire_overseas_orders(self, exchange, *, is_mock=False):
        raise RuntimeError(OVERSEAS_PENDING_MOCK_UNSUPPORTED)


def _use_placeholder_kis_account(monkeypatch, client) -> None:
    monkeypatch.setattr(
        type(client._settings),
        "kis_account_no",
        property(lambda self: "00000000-01"),
    )


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
async def test_order_validation_kis_mock_balance_uses_domestic_cash_not_integrated_margin(
    monkeypatch,
):
    from app.mcp_server.tooling import order_validation

    fake_kis = MagicMock()
    fake_kis.inquire_integrated_margin = AsyncMock(
        side_effect=AssertionError("must not call integrated margin in mock")
    )
    fake_kis.inquire_domestic_cash_balance = AsyncMock(
        return_value={"stck_cash_ord_psbl_amt": "900000", "dnca_tot_amt": "1000000"}
    )

    monkeypatch.setattr(
        order_validation, "_create_kis_client", lambda *, is_mock: fake_kis
    )

    balance = await order_validation._get_balance_for_order("equity_kr", is_mock=True)

    assert balance == pytest.approx(900000.0)
    fake_kis.inquire_integrated_margin.assert_not_called()
    fake_kis.inquire_domestic_cash_balance.assert_awaited_once_with(is_mock=True)


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

    monkeypatch.setattr(orders_history, "KISClient", OverseasPendingMockUnsupportedKIS)
    result = await orders_history.get_order_history_impl(
        status="pending", market="us", is_mock=True
    )

    assert result["orders"] == []
    assert any(
        e.get("market") == "equity_us" and "mock" in (e.get("error") or "").lower()
        for e in result["errors"]
    )


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_kr_returns_mock_unsupported(monkeypatch):
    from app.mcp_server.tooling import orders_modify_cancel

    monkeypatch.setattr(
        orders_modify_cancel, "KISClient", KoreaPendingMockUnsupportedKIS
    )

    result = await orders_modify_cancel.cancel_order_impl(
        order_id="0001", symbol="005930", market="kr", is_mock=True
    )

    assert result["success"] is False
    assert result.get("mock_unsupported") is True
    assert "mock" in result["error"].lower()


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_kr_without_symbol_returns_mock_unsupported(
    monkeypatch,
):
    from app.mcp_server.tooling import orders_modify_cancel

    monkeypatch.setattr(
        orders_modify_cancel, "KISClient", KoreaPendingMockUnsupportedKIS
    )

    result = await orders_modify_cancel.cancel_order_impl(
        order_id="0001", symbol=None, market="kr", is_mock=True
    )

    assert result["success"] is False
    assert result.get("mock_unsupported") is True
    assert "mock" in result["error"].lower()


@pytest.mark.asyncio
async def test_modify_order_kis_mock_kr_returns_mock_unsupported(monkeypatch):
    from app.mcp_server.tooling import orders_modify_cancel

    monkeypatch.setattr(
        orders_modify_cancel, "KISClient", KoreaPendingMockUnsupportedKIS
    )

    result = await orders_modify_cancel.modify_order_impl(
        order_id="0001",
        symbol="005930",
        market="kr",
        new_price=70100.0,
        dry_run=False,
        is_mock=True,
    )

    assert result["success"] is False
    assert result.get("mock_unsupported") is True
    assert "mock" in result["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_tr_id"),
    [
        ("inquire_daily_order_domestic", "VTTC8001R"),
        ("inquire_daily_order_overseas", "VTTS3035R"),
    ],
    ids=["domestic", "overseas"],
)
async def test_inquire_daily_order_mock_uses_mock_tr(
    monkeypatch,
    method_name: str,
    expected_tr_id: str,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient(is_mock=True)
    monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
    _use_placeholder_kis_account(monkeypatch, client)

    captured: dict = {}

    async def fake_request(method, url, *, headers, params, **kwargs):
        captured["tr_id"] = headers.get("tr_id")
        return {"rt_cd": "0", "output1": []}

    monkeypatch.setattr(client, "_request_with_rate_limit", fake_request)

    await getattr(client, method_name)(
        start_date="20260101", end_date="20260102", is_mock=True
    )
    assert captured["tr_id"] == expected_tr_id
