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


# ---------------------------------------------------------------------------
# ROB-105: confirmed actions must NOT return stub success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_confirmed_returns_explicit_not_implemented_failure(
    monkeypatch,
):
    """dry_run=False + confirm=True must NOT return stub success — that would
    trick operators into thinking a real mock order was submitted."""

    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_place_order_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert "not implemented" in response["error"].lower()
    assert response["account_mode"] == "kiwoom_mock"
    assert impl_calls["count"] == 0


@pytest.mark.asyncio
async def test_cancel_order_confirmed_returns_explicit_not_implemented_failure(
    monkeypatch,
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_cancel_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        symbol="005930",
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert "not implemented" in response["error"].lower()
    assert impl_calls["count"] == 0


@pytest.mark.asyncio
async def test_modify_order_confirmed_returns_explicit_not_implemented_failure(
    monkeypatch,
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_modify_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_modify_order"](
        order_id="0000111222",
        symbol="005930",
        new_price=72000,
        new_quantity=2,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert "not implemented" in response["error"].lower()
    assert impl_calls["count"] == 0


@pytest.mark.asyncio
async def test_place_order_dry_run_false_without_confirm_blocked(monkeypatch):
    """dry_run=False + confirm=False must continue to be blocked (separate path)."""

    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_place_order_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        dry_run=False,
        confirm=False,
    )

    assert response["success"] is False
    assert "confirm=true" in response["error"].lower()
    assert impl_calls["count"] == 0


# ---------------------------------------------------------------------------
# ROB-105: positive-amount validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_qty", [0, -1, -1000])
async def test_place_order_rejects_non_positive_quantity(monkeypatch, bad_qty):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_place_order_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="buy",
        quantity=bad_qty,
        price=70000,
    )

    assert response["success"] is False
    assert "quantity" in response["error"].lower()
    assert impl_calls["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [0, -1, -100])
async def test_place_order_rejects_non_positive_price(monkeypatch, bad_price):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_place_order_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="buy",
        quantity=1,
        price=bad_price,
    )

    assert response["success"] is False
    assert "price" in response["error"].lower()
    assert impl_calls["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_qty", [0, -1])
async def test_cancel_order_rejects_non_positive_cancel_quantity(monkeypatch, bad_qty):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_cancel_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        symbol="005930",
        cancel_quantity=bad_qty,
    )

    assert response["success"] is False
    assert "cancel_quantity" in response["error"].lower()
    assert impl_calls["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("new_quantity", {"new_quantity": 0, "new_price": 100}),
        ("new_quantity", {"new_quantity": -1, "new_price": 100}),
        ("new_price", {"new_quantity": 1, "new_price": 0}),
        ("new_price", {"new_quantity": 1, "new_price": -100}),
    ],
)
async def test_modify_order_rejects_non_positive_amounts(monkeypatch, field, kwargs):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_modify_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_modify_order"](
        order_id="0000111222",
        symbol="005930",
        **kwargs,
    )

    assert response["success"] is False
    assert field in response["error"].lower()
    assert impl_calls["count"] == 0


@pytest.mark.asyncio
async def test_modify_order_allows_omitted_amounts(monkeypatch):
    """Either new_quantity or new_price may be omitted (only the supplied
    field is validated and forwarded)."""

    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    captured: dict[str, Any] = {}

    async def fake_impl(**kwargs):
        captured.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_modify_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_modify_order"](
        order_id="0000111222",
        symbol="005930",
        new_price=72000,
    )

    assert response["success"] is True
    assert captured["new_price"] == 72000
    assert captured["new_quantity"] is None
