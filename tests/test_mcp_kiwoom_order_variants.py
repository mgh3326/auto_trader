# tests/test_mcp_kiwoom_order_variants.py
"""Verify kiwoom_mock_* MCP tools are registered, fail-closed, and KRX-only.

Mirrors the patterns in tests/test_mcp_kis_order_variants.py.
"""

from __future__ import annotations

from typing import Any

import pytest


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str = ""):  # noqa: ARG002
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


EXPECTED_TOOL_NAMES = {
    "kiwoom_mock_preview_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_modify_order",
    "kiwoom_mock_get_order_history",
    "kiwoom_mock_get_positions",
    "kiwoom_mock_get_orderable_cash",
}


def _register(mcp: DummyMCP) -> None:
    from app.mcp_server.tooling import orders_kiwoom_variants

    orders_kiwoom_variants.register(mcp)


def test_all_seven_tools_register():
    mcp = DummyMCP()
    _register(mcp)
    assert EXPECTED_TOOL_NAMES.issubset(set(mcp.tools))


@pytest.mark.asyncio
async def test_place_order_fails_closed_when_mock_disabled(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_enabled", False)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
    )

    assert response["success"] is False
    assert "KIWOOM_MOCK_ENABLED" in response["error"]
    assert response["account_mode"] == "kiwoom_mock"


@pytest.mark.asyncio
async def test_place_order_defaults_to_dry_run(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    captured: dict[str, Any] = {}

    async def fake_impl(**kwargs):
        captured.update(kwargs)
        return {"success": True, "echo": kwargs}

    monkeypatch.setattr(mod, "_kiwoom_mock_place_order_impl", fake_impl)
    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)

    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
    )

    assert response["success"] is True
    assert captured["dry_run"] is True


@pytest.mark.asyncio
async def test_place_order_rejects_non_kr_market(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="AAPL",
        side="buy",
        quantity=1,
        price=100,
        market="us",
    )
    assert response["success"] is False
    assert "kr" in response["error"].lower()


@pytest.mark.asyncio
async def test_place_order_rejects_nxt_or_sor(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    mcp = DummyMCP()
    _register(mcp)

    for bad in ("NXT", "SOR", "nxt"):
        response = await mcp.tools["kiwoom_mock_place_order"](
            symbol="005930",
            side="buy",
            quantity=1,
            price=70000,
            exchange=bad,
        )
        assert response["success"] is False
        assert "krx" in response["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "../etc", "a/b", "a?b=c", "a,b", "a b", "a\nb"],
)
async def test_cancel_rejects_unsafe_order_ids(monkeypatch, bad_id):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id=bad_id,
        symbol="005930",
    )
    assert response["success"] is False
    assert "order" in response["error"].lower()
