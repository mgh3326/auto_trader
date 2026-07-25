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


def _patch_fake_kiwoom_order_client(
    monkeypatch, mod, responses: dict[str, dict[str, Any]]
):
    calls: list[dict[str, Any]] = []

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            calls.append({"client": "from_app_settings"})
            return cls()

    class FakeOrderClient:
        def __init__(self, client):
            calls.append({"order_client": client.__class__.__name__})

        async def place_buy_order(self, **kwargs):
            calls.append({"method": "buy", **kwargs})
            return responses["buy"]

        async def place_sell_order(self, **kwargs):
            calls.append({"method": "sell", **kwargs})
            return responses["sell"]

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", FakeOrderClient, raising=False
    )
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "quantity", "price", "broker_response", "extra_assertions"),
    [
        (
            "buy",
            1,
            70000,
            {"return_code": 0, "return_msg": "정상", "ord_no": "0000111222"},
            {"return_code": 0, "ord_no": "0000111222"},
        ),
        (
            "sell",
            2,
            71000,
            {
                "return_code": "0",
                "return_msg": "정상",
                "ord_no": "0000222333",
                "continuation": {"cont_yn": "N", "next_key": ""},
            },
            {
                "return_code": "0",
                "ord_no": "0000222333",
                "continuation": {"cont_yn": "N", "next_key": ""},
            },
        ),
    ],
)
async def test_place_order_confirmed_calls_kiwoom_mock_order_client(
    monkeypatch,
    side,
    quantity,
    price,
    broker_response,
    extra_assertions,
):
    """dry_run=False + confirm=True should call the Kiwoom mock client once.

    This must not return a local stub success: the response should come from
    KiwoomDomesticOrderClient using KiwoomMockClient.from_app_settings().
    """

    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    fallback_response = {"return_code": 0, "return_msg": "정상", "ord_no": "unused"}
    calls = _patch_fake_kiwoom_order_client(
        monkeypatch,
        mod,
        responses={
            "buy": fallback_response,
            "sell": fallback_response,
            side: broker_response,
        },
    )
    _patch_preflight_success(monkeypatch, mod)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side=side,
        quantity=quantity,
        price=price,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is True
    assert response["source"] == "kiwoom"
    assert response["account_mode"] == "kiwoom_mock"
    assert response["dry_run"] is False
    assert response["broker_response"] == broker_response
    assert response["return_msg"] == "정상"
    for key, value in extra_assertions.items():
        assert response[key] == value
    assert calls == [
        {"client": "from_app_settings"},
        {"order_client": "FakeKiwoomMockClient"},
        {
            "method": side,
            "symbol": "005930",
            "quantity": quantity,
            "price": price,
            "exchange": "KRX",
        },
    ]


@pytest.mark.asyncio
async def test_place_order_confirmed_rejects_unexpected_side_without_broker_call(
    monkeypatch,
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = {"client": 0, "order_client": 0}

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    class FakeOrderClient:
        def __init__(self, client):  # noqa: ARG002
            calls["order_client"] += 1

    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", FakeOrderClient, raising=False
    )

    response = await mod._kiwoom_mock_place_order_impl(
        symbol="005930",
        side="hold",
        quantity=1,
        price=70000,
        exchange="KRX",
        dry_run=False,
    )

    assert response["success"] is False
    assert "buy" in response["error"]
    assert calls == {"client": 0, "order_client": 0}


def _patch_fake_kiwoom_mutation_client(monkeypatch, mod, *, modify=None, cancel=None):
    calls: list[dict[str, Any]] = []

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            calls.append({"client": "from_app_settings"})
            return cls()

    class FakeOrderClient:
        def __init__(self, client):
            calls.append({"order_client": client.__class__.__name__})

        async def modify_order(self, **kwargs):
            calls.append({"method": "modify", **kwargs})
            return modify

        async def cancel_order(self, **kwargs):
            calls.append({"method": "cancel", **kwargs})
            return cancel

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", FakeOrderClient, raising=False
    )
    return calls


@pytest.mark.asyncio
async def test_cancel_order_confirmed_calls_broker_cancel(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_mutation_client(
        monkeypatch,
        mod,
        cancel={"return_code": 0, "return_msg": "정상", "ord_no": "0000999888"},
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        symbol="005930",
        cancel_quantity=1,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is True
    assert response["source"] == "kiwoom"
    assert response["account_mode"] == "kiwoom_mock"
    assert response["dry_run"] is False
    assert response["broker_response"]["ord_no"] == "0000999888"
    cancel_call = next(c for c in calls if c.get("method") == "cancel")
    assert cancel_call["original_order_no"] == "0000111222"
    assert cancel_call["symbol"] == "005930"
    assert cancel_call["cancel_quantity"] == 1


@pytest.mark.asyncio
async def test_cancel_order_confirmed_requires_symbol_and_quantity(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_mutation_client(
        monkeypatch, mod, cancel={"return_code": 0}
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert (
        "symbol" in response["error"].lower()
        or "cancel_quantity" in response["error"].lower()
    )
    assert all(c.get("method") != "cancel" for c in calls)


@pytest.mark.asyncio
async def test_cancel_order_unsupported_broker_response_is_fail_closed(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_mutation_client(
        monkeypatch,
        mod,
        cancel={"return_code": 40, "return_msg": "취소불가"},
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        symbol="005930",
        cancel_quantity=1,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False  # non-zero return_code -> not faked
    assert response["broker_response"]["return_code"] == 40
    assert response["return_msg"] == "취소불가"


@pytest.mark.asyncio
async def test_cancel_order_dry_run_false_without_confirm_blocked(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        symbol="005930",
        cancel_quantity=1,
        dry_run=False,
        confirm=False,
    )
    assert response["success"] is False
    assert "confirm=true" in response["error"].lower()


@pytest.mark.asyncio
async def test_modify_order_confirmed_calls_broker_modify(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_mutation_client(
        monkeypatch,
        mod,
        modify={"return_code": 0, "return_msg": "정상", "ord_no": "0000777666"},
    )
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

    assert response["success"] is True
    assert response["dry_run"] is False
    assert response["broker_response"]["ord_no"] == "0000777666"
    modify_call = next(c for c in calls if c.get("method") == "modify")
    assert modify_call["original_order_no"] == "0000111222"
    assert modify_call["symbol"] == "005930"
    assert modify_call["new_price"] == 72000
    assert modify_call["new_quantity"] == 2


@pytest.mark.asyncio
async def test_modify_order_confirmed_requires_both_amounts(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_mutation_client(
        monkeypatch, mod, modify={"return_code": 0}
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_modify_order"](
        order_id="0000111222",
        symbol="005930",
        new_price=72000,  # new_quantity omitted
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert (
        "new_quantity" in response["error"].lower()
        or "new_price" in response["error"].lower()
    )
    assert all(c.get("method") != "modify" for c in calls)


@pytest.mark.asyncio
async def test_modify_order_unsupported_broker_response_is_fail_closed(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_mutation_client(
        monkeypatch,
        mod,
        modify={"return_code": 40, "return_msg": "정정불가"},
    )
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
    assert response["broker_response"]["return_code"] == 40


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


# ---------------------------------------------------------------------------
# ROB-319: shared broker-response helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("broker_response", "expected"),
    [
        ({"return_code": 0}, True),
        ({"return_code": "0"}, True),
        ({"return_code": False}, False),
        ({"return_code": 0.0}, False),
        ({"return_code": 0.5}, False),
        ({"return_code": -0.5}, False),
        ({"return_code": "0.0"}, False),
        ({"return_code": " 0 "}, False),
        ({"return_code": "+0"}, False),
        ({"return_code": "00"}, False),
        ({}, False),  # fail-closed: missing return_code is NOT success
        ({"return_code": 1}, False),
        ({"return_code": "40"}, False),
        ({"return_code": None}, False),  # fail-closed: None is NOT success
        ({"return_code": ""}, False),
        ({"return_code": "RC9999"}, False),
    ],
)
def test_derive_broker_success(broker_response, expected):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    assert mod._derive_broker_success(broker_response) is expected


# ---------------------------------------------------------------------------
# ROB-319 Hermes review: preview_order positive-amount guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_field", "quantity", "price"),
    [
        ("quantity", 0, 70000),
        ("quantity", -1, 70000),
        ("price", 1, 0),
        ("price", 1, -100),
    ],
)
async def test_preview_order_rejects_non_positive_amounts(
    monkeypatch, bad_field, quantity, price
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    impl_calls = {"count": 0}

    async def fake_impl(**kwargs):
        impl_calls["count"] += 1
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_preview_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_preview_order"](
        symbol="005930",
        side="buy",
        quantity=quantity,
        price=price,
    )

    assert response["success"] is False
    assert bad_field in response["error"].lower()
    assert impl_calls["count"] == 0


@pytest.mark.asyncio
async def test_preview_order_allows_positive_amounts(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    _patch_preflight_success(monkeypatch, mod)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_preview_order"](
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
    )

    assert response["success"] is True
    assert response["preview"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol",
    [
        "A005930",
        "AAPL",
        "",
        "   ",
        "5930",
        "../005930",
        "005930?x",
        "005930.KS",
        "0123G0",
        "００５９３０",
        "٠٠٥٩٣٠",
        "00\n5930",
    ],
)
async def test_registered_symbol_tools_reject_noncanonical_krx_symbols_without_calls(
    monkeypatch, symbol
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls: list[dict[str, Any]] = []

    async def fake_impl(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    for name in (
        "_kiwoom_mock_preview_impl",
        "_kiwoom_mock_place_order_impl",
        "_kiwoom_mock_cancel_impl",
        "_kiwoom_mock_cancel_confirmed_impl",
        "_kiwoom_mock_modify_impl",
        "_kiwoom_mock_modify_confirmed_impl",
        "_kiwoom_mock_orderable_cash_impl",
    ):
        monkeypatch.setattr(mod, name, fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    responses = [
        await mcp.tools["kiwoom_mock_preview_order"](
            symbol=symbol, side="buy", quantity=1, price=70000
        ),
        await mcp.tools["kiwoom_mock_place_order"](
            symbol=symbol, side="buy", quantity=1, price=70000
        ),
        await mcp.tools["kiwoom_mock_place_order"](
            symbol=symbol,
            side="buy",
            quantity=1,
            price=70000,
            dry_run=False,
            confirm=True,
        ),
        await mcp.tools["kiwoom_mock_cancel_order"](
            order_id="0000111222",
            symbol=symbol,
            cancel_quantity=1,
        ),
        await mcp.tools["kiwoom_mock_cancel_order"](
            order_id="0000111222",
            symbol=symbol,
            cancel_quantity=1,
            dry_run=False,
            confirm=True,
        ),
        await mcp.tools["kiwoom_mock_modify_order"](
            order_id="0000111222",
            symbol=symbol,
            new_quantity=1,
            new_price=70000,
        ),
        await mcp.tools["kiwoom_mock_modify_order"](
            order_id="0000111222",
            symbol=symbol,
            new_quantity=1,
            new_price=70000,
            dry_run=False,
            confirm=True,
        ),
        await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol=symbol),
    ]

    assert calls == []
    assert all(response["success"] is False for response in responses)
    assert all("symbol" in response["error"].lower() for response in responses)
    assert responses[-1]["cash"] is None


@pytest.mark.asyncio
async def test_registered_symbol_tools_forward_trimmed_canonical_symbol(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls: list[dict[str, Any]] = []

    async def fake_impl(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_kiwoom_mock_preview_impl", fake_impl)
    monkeypatch.setattr(mod, "_kiwoom_mock_orderable_cash_impl", fake_impl)
    mcp = DummyMCP()
    _register(mcp)

    await mcp.tools["kiwoom_mock_preview_order"](
        symbol=" 005930 ", side="buy", quantity=1, price=70000
    )
    await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol=" 005930 ")

    assert [call["symbol"] for call in calls] == ["005930", "005930"]


# ---------------------------------------------------------------------------
# ROB-319: account read tools call the broker client (no stub-success)
# ---------------------------------------------------------------------------


def _patch_fake_kiwoom_account_client(monkeypatch, mod, payloads):
    """payloads keyed by method name: 'orderable_amount' | 'balance' | 'deposit' | 'order_status'."""

    calls: list[dict[str, Any]] = []

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            calls.append({"client": "from_app_settings"})
            return cls()

    class FakeAccountClient:
        def __init__(self, client):
            calls.append({"account_client": client.__class__.__name__})

        async def get_orderable_amount(self, **kwargs):
            calls.append({"method": "orderable_amount", **kwargs})
            return payloads.get("orderable_amount", {"return_code": 0})

        async def get_deposit(self, **kwargs):
            calls.append({"method": "deposit", **kwargs})
            return payloads.get("deposit", {"return_code": 0})

        async def get_balance(self, **kwargs):
            calls.append({"method": "balance", **kwargs})
            return payloads.get("balance", {"return_code": 0})

        async def get_order_status(self, **kwargs):
            calls.append({"method": "order_status", **kwargs})
            return payloads.get("order_status", {"return_code": 0})

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    return calls


def _patch_preflight_success(monkeypatch, mod) -> None:
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_OK,
        PreflightResult,
    )

    async def fake_preflight(**kwargs):  # noqa: ARG001
        return PreflightResult(
            ok=True,
            error_code=PREFLIGHT_OK,
            checks=[],
            estimated_evidence={"type": "estimated"},
        )

    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", fake_preflight, raising=False
    )


@pytest.mark.asyncio
async def test_orderable_cash_with_symbol_calls_deposit_fallback(monkeypatch):
    # ROB-904 — kt00010 (주문인출가능금액) is unsupported by mockapi (RC7006,
    # ROB-891 4-variant probe). The symbol path now falls back to kt00001
    # (예수금상세현황) and must never dispatch kt00010.
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "deposit": {
                "return_code": 0,
                "return_msg": "정상",
                "ord_alow_amt": "1500000",
            },
            "balance": {"return_code": 0},
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](
        symbol="005930", side="buy", price=70000
    )

    assert response["success"] is True
    assert response["source"] == "kiwoom"
    assert response["account_mode"] == "kiwoom_mock"
    assert response["broker_response"]["ord_alow_amt"] == "1500000"
    assert response["cash"] == 1500000
    assert response["cash_source"] == "deposit_fallback_kt00010_unsupported"
    assert response["symbol"] == "005930"
    assert response["provenance"]["api_id"] == "kt00001"
    assert response["provenance"]["host"] == "mockapi.kiwoom.com"
    # balance/orderable_amount (kt00010) must NOT have been called
    assert all(c.get("method") not in ("balance", "orderable_amount") for c in calls)
    assert any(c.get("method") == "deposit" for c in calls)


@pytest.mark.asyncio
async def test_orderable_cash_with_symbol_side_price_accepted_but_unused(monkeypatch):
    # ROB-904 — symbol/side/price are accepted for call-site backcompat but no
    # longer shape the broker dispatch: the deposit call takes no args derived
    # from them (kt00010's trde_tp/uv are gone).
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "deposit": {
                "return_code": 0,
                "ord_alow_amt": "1500000",
            },
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](
        symbol="005930", side="buy", price=70000
    )

    assert response["success"] is True
    deposit_calls = [c for c in calls if c.get("method") == "deposit"]
    assert len(deposit_calls) == 1
    assert "side" not in deposit_calls[0]
    assert "price" not in deposit_calls[0]
    assert all(c.get("method") != "orderable_amount" for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "price"),
    [
        (None, 70000),
        ("buy", None),
        (None, None),
        ("buy", True),
        ("buy", False),
        ("buy", 1.5),
        ("buy", 70000.0),
        ("buy", "70000"),
    ],
)
async def test_orderable_cash_symbol_path_ignores_missing_or_invalid_side_price(
    monkeypatch, side, price
):
    # ROB-904 — kt00010 is no longer dispatched, so the symbol path no longer
    # requires side/price to be well-formed; it succeeds via kt00001 regardless.
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "deposit": {"return_code": 0, "ord_alow_amt": "1500000"},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](
        symbol="005930", side=side, price=price
    )

    assert response["success"] is True
    assert response["cash"] == 1500000
    assert response["provenance"]["api_id"] == "kt00001"
    assert all(c.get("method") != "orderable_amount" for c in calls)


@pytest.mark.asyncio
async def test_orderable_cash_without_symbol_calls_deposit(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "deposit": {
                "return_code": 0,
                "return_msg": "정상",
                "ord_alow_amt": "987654",
            },
            "balance": {"return_code": 0},
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"]()

    assert response["success"] is True
    assert response["broker_response"]["ord_alow_amt"] == "987654"
    assert response["cash"] == 987654
    assert response["cash_source"] == "deposit"
    assert response["provenance"]["api_id"] == "kt00001"
    assert response["provenance"]["host"] == "mockapi.kiwoom.com"
    assert any(c.get("method") == "deposit" for c in calls)
    assert all(c.get("method") not in ("orderable_amount", "balance") for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "payload", "api_id", "cash_source"),
    [
        (
            "005930",
            {"return_code": 0, "some_unknown_field": "x"},
            "kt00001",
            "deposit_fallback_kt00010_unsupported_unavailable",
        ),
        (
            "005930",
            {"return_code": 0, "ord_alow_amt": "not-a-number"},
            "kt00001",
            "deposit_fallback_kt00010_unsupported_unavailable",
        ),
        (
            "005930",
            {"return_code": 0, "ord_alow_amt": "-1"},
            "kt00001",
            "deposit_fallback_kt00010_unsupported_unavailable",
        ),
        (
            None,
            {"return_code": 0, "some_unknown_field": "x"},
            "kt00001",
            "deposit_unavailable",
        ),
        (
            None,
            {"return_code": 0, "ord_alow_amt": "not-a-number"},
            "kt00001",
            "deposit_unavailable",
        ),
        (
            None,
            {"return_code": 0, "ord_alow_amt": "-1"},
            "kt00001",
            "deposit_unavailable",
        ),
    ],
)
async def test_orderable_cash_unavailable_evidence_fails_closed(
    monkeypatch, symbol, payload, api_id, cash_source
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    payloads = {
        "deposit": payload,
        "balance": {"return_code": 0},
        "order_status": {"return_code": 0},
    }
    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads=payloads,
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](
        symbol=symbol, side="buy", price=70000
    )

    assert response["success"] is False
    assert response["error"] == "kiwoom_mock_evidence_invalid"
    assert response["cash"] is None
    assert response["cash_source"] == cash_source
    assert response["broker_response"] == payload
    assert response["provenance"]["api_id"] == api_id
    assert all(c.get("method") != "orderable_amount" for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "api_id", "cash_source", "return_code"),
    [
        (
            "005930",
            "kt00001",
            "deposit_fallback_kt00010_unsupported_unavailable",
            40,
        ),
        (
            "005930",
            "kt00001",
            "deposit_fallback_kt00010_unsupported_unavailable",
            False,
        ),
        (
            "005930",
            "kt00001",
            "deposit_fallback_kt00010_unsupported_unavailable",
            0.5,
        ),
        (None, "kt00001", "deposit_unavailable", 40),
        (None, "kt00001", "deposit_unavailable", False),
        (None, "kt00001", "deposit_unavailable", 0.5),
    ],
)
async def test_orderable_cash_broker_rejection_has_stable_failure_source(
    monkeypatch, symbol, api_id, cash_source, return_code
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    payloads = {
        "deposit": {
            "return_code": return_code,
            "return_msg": "broker rejected",
        },
        "balance": {"return_code": 0},
        "order_status": {"return_code": 0},
    }
    calls = _patch_fake_kiwoom_account_client(monkeypatch, mod, payloads=payloads)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](
        symbol=symbol, side="buy", price=70000
    )

    assert response["success"] is False
    assert response["error"] == "kiwoom_mock_broker_error"
    assert response["cash"] is None
    assert response["cash_source"] == cash_source
    assert response["provenance"]["api_id"] == api_id
    assert all(c.get("method") != "orderable_amount" for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "api_id"),
    [
        ("005930", "kt00001"),
        (None, "kt00001"),
    ],
)
async def test_orderable_cash_broker_error_is_fail_closed(monkeypatch, symbol, api_id):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccountClient:
        def __init__(self, client):  # noqa: ARG002
            pass

        async def get_orderable_amount(self, **kwargs):  # noqa: ARG002
            raise AssertionError("kt00010 must never be dispatched (ROB-904)")

        async def get_deposit(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("boom")

        async def get_balance(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](
        symbol=symbol, side="buy", price=70000
    )

    assert response["success"] is False
    assert response["error"] == "kiwoom_mock_transport_error"
    assert "RuntimeError" in response["error_detail"]
    assert response["account_mode"] == "kiwoom_mock"
    assert response["cash"] is None
    assert response["provenance"]["api_id"] == api_id


@pytest.mark.asyncio
async def test_get_positions_calls_balance_and_passes_through(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {
                "return_code": 0,
                "return_msg": "정상",
                "acnt_evlt_remn_indv_tot": [
                    {
                        "stk_cd": "A005930",
                        "rmnd_qty": "3",
                        "pur_pric": "72300",
                    }
                ],
            },
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_positions"]()

    assert response["success"] is True
    assert response["source"] == "kiwoom"
    assert (
        response["broker_response"]["acnt_evlt_remn_indv_tot"][0]["stk_cd"] == "A005930"
    )
    assert response["positions"] == [
        {
            "symbol": "005930",
            "quantity": 3,
            "average_price": 72300,
            "currency": "KRW",
        }
    ]
    assert response["provenance"]["api_id"] == "kt00018"
    assert response["provenance"]["environment"] == "mock"
    assert any(c.get("method") == "balance" for c in calls)


@pytest.mark.asyncio
async def test_get_order_history_calls_order_status_with_pagination(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {"return_code": 0},
            "order_status": {
                "return_code": 0,
                "return_msg": "정상",
                "continuation": {"cont_yn": "Y", "next_key": "page-2"},
                "acnt_ord_cntr_prst_array": [
                    {
                        "ord_no": "1112222",
                        "stk_cd": "A005930",
                        "ord_qty": "3",
                        "ord_uv": "72300",
                        "cntr_qty": "1",
                        "cntr_uv": "72200",
                        "mdfy_cncl_tp": "",
                    }
                ],
            },
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_order_history"](
        cont_yn="Y", next_key="page-1"
    )

    assert response["success"] is True
    assert (
        response["broker_response"]["acnt_ord_cntr_prst_array"][0]["ord_no"]
        == "1112222"
    )
    assert response["orders"] == [
        {
            "order_id": "1112222",
            "symbol": "005930",
            "status": "partially_filled",
            "ordered_price": 72300,
            "filled_quantity": 1,
            "average_price": 72200,
            "remaining_quantity": 2,
        }
    ]
    assert response["provenance"]["api_id"] == "kt00009"
    assert response["continuation"] == {"cont_yn": "Y", "next_key": "page-2"}
    status_call = next(c for c in calls if c.get("method") == "order_status")
    assert status_call["cont_yn"] == "Y"
    assert status_call["next_key"] == "page-1"


@pytest.mark.asyncio
async def test_get_positions_broker_error_is_fail_closed(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccountClient:
        def __init__(self, client):  # noqa: ARG002
            pass

        async def get_balance(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("balance boom")

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_positions"]()

    assert response["success"] is False
    assert response["error"] == "kiwoom_mock_transport_error"
    assert "RuntimeError" in response["error_detail"]


@pytest.mark.asyncio
async def test_get_order_history_fails_closed_on_live_provenance(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {"return_code": 0},
            "order_status": {
                "return_code": 0,
                "provenance": {
                    "environment": "live",
                    "host": "api.kiwoom.com",
                },
                "acnt_ord_cntr_prst_array": [],
            },
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_order_history"]()

    assert response["success"] is False
    assert response["orders"] == []
    assert response["error"] == "kiwoom_mock_provenance_conflict"
    assert response["account_mode"] == "kiwoom_mock"


@pytest.mark.asyncio
async def test_get_positions_fails_closed_on_malformed_row(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {
                "return_code": 0,
                "acnt_evlt_remn_indv_tot": [
                    {"stk_cd": "A005930", "rmnd_qty": "invalid"}
                ],
            },
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_positions"]()

    assert response["success"] is False
    assert response["positions"] == []
    assert response["error"] == "kiwoom_mock_evidence_invalid"


def _assert_stable_read_failure(
    response: dict[str, Any],
    *,
    result_key: str,
    api_id: str,
    error: str,
) -> None:
    assert response["success"] is False
    assert response[result_key] == []
    assert response["error"] == error
    assert response["source"] == "kiwoom"
    assert response["account_mode"] == "kiwoom_mock"
    assert response["provenance"] == {
        "broker": "kiwoom",
        "environment": "mock",
        "account_mode": "kiwoom_mock",
        "host": "mockapi.kiwoom.com",
        "api_id": api_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "api_id"),
    [
        ("005930", "kt00001"),
        (None, "kt00001"),
    ],
)
async def test_orderable_cash_both_branches_fail_closed_on_live_provenance(
    monkeypatch, symbol, api_id
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    payloads = {
        "deposit": {
            "return_code": 0,
            "ord_alow_amt": "987654",
            "provenance": {"environment": "live"},
        },
        "balance": {"return_code": 0},
        "order_status": {"return_code": 0},
    }
    calls = _patch_fake_kiwoom_account_client(monkeypatch, mod, payloads=payloads)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](
        symbol=symbol, side="buy", price=70000
    )
    assert all(c.get("method") != "orderable_amount" for c in calls)

    assert response["success"] is False
    assert response["cash"] is None
    assert response["error"] == "kiwoom_mock_provenance_conflict"
    assert response["provenance"]["api_id"] == api_id
    assert response["provenance"]["host"] == "mockapi.kiwoom.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflicting_provenance",
    [
        {"source": "kiwoom_prod"},
        {"source": "kiwoom_production"},
        {"source": "kiwoom_real"},
        {"source": "kiwoom_live"},
        {"broker": "kiwoom-prod"},
        {"broker": "kiwoom-production"},
        {"broker": "kiwoom-real"},
        {"broker": "kiwoom-live"},
        {"environment": "prod"},
        {"environment": "production"},
        {"environment": "real"},
        {"is_mock": "false"},
        {"is_mock": 0},
        {"is_mock": "maybe"},
        {"environment": {}},
        {"accountMode": "kiwoom_live"},
        {"account-mode": "kiwoom_live"},
        {"isMock": False},
        {"is-mock": False},
        {"baseUrl": "https://api.kiwoom.com"},
        {"base-url": "https://api.kiwoom.com"},
    ],
)
async def test_registered_order_history_rejects_non_mock_or_malformed_provenance(
    monkeypatch, conflicting_provenance
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {"return_code": 0},
            "order_status": {
                "return_code": 0,
                "provenance": conflicting_provenance,
                "acnt_ord_cntr_prst_array": [],
            },
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_order_history"]()

    _assert_stable_read_failure(
        response,
        result_key="orders",
        api_id="kt00009",
        error="kiwoom_mock_provenance_conflict",
    )


@pytest.mark.asyncio
async def test_registered_order_history_recursively_redacts_aliases_and_passthrough(
    monkeypatch,
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    sensitive = {
        "api_key": "api-underscore-value",
        "API-Key": "api-value",
        "Authorization_Header": "auth-value",
        "refresh-token": "refresh-value",
        "App Key": "app-key-value",
        "APP-SECRET": "app-secret-value",
        "Account Identifier": "account-value",
        "Cookie": "cookie-value",
        "Credential": "credential-value",
        "Password": "password-value",
        "Passwd": "passwd-value",
        "approval": "approval-value",
    }
    safe_false_positives = {
        "tokenizer_version": "preserve-tokenizer",
        "secretary_name": "preserve-secretary",
        "accounting_note": "preserve-accounting",
        "credentialed_role": "preserve-role",
        "disapproval_reason": "preserve-reason",
    }
    payload = {
        "return_code": 0,
        "continuation": {
            "cont_yn": "Y",
            "next_key": "page-2",
            "authorization_header": "nested-secret",
        },
        "metadata": {**sensitive, **safe_false_positives},
        "acnt_ord_cntr_prst_array": [],
    }
    _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {"return_code": 0},
            "order_status": payload,
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_order_history"]()

    redacted_metadata = response["broker_response"]["metadata"]
    assert set(redacted_metadata.values()) == {
        "[REDACTED]",
        *safe_false_positives.values(),
    }
    for key in sensitive:
        assert redacted_metadata[key] == "[REDACTED]"
    for key, value in safe_false_positives.items():
        assert redacted_metadata[key] == value
    assert response["continuation"]["authorization_header"] == "[REDACTED]"
    assert response["broker_response"]["continuation"]["authorization_header"] == (
        "[REDACTED]"
    )
    assert payload["metadata"] == {**sensitive, **safe_false_positives}
    assert payload["continuation"]["authorization_header"] == "nested-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "result_key", "api_id"),
    [
        ("kiwoom_mock_get_positions", "positions", "kt00018"),
        ("kiwoom_mock_get_order_history", "orders", "kt00009"),
    ],
)
async def test_registered_reads_config_failure_has_stable_envelope(
    monkeypatch, tool_name, result_key, api_id
):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_enabled", False)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools[tool_name]()

    _assert_stable_read_failure(
        response,
        result_key=result_key,
        api_id=api_id,
        error="kiwoom_mock_config_invalid",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "api_id", "cash_source"),
    [
        (
            "005930",
            "kt00001",
            "deposit_fallback_kt00010_unsupported_unavailable",
        ),
        (None, "kt00001", "deposit_unavailable"),
    ],
)
async def test_registered_cash_config_failure_has_stable_source(
    monkeypatch, symbol, api_id, cash_source
):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_enabled", False)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol=symbol)

    assert response["success"] is False
    assert response["cash"] is None
    assert response["error"] == "kiwoom_mock_config_invalid"
    assert response["cash_source"] == cash_source
    assert response["provenance"]["api_id"] == api_id
    if symbol is not None:
        assert response["symbol"] == symbol


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "result_key", "api_id"),
    [
        ("kiwoom_mock_get_positions", "positions", "kt00018"),
        ("kiwoom_mock_get_order_history", "orders", "kt00009"),
    ],
)
async def test_registered_reads_transport_exception_has_stable_envelope(
    monkeypatch, tool_name, result_key, api_id
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FailingAccountClient:
        def __init__(self, client):  # noqa: ARG002
            pass

        async def get_balance(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("transport secret must not escape")

        async def get_order_status(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("transport secret must not escape")

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient)
    monkeypatch.setattr(mod, "KiwoomDomesticAccountClient", FailingAccountClient)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools[tool_name]()

    _assert_stable_read_failure(
        response,
        result_key=result_key,
        api_id=api_id,
        error="kiwoom_mock_transport_error",
    )
    assert "transport secret" not in str(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "result_key", "api_id", "payload_key"),
    [
        ("kiwoom_mock_get_positions", "positions", "kt00018", "balance"),
        ("kiwoom_mock_get_order_history", "orders", "kt00009", "order_status"),
    ],
)
async def test_registered_reads_broker_failure_has_stable_envelope(
    monkeypatch, tool_name, result_key, api_id, payload_key
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    payloads = {
        "orderable_amount": {"return_code": 0},
        "balance": {"return_code": 0},
        "order_status": {"return_code": 0},
    }
    payloads[payload_key] = {"return_code": 1, "return_msg": "broker failure"}
    _patch_fake_kiwoom_account_client(monkeypatch, mod, payloads=payloads)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools[tool_name]()

    _assert_stable_read_failure(
        response,
        result_key=result_key,
        api_id=api_id,
        error="kiwoom_mock_broker_error",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_order_id", ["x", "   ", "12 34", "../123", "123?x=1"])
async def test_registered_order_history_rejects_malformed_official_order_id(
    monkeypatch, bad_order_id
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {"return_code": 0},
            "order_status": {
                "return_code": 0,
                "acnt_ord_cntr_prst_array": [
                    {
                        "ord_no": bad_order_id,
                        "stk_cd": "A005930",
                        "ord_qty": "1",
                        "ord_uv": "70000",
                        "cntr_qty": "0",
                        "cntr_uv": "0",
                        "mdfy_cncl_tp": "",
                    }
                ],
            },
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_order_history"]()

    _assert_stable_read_failure(
        response,
        result_key="orders",
        api_id="kt00009",
        error="kiwoom_mock_evidence_invalid",
    )


# ---------------------------------------------------------------------------
# ROB-893 — preview/place shared preflight parity + mutation-boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_failure_returns_stable_error_code(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_SELLABLE_EXCEEDED,
        PreflightCheck,
        PreflightResult,
    )

    async def failing_preflight(**_kwargs):
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_SELLABLE_EXCEEDED,
            error_detail="Requested 10 exceeds sellable 5 for 005930",
            checks=[PreflightCheck("sellable", False, "requested=10, sellable=5")],
        )

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", failing_preflight, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_preview_order"](
        symbol="005930",
        side="sell",
        quantity=10,
        price=70000,
    )

    assert response["success"] is False
    assert response["preview"] is True
    assert response["error"] == PREFLIGHT_SELLABLE_EXCEEDED
    assert "sellable" in response["error_detail"]
    assert response["preflight_checks"][0]["name"] == "sellable"
    assert response["preflight_checks"][0]["ok"] is False


@pytest.mark.asyncio
async def test_place_order_dry_run_returns_stable_failure_without_post(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_CASH_INSUFFICIENT,
        PreflightResult,
    )

    order_calls: list[str] = []

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FailingAccountClient:
        def __init__(self, _client):
            pass

        async def get_balance(self, **_kwargs):
            return {"return_code": 0}

        async def get_orderable_amount(self, **_kwargs):
            return {"return_code": 0}

        async def get_deposit(self, **_kwargs):
            return {"return_code": 0}

    class FailingOrderClient:
        def __init__(self, _client):
            pass

        async def place_buy_order(self, **_kwargs):
            order_calls.append("buy")
            return {"return_code": 0}

        async def place_sell_order(self, **_kwargs):
            order_calls.append("sell")
            return {"return_code": 0}

    async def failing_preflight(**_kwargs):
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_CASH_INSUFFICIENT,
            error_detail="insufficient cash",
            checks=[],
        )

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FailingAccountClient, raising=False
    )
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", FailingOrderClient, raising=False
    )
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", failing_preflight, raising=False
    )
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
    assert response["error"] == PREFLIGHT_CASH_INSUFFICIENT
    assert order_calls == [], "broker POST must not happen after preflight failure"


@pytest.mark.asyncio
async def test_place_order_confirmed_runs_single_preflight_right_before_post(
    monkeypatch,
):
    """ROB-893 v2: confirmed place runs preflight EXACTLY ONCE.

    The single preflight is the mutation-boundary check, run immediately before
    POST on the SAME shared client/auth/token. Exactly one client is constructed
    (via ``_new_kiwoom_mock_client``) and reused across preflight + POST.
    """
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_OK,
        PreflightCheck,
        PreflightResult,
    )

    preflight_call_count = 0
    client_construction_count = 0

    async def counting_preflight(**_kwargs):
        nonlocal preflight_call_count
        preflight_call_count += 1
        return PreflightResult(
            ok=True,
            error_code=PREFLIGHT_OK,
            checks=[PreflightCheck(f"preflight_call_{preflight_call_count}", True)],
            estimated_evidence={
                "type": "estimated",
                "preflight_call": preflight_call_count,
            },
        )

    class FakeKiwoomMockClient:
        pass

    class SuccessOrderClient:
        def __init__(self, _client):
            pass

        async def place_buy_order(self, **_kwargs):
            return {"return_code": 0, "return_msg": "정상", "ord_no": "0000111222"}

        async def place_sell_order(self, **_kwargs):
            return {"return_code": 0, "return_msg": "정상", "ord_no": "0000333444"}

    class SuccessAccountClient:
        def __init__(self, _client):
            pass

        async def get_balance(self, **_kwargs):
            return {"return_code": 0}

        async def get_orderable_amount(self, **_kwargs):
            return {"return_code": 0}

        async def get_deposit(self, **_kwargs):
            return {"return_code": 0}

    def counting_client_factory():
        nonlocal client_construction_count
        client_construction_count += 1
        return FakeKiwoomMockClient()

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", counting_client_factory)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", SuccessAccountClient, raising=False
    )
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", SuccessOrderClient, raising=False
    )
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", counting_preflight, raising=False
    )
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

    assert response["success"] is True
    assert preflight_call_count == 1, (
        "confirmed place must run preflight exactly once (the single "
        "mutation-boundary check shared across preflight + POST)"
    )
    assert client_construction_count == 1, (
        "confirmed place must construct exactly one client, shared across "
        "preflight + POST"
    )
    assert response["preflight_checks"] == [
        {"name": "preflight_call_1", "ok": True, "detail": None}
    ]
    assert response["estimated_evidence"] == {
        "type": "estimated",
        "preflight_call": 1,
    }


@pytest.mark.asyncio
async def test_place_order_confirmed_blocks_when_preflight_fails(monkeypatch):
    """ROB-893 v2: there is exactly ONE preflight; if it fails, POST count is 0.

    The old 2-phase (initial ok then final recheck fail) was removed. The single
    preflight is the sole mutation-boundary check; failing it fail-closes.
    """
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_CASH_INSUFFICIENT,
        PreflightCheck,
        PreflightResult,
    )

    order_calls: list[dict[str, Any]] = []
    preflight_calls = 0
    client_construction_count = 0

    class FakeKiwoomMockClient:
        pass

    class FakeOrderClient:
        def __init__(self, _client):
            pass

        async def place_buy_order(self, **kwargs):
            order_calls.append({"method": "buy", **kwargs})
            return {"return_code": 0, "ord_no": "must-not-submit"}

        async def place_sell_order(self, **kwargs):
            order_calls.append({"method": "sell", **kwargs})
            return {"return_code": 0, "ord_no": "must-not-submit"}

    async def failing_preflight(**_kwargs):
        nonlocal preflight_calls
        preflight_calls += 1
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_CASH_INSUFFICIENT,
            error_detail="insufficient cash for confirmed POST",
            checks=[PreflightCheck("cash", False)],
        )

    def counting_client_factory():
        nonlocal client_construction_count
        client_construction_count += 1
        return FakeKiwoomMockClient()

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", counting_client_factory)
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", FakeOrderClient, raising=False
    )
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", failing_preflight, raising=False
    )
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

    assert preflight_calls == 1
    assert response["success"] is False
    assert response["error"] == PREFLIGHT_CASH_INSUFFICIENT
    assert order_calls == [], "broker POST must not happen after preflight failure"
    # Client is still constructed once (eager, to share across preflight + POST),
    # even though preflight failed before POST.
    assert client_construction_count == 1


@pytest.mark.asyncio
async def test_preview_dry_run_zero_mutation(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_OK,
        PreflightResult,
    )

    order_calls: list[str] = []
    account_calls: list[str] = []

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class TrackingAccountClient:
        def __init__(self, _client):
            pass

        async def get_balance(self, **_kwargs):
            account_calls.append("balance")
            return {"return_code": 0, "acnt_evlt_remn_indv_tot": []}

        async def get_orderable_amount(self, **_kwargs):
            account_calls.append("orderable_amount")
            return {"return_code": 0, "ord_alowa": "1000000"}

        async def get_deposit(self, **_kwargs):
            account_calls.append("deposit")
            return {"return_code": 0, "ord_alow_amt": "1000000"}

    class TrackingOrderClient:
        def __init__(self, _client):
            pass

        async def place_buy_order(self, **_kwargs):
            order_calls.append("buy")
            return {"return_code": 0}

        async def place_sell_order(self, **_kwargs):
            order_calls.append("sell")
            return {"return_code": 0}

    async def success_preflight(**_kwargs):
        return PreflightResult(ok=True, error_code=PREFLIGHT_OK, checks=[])

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", TrackingAccountClient, raising=False
    )
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", TrackingOrderClient, raising=False
    )
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", success_preflight, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    preview_response = await mcp.tools["kiwoom_mock_preview_order"](
        symbol="005930", side="buy", quantity=1, price=70000
    )
    dry_response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930", side="buy", quantity=1, price=70000, dry_run=True
    )

    assert preview_response["success"] is True
    assert preview_response["preview"] is True
    assert dry_response["success"] is True
    assert order_calls == [], "preview/dry_run must not POST to broker"


@pytest.mark.asyncio
async def test_preview_and_place_normalize_with_same_error_code(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_SELLABLE_EXCEEDED,
        PreflightResult,
    )

    class FakeKiwoomMockClient:
        pass

    async def failing_preflight(**_kwargs):
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_SELLABLE_EXCEEDED,
            error_detail="Requested 10 exceeds sellable 5 for 005930",
            checks=[],
        )

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", lambda: FakeKiwoomMockClient())
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", failing_preflight, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    preview = await mcp.tools["kiwoom_mock_preview_order"](
        symbol="005930", side="sell", quantity=10, price=70000
    )
    place = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930", side="sell", quantity=10, price=70000
    )

    assert preview["error"] == PREFLIGHT_SELLABLE_EXCEEDED
    assert place["error"] == PREFLIGHT_SELLABLE_EXCEEDED
    assert preview["error_detail"] == place["error_detail"]


@pytest.mark.asyncio
async def test_loss_sell_not_blocked_in_mcp_layer(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_OK,
        PreflightCheck,
        PreflightResult,
    )

    async def loss_sell_preflight(**_kwargs):
        return PreflightResult(
            ok=True,
            error_code=PREFLIGHT_OK,
            checks=[
                PreflightCheck("quote_freshness", True),
                PreflightCheck("tick_valid", True),
                PreflightCheck("sellable", True, "sellable=10"),
            ],
            estimated_evidence={
                "type": "estimated",
                "loss_sell": True,
                "estimated_gross_pnl": -150000,
                "estimated_gross_pnl_pct": -21.43,
                "estimated_net_pnl": None,
            },
            warnings=["estimated_costs_unavailable", "estimated_loss_sell"],
        )

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", loss_sell_preflight, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_preview_order"](
        symbol="005930", side="sell", quantity=5, price=70000
    )

    assert response["success"] is True
    assert response["estimated_evidence"]["loss_sell"] is True
    assert response["estimated_evidence"]["estimated_gross_pnl"] == -150000
    assert response["estimated_evidence"]["estimated_net_pnl"] is None
    assert response["preflight_warnings"] == [
        "estimated_costs_unavailable",
        "estimated_loss_sell",
    ]


# ---------------------------------------------------------------------------
# ROB-893 v2: request-scoped client reuse + structured pre-dispatch error
#
# Confirmed place must build ONE client, mint ONE cold token, run ONE preflight
# (the mutation-boundary check), and POST once — all on the same client/auth.
# KiwoomPreDispatchError carries redacted structured fields (stage/api_id/
# cause_type); post-dispatch failures require reconciliation (Oracle Q6).
# ---------------------------------------------------------------------------


def _patch_lifecycle_fakes(
    monkeypatch,
    mod,
    *,
    preflight_fn=None,
    order_buy_exc: Exception | None = None,
    order_sell_exc: Exception | None = None,
    order_buy_response: dict[str, Any] | None = None,
    order_sell_response: dict[str, Any] | None = None,
):
    """Wire up the full place-lifecycle with counting fakes.

    Returns a dict of counters: ``client_constructions``, ``preflight_calls``,
    ``order_calls``.
    """
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_OK,
        PreflightResult,
    )

    counters = {
        "client_constructions": 0,
        "preflight_calls": 0,
        "order_calls": [],
    }

    class FakeKiwoomMockClient:
        pass

    class FakeAccountClient:
        def __init__(self, _client):
            pass

        async def get_balance(self, **_kwargs):
            return {"return_code": 0}

        async def get_orderable_amount(self, **_kwargs):
            return {"return_code": 0, "ord_alowa": "1000000"}

        async def get_deposit(self, **_kwargs):
            return {"return_code": 0}

    class FakeOrderClient:
        def __init__(self, _client):
            pass

        async def place_buy_order(self, **kwargs):
            counters["order_calls"].append({"method": "buy", **kwargs})
            if order_buy_exc is not None:
                raise order_buy_exc
            return order_buy_response or {
                "return_code": 0,
                "return_msg": "정상",
                "ord_no": "0000111222",
            }

        async def place_sell_order(self, **kwargs):
            counters["order_calls"].append({"method": "sell", **kwargs})
            if order_sell_exc is not None:
                raise order_sell_exc
            return order_sell_response or {
                "return_code": 0,
                "return_msg": "정상",
                "ord_no": "0000333444",
            }

    def counting_client_factory():
        counters["client_constructions"] += 1
        return FakeKiwoomMockClient()

    if preflight_fn is None:

        async def default_preflight(**_kwargs):
            return PreflightResult(ok=True, error_code=PREFLIGHT_OK, checks=[])

        preflight_fn = default_preflight

    async def counting_preflight(**kwargs):
        counters["preflight_calls"] += 1
        return await preflight_fn(**kwargs)

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", counting_client_factory)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", FakeOrderClient, raising=False
    )
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", counting_preflight, raising=False
    )
    return counters


# === A. Reproduction / client reuse (structural) ===


@pytest.mark.asyncio
async def test_confirmed_place_reuses_single_client_for_preflight_and_post(monkeypatch):
    """Confirmed place: ONE client constructed, shared across preflight + POST."""
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    counters = _patch_lifecycle_fakes(monkeypatch, mod)
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

    assert response["success"] is True
    assert counters["client_constructions"] == 1
    assert counters["preflight_calls"] == 1
    assert len(counters["order_calls"]) == 1
    assert counters["order_calls"][0]["method"] == "buy"


@pytest.mark.asyncio
async def test_confirmed_place_cold_cache_token_issued_once():
    """Behavioral (Oracle Q5): one cold-cache token mint across preflight + POST.

    Uses a REAL KiwoomMockClient + KiwoomAuthClient with fake httpx transports.
    Does NOT use ``set_transport_for_test`` (which sets ``_token_override`` and
    bypasses auth) — we WANT auth to run so we can count token-mint HTTP calls.
    """
    import datetime as dt

    import httpx

    from app.services.brokers.kiwoom import constants
    from app.services.brokers.kiwoom.client import KiwoomMockClient
    from app.services.brokers.kiwoom.domestic_account import (
        KiwoomDomesticAccountClient,
    )
    from app.services.brokers.kiwoom.domestic_orders import KiwoomDomesticOrderClient

    expires = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).strftime("%Y%m%d%H%M%S")
    mint_count = 0

    def oauth_handler(request):  # noqa: ARG001
        nonlocal mint_count
        mint_count += 1
        return httpx.Response(
            200,
            json={"return_code": 0, "token": "tok-1", "expires_dt": expires},
        )

    def tr_handler(request):
        api_id = request.headers.get(constants.HEADER_API_ID, "")
        if api_id == constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID:
            return httpx.Response(200, json={"return_code": 0, "ord_alowa": "1000000"})
        return httpx.Response(
            200,
            json={"return_code": 0, "return_msg": "정상", "ord_no": "0000111222"},
        )

    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="k",
        app_secret="s",
        account_no="a",
    )
    client._transport = httpx.MockTransport(tr_handler)
    client._auth._transport = httpx.MockTransport(oauth_handler)

    account_client = KiwoomDomesticAccountClient(client)
    order_client = KiwoomDomesticOrderClient(client)

    await account_client.get_orderable_amount(symbol="005930", side="buy", price=70000)
    await order_client.place_buy_order(
        symbol="005930", quantity=1, price=70000, exchange="KRX"
    )

    assert mint_count == 1, (
        "cold-cache token must be minted exactly once across preflight + POST"
    )


# === B. Mutation-boundary counts ===


@pytest.mark.asyncio
async def test_dry_run_zero_mutations(monkeypatch):
    """Dry-run place: exactly one preflight, zero POST."""
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    counters = _patch_lifecycle_fakes(monkeypatch, mod)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="buy",
        quantity=1,
        price=70000,
        dry_run=True,
    )

    assert response["success"] is True
    assert response["dry_run"] is True
    assert counters["preflight_calls"] == 1
    assert counters["order_calls"] == [], "dry_run must not POST to broker"


@pytest.mark.asyncio
async def test_confirmed_one_preflight_one_post(monkeypatch):
    """Confirmed place: exactly one preflight, exactly one POST."""
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    counters = _patch_lifecycle_fakes(monkeypatch, mod)
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

    assert response["success"] is True
    assert counters["preflight_calls"] == 1
    assert len(counters["order_calls"]) == 1


# === C. Fail-closed: POST count 0 on every pre-dispatch failure ===


@pytest.mark.asyncio
async def test_token_resolution_failure_blocks_post(monkeypatch):
    """KiwoomPreDispatchError(stage=token_resolution) → not_submitted, no reconcile."""
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.client import KiwoomPreDispatchError

    exc = KiwoomPreDispatchError(
        stage="token_resolution",
        api_id="kt10000",
        cause_type="RuntimeError",
    )
    counters = _patch_lifecycle_fakes(monkeypatch, mod, order_buy_exc=exc)
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
    assert response["status"] == "not_submitted"
    assert response["dispatch_started"] is False
    assert response["reconcile_required"] is False
    assert response["stage"] == "token_resolution"
    assert response["api_id"] == "kt10000"
    assert response["cause_type"] == "RuntimeError"
    assert "RuntimeError" in response["error"]
    assert len(counters["order_calls"]) == 1, "POST was attempted once then failed"


@pytest.mark.asyncio
async def test_request_build_failure_blocks_post(monkeypatch):
    """KiwoomPreDispatchError(stage=request_build) → not_submitted."""
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.client import KiwoomPreDispatchError

    exc = KiwoomPreDispatchError(
        stage="request_build",
        api_id="kt10001",
        cause_type="ValueError",
    )
    counters = _patch_lifecycle_fakes(monkeypatch, mod, order_sell_exc=exc)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert response["status"] == "not_submitted"
    assert response["dispatch_started"] is False
    assert response["reconcile_required"] is False
    assert response["stage"] == "request_build"
    assert response["api_id"] == "kt10001"
    assert response["cause_type"] == "ValueError"
    assert len(counters["order_calls"]) == 1


@pytest.mark.asyncio
async def test_post_dispatch_unknown_failure_requires_reconcile(monkeypatch):
    """Oracle Q6: generic post-dispatch failure → acceptance_uncertain + reconcile.

    The raw exception message must NOT surface; only ``type(exc).__name__``.
    """
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    counters = _patch_lifecycle_fakes(
        monkeypatch, mod, order_buy_exc=RuntimeError("boom")
    )
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
    assert response["status"] == "acceptance_uncertain"
    assert response["reconcile_required"] is True
    assert response["retry_allowed"] is False
    assert "RuntimeError" in response["error"]
    assert "boom" not in response["error"], "raw exception message must not surface"
    assert len(counters["order_calls"]) == 1


# === E. Structured stage classification + redaction ===


def _patch_real_confirmed_sell_client(monkeypatch, mod, *, stage: str | None = None):
    import datetime as dt

    import httpx

    from app.services.brokers.kiwoom import constants
    from app.services.brokers.kiwoom.client import KiwoomMockClient

    order_transport_calls = {"count": 0, "api_ids": []}
    token_mint_calls = {"count": 0}

    def broker_handler(request: httpx.Request) -> httpx.Response:
        order_transport_calls["count"] += 1
        api_id = request.headers.get(constants.HEADER_API_ID, "")
        order_transport_calls["api_ids"].append(api_id)
        if api_id == constants.ACCOUNT_BALANCE_API_ID:
            return httpx.Response(
                200,
                json={
                    "return_code": 0,
                    "acnt_evlt_remn_indv_tot": [
                        {
                            "stk_cd": "005930",
                            "rmnd_qty": "10",
                            "pur_pric": "70000",
                        }
                    ],
                },
            )
        if api_id == constants.ORDER_SELL_API_ID:
            return httpx.Response(
                200,
                json={"return_code": 0, "return_msg": "정상", "ord_no": "777000111"},
            )
        raise AssertionError(f"unexpected api_id={api_id!r}")

    expires = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).strftime("%Y%m%d%H%M%S")

    def oauth_handler(_request: httpx.Request) -> httpx.Response:
        token_mint_calls["count"] += 1
        if stage == "token_resolution":
            raise RuntimeError(
                "token=super-secret-token authorization=Bearer leaked-token"
            )
        return httpx.Response(
            200,
            json={"return_code": 0, "token": "tok-1", "expires_dt": expires},
        )

    client = KiwoomMockClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="k",
        app_secret="s",
        account_no="12345678",
    )
    client._transport = httpx.MockTransport(broker_handler)
    client._auth._transport = httpx.MockTransport(oauth_handler)

    if stage == "pre_dispatch_hook":

        async def fail_before_dispatch(_api_id: str) -> None:
            raise RuntimeError(
                "app_secret=hidden-secret account_no=12345678 body=raw-request-body"
            )

        client._before_api_dispatch = fail_before_dispatch  # type: ignore[method-assign]
    elif stage == "request_build":
        original_build_request = httpx.AsyncClient.build_request

        def fail_build_request(self, method, url, **kwargs):
            if "/api/dostk/" not in str(url):
                return original_build_request(self, method, url, **kwargs)
            del self, method, url, kwargs
            raise ValueError("body=raw-request-body token=super-secret-token")

        monkeypatch.setattr(httpx.AsyncClient, "build_request", fail_build_request)
    elif stage == "host_validation":
        original_build_request = httpx.AsyncClient.build_request

        def wrong_host_build_request(self, method, url, **kwargs):
            if "/api/dostk/" not in str(url):
                return original_build_request(self, method, url, **kwargs)
            del self, url
            return httpx.Request(
                method,
                "https://api.kiwoom.com/api/dostk/acnt",
                headers=kwargs.get("headers"),
                json=kwargs.get("json"),
            )

        monkeypatch.setattr(
            httpx.AsyncClient, "build_request", wrong_host_build_request
        )

    async def fake_quote(_symbol: str) -> tuple[int | None, str]:
        return 70000, "fresh"

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_fetch_kr_quote_for_preflight", fake_quote)
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", lambda: client)
    return order_transport_calls, token_mint_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    ["token_resolution", "pre_dispatch_hook", "request_build", "host_validation"],
)
async def test_structured_stage_classified_explicitly(stage):
    """Each of the 4 valid stages is carried verbatim into the response."""
    from app.services.brokers.kiwoom.client import KiwoomPreDispatchError

    exc = KiwoomPreDispatchError(
        stage=stage, api_id="kt10000", cause_type="RuntimeError"
    )
    from app.mcp_server.tooling.orders_kiwoom_variants import _not_submitted_response

    result = _not_submitted_response(
        {"source": "kiwoom", "account_mode": "kiwoom_mock"}, exc
    )

    assert result["stage"] == stage
    assert result["api_id"] == "kt10000"
    assert result["cause_type"] == "RuntimeError"
    assert result["dispatch_started"] is False
    assert result["status"] == "not_submitted"
    assert result["reconcile_required"] is False


@pytest.mark.asyncio
async def test_pre_dispatch_error_redacts_secrets():
    """The structured response must never surface secrets from __cause__."""
    from app.mcp_server.tooling.orders_kiwoom_variants import _not_submitted_response
    from app.services.brokers.kiwoom.client import KiwoomPreDispatchError

    sensitive_msg = (
        "token=super-secret-token authorization=Bearer leaked-token "
        "app_secret=hidden-secret account_no=99999999 body=raw-request-body"
    )
    cause = RuntimeError(sensitive_msg)
    exc = KiwoomPreDispatchError(
        stage="token_resolution", api_id="kt00018", cause_type="RuntimeError"
    )
    exc.__cause__ = cause

    response = _not_submitted_response(
        {
            "source": "kiwoom",
            "account_mode": "kiwoom_mock",
            "symbol": "005930",
        },
        exc,
    )

    assert response["stage"] == "token_resolution"
    assert response["api_id"] == "kt00018"
    assert response["cause_type"] == "RuntimeError"

    dumped = str(response)
    for secret in [
        "super-secret-token",
        "leaked-token",
        "hidden-secret",
        "99999999",
        "raw-request-body",
        "Bearer",
        "authorization",
        "app_secret",
        "body=",
    ]:
        assert secret not in dumped, f"secret {secret!r} leaked into response"


# === F. Regression guards ===


@pytest.mark.asyncio
async def test_confirmed_sell_runs_real_balance_preflight_before_post(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom import constants

    order_transport_calls, token_mint_calls = _patch_real_confirmed_sell_client(
        monkeypatch, mod
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="sell",
        quantity=5,
        price=70000,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is True
    assert response["dry_run"] is False
    assert response["estimated_evidence"]["sellable_quantity"] == 10
    assert order_transport_calls["api_ids"] == [
        constants.ACCOUNT_BALANCE_API_ID,
        constants.ORDER_SELL_API_ID,
    ]
    assert order_transport_calls["count"] == 2
    assert token_mint_calls["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    ["token_resolution", "pre_dispatch_hook", "request_build", "host_validation"],
)
async def test_confirmed_sell_pre_dispatch_failures_preserve_structured_diagnostics(
    monkeypatch, stage
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom import constants

    order_transport_calls, token_mint_calls = _patch_real_confirmed_sell_client(
        monkeypatch, mod, stage=stage
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="sell",
        quantity=1,
        price=70000,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert response["status"] == "not_submitted"
    assert response["dispatch_started"] is False
    assert response["reconcile_required"] is False
    assert response["stage"] == stage
    assert response["api_id"] == constants.ACCOUNT_BALANCE_API_ID
    assert response["cause_type"] in {"RuntimeError", "ValueError"}
    assert order_transport_calls["count"] == 0
    assert token_mint_calls["count"] <= 1
    dumped = str(response)
    for secret in [
        "super-secret-token",
        "leaked-token",
        "hidden-secret",
        "12345678",
        "raw-request-body",
        "Bearer",
        "authorization",
        "app_secret",
        "body=",
    ]:
        assert secret not in dumped


@pytest.mark.asyncio
async def test_preview_pre_dispatch_failure_preserves_structured_diagnostics(
    monkeypatch,
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom import constants

    order_transport_calls, token_mint_calls = _patch_real_confirmed_sell_client(
        monkeypatch, mod, stage="pre_dispatch_hook"
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_preview_order"](
        symbol="005930", side="sell", quantity=1, price=70000
    )

    assert response["success"] is False
    assert response["preview"] is True
    assert response["status"] == "not_submitted"
    assert response["dispatch_started"] is False
    assert response["reconcile_required"] is False
    assert response["stage"] == "pre_dispatch_hook"
    assert response["api_id"] == constants.ACCOUNT_BALANCE_API_ID
    assert order_transport_calls["count"] == 0
    assert token_mint_calls["count"] == 1


@pytest.mark.asyncio
async def test_standalone_position_read_then_confirmed_sell_succeeds(monkeypatch):
    """Standalone position read (1 client/1 dispatch) + confirmed SELL on the
    new shared-client path both succeed. The position impl still calls
    ``KiwoomMockClient.from_app_settings()`` directly (left untouched); the
    place impl calls ``_new_kiwoom_mock_client`` once."""
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_OK,
        PreflightCheck,
        PreflightResult,
    )

    place_client_constructions = 0

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakePlaceClient:
        pass

    class FakeAccountClient:
        def __init__(self, _client):
            pass

        async def get_balance(self, **_kwargs):
            return {
                "return_code": 0,
                "acnt_evlt_remn_indv_tot": [
                    {"stk_cd": "005930", "rmnd_qty": "10", "pur_pric": "70000"}
                ],
            }

        async def get_orderable_amount(self, **_kwargs):
            return {"return_code": 0}

        async def get_deposit(self, **_kwargs):
            return {"return_code": 0}

    class FakeOrderClient:
        def __init__(self, _client):
            pass

        async def place_sell_order(self, **_kwargs):
            return {"return_code": 0, "return_msg": "정상", "ord_no": "0000555666"}

        async def place_buy_order(self, **_kwargs):
            return {"return_code": 0, "ord_no": "unused"}

    async def sell_ok_preflight(**_kwargs):
        return PreflightResult(
            ok=True,
            error_code=PREFLIGHT_OK,
            checks=[PreflightCheck("sellable", True, "sellable=10")],
        )

    def counting_place_client_factory():
        nonlocal place_client_constructions
        place_client_constructions += 1
        return FakePlaceClient()

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", FakeOrderClient, raising=False
    )
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", counting_place_client_factory)
    monkeypatch.setattr(
        mod, "_run_preflight_for_kiwoom_mock", sell_ok_preflight, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    positions_response = await mcp.tools["kiwoom_mock_get_positions"]()
    assert positions_response["success"] is True

    sell_response = await mcp.tools["kiwoom_mock_place_order"](
        symbol="005930",
        side="sell",
        quantity=5,
        price=71000,
        dry_run=False,
        confirm=True,
    )

    assert sell_response["success"] is True
    assert sell_response["dry_run"] is False
    assert place_client_constructions == 1
