from __future__ import annotations

import pytest

from tests._mcp_tooling_support import DummyMCP


def test_default_account_mode_is_kis_live():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode()

    assert routing.account_mode == "kis_live"
    assert routing.is_kis_live is True
    assert routing.warnings == []


def test_account_type_paper_is_db_simulated_alias():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_type="paper")

    assert routing.account_mode == "db_simulated"
    assert routing.is_db_simulated is True
    assert routing.deprecated_alias_used is True
    assert routing.warnings


def test_account_mode_simulated_is_db_simulated_alias():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_mode="simulated")

    assert routing.account_mode == "db_simulated"
    assert routing.is_db_simulated is True
    assert routing.deprecated_alias_used is True


def test_account_mode_kis_mock_is_official_kis_mock():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_mode="kis_mock")

    assert routing.account_mode == "kis_mock"
    assert routing.is_kis_mock is True
    assert routing.is_db_simulated is False


def test_conflicting_account_selectors_fail():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    with pytest.raises(ValueError, match="conflicting account selectors"):
        normalize_account_mode(account_mode="kis_mock", account_type="paper")


def test_validate_kis_mock_config_reports_names_only():
    from app.core.config import validate_kis_mock_config

    class DummySettings:
        kis_mock_enabled = False
        kis_mock_app_key = None
        kis_mock_app_secret = "secret-value"
        kis_mock_account_no = ""

    missing = validate_kis_mock_config(DummySettings())

    assert missing == [
        "KIS_MOCK_ENABLED",
        "KIS_MOCK_APP_KEY",
        "KIS_MOCK_ACCOUNT_NO",
    ]
    assert "secret-value" not in repr(missing)


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_fails_closed_when_config_missing(monkeypatch):
    from app.mcp_server.tooling import orders_registration

    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)

    monkeypatch.setattr(
        orders_registration,
        "validate_kis_mock_config",
        lambda: ["KIS_MOCK_ENABLED", "KIS_MOCK_APP_KEY"],
    )

    captured: list = []

    async def fake_cancel_impl(**kwargs):
        captured.append(kwargs)
        return {"success": True, "order_id": kwargs["order_id"]}

    monkeypatch.setattr(
        orders_registration, "cancel_order_impl", fake_cancel_impl
    )

    result = await mcp.tools["cancel_order"](
        order_id="test-order",
        account_mode="kis_mock",
    )

    assert result["success"] is False
    assert "KIS_MOCK_ENABLED" in result["error"]
    assert "KIS_MOCK_APP_KEY" in result["error"]
    assert result["account_mode"] == "kis_mock"
    assert captured == []


@pytest.mark.asyncio
async def test_cancel_order_db_simulated_is_not_supported(monkeypatch):
    from app.mcp_server.tooling import orders_registration

    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)

    result = await mcp.tools["cancel_order"](
        order_id="test-order",
        account_mode="db_simulated",
    )

    assert result["success"] is False
    assert "not supported" in result["error"].lower()
    assert result["account_mode"] == "db_simulated"


@pytest.mark.asyncio
async def test_modify_order_kis_mock_fails_closed_when_config_missing(monkeypatch):
    from app.mcp_server.tooling import orders_registration

    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)

    monkeypatch.setattr(
        orders_registration,
        "validate_kis_mock_config",
        lambda: ["KIS_MOCK_ENABLED", "KIS_MOCK_ACCOUNT_NO"],
    )

    captured: list = []

    async def fake_modify_impl(**kwargs):
        captured.append(kwargs)
        return {"success": True, "order_id": kwargs["order_id"]}

    monkeypatch.setattr(
        orders_registration, "modify_order_impl", fake_modify_impl
    )

    result = await mcp.tools["modify_order"](
        order_id="test-order",
        symbol="005930",
        account_mode="kis_mock",
        new_price=70000.0,
    )

    assert result["success"] is False
    assert "KIS_MOCK_ENABLED" in result["error"]
    assert "KIS_MOCK_ACCOUNT_NO" in result["error"]
    assert result["account_mode"] == "kis_mock"
    assert captured == []


@pytest.mark.asyncio
async def test_modify_order_db_simulated_is_not_supported():
    from app.mcp_server.tooling import orders_registration

    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)

    result = await mcp.tools["modify_order"](
        order_id="test-order",
        symbol="005930",
        account_mode="db_simulated",
        new_price=70000.0,
    )

    assert result["success"] is False
    assert "not supported" in result["error"].lower()
    assert result["account_mode"] == "db_simulated"


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_passes_is_mock_to_impl(monkeypatch):
    from app.mcp_server.tooling import orders_registration

    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)

    monkeypatch.setattr(
        orders_registration,
        "validate_kis_mock_config",
        lambda: [],
    )

    captured: list = []

    async def fake_cancel_impl(**kwargs):
        captured.append(kwargs)
        return {"success": True, "order_id": kwargs["order_id"]}

    monkeypatch.setattr(
        orders_registration, "cancel_order_impl", fake_cancel_impl
    )

    result = await mcp.tools["cancel_order"](
        order_id="test-order",
        account_mode="kis_mock",
    )

    assert result["success"] is True
    assert captured == [{"order_id": "test-order", "symbol": None, "market": None, "is_mock": True}]


@pytest.mark.asyncio
async def test_modify_order_kis_mock_passes_is_mock_to_impl(monkeypatch):
    from app.mcp_server.tooling import orders_registration

    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)

    monkeypatch.setattr(
        orders_registration,
        "validate_kis_mock_config",
        lambda: [],
    )

    captured: list = []

    async def fake_modify_impl(**kwargs):
        captured.append(kwargs)
        return {"success": True, "order_id": kwargs["order_id"]}

    monkeypatch.setattr(
        orders_registration, "modify_order_impl", fake_modify_impl
    )

    result = await mcp.tools["modify_order"](
        order_id="test-order",
        symbol="005930",
        account_mode="kis_mock",
        new_price=70000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert captured == [{
        "order_id": "test-order",
        "symbol": "005930",
        "market": None,
        "new_price": 70000.0,
        "new_quantity": None,
        "dry_run": True,
        "is_mock": True,
    }]
