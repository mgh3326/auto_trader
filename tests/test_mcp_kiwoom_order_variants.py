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
async def test_orderable_cash_with_symbol_calls_orderable_amount(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {
                "return_code": 0,
                "return_msg": "정상",
                "ord_alowa": "1500000",
            },
            "balance": {"return_code": 0},
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol="005930")

    assert response["success"] is True
    assert response["source"] == "kiwoom"
    assert response["account_mode"] == "kiwoom_mock"
    assert response["broker_response"]["ord_alowa"] == "1500000"
    assert response["cash"] == 1500000
    assert response["cash_source"] == "orderable_amount"
    assert response["symbol"] == "005930"
    assert response["provenance"]["api_id"] == "kt00010"
    assert response["provenance"]["host"] == "mockapi.kiwoom.com"
    # balance/deposit must NOT have been called
    assert all(c.get("method") not in ("balance", "deposit") for c in calls)


@pytest.mark.asyncio
async def test_orderable_cash_with_symbol_side_price_sends_trde_tp_and_uv(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {
                "return_code": 0,
                "ord_alowa": "1500000",
            },
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    await mcp.tools["kiwoom_mock_get_orderable_cash"](
        symbol="005930", side="buy", price=70000
    )

    orderable_calls = [c for c in calls if c.get("method") == "orderable_amount"]
    assert len(orderable_calls) == 1
    assert orderable_calls[0]["side"] == "buy"
    assert orderable_calls[0]["price"] == 70000


@pytest.mark.asyncio
async def test_orderable_cash_without_symbol_calls_deposit(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "deposit": {"return_code": 0, "return_msg": "정상", "ord_alow_amt": "987654"},
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
    ("symbol", "payload_key", "payload", "api_id", "cash_source"),
    [
        (
            "005930",
            "orderable_amount",
            {"return_code": 0, "some_unknown_field": "x"},
            "kt00010",
            "orderable_amount_unavailable",
        ),
        (
            "005930",
            "orderable_amount",
            {"return_code": 0, "ord_alowa": "not-a-number"},
            "kt00010",
            "orderable_amount_unavailable",
        ),
        (
            "005930",
            "orderable_amount",
            {"return_code": 0, "ord_alowa": "-1"},
            "kt00010",
            "orderable_amount_unavailable",
        ),
        (
            None,
            "deposit",
            {"return_code": 0, "some_unknown_field": "x"},
            "kt00001",
            "deposit_unavailable",
        ),
        (
            None,
            "deposit",
            {"return_code": 0, "ord_alow_amt": "not-a-number"},
            "kt00001",
            "deposit_unavailable",
        ),
        (
            None,
            "deposit",
            {"return_code": 0, "ord_alow_amt": "-1"},
            "kt00001",
            "deposit_unavailable",
        ),
    ],
)
async def test_orderable_cash_unavailable_evidence_fails_closed(
    monkeypatch, symbol, payload_key, payload, api_id, cash_source
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    payloads = {
        "orderable_amount": {"return_code": 0, "ord_alowa": "1500000"},
        "deposit": {"return_code": 0, "ord_alow_amt": "987654"},
        "balance": {"return_code": 0},
        "order_status": {"return_code": 0},
    }
    payloads[payload_key] = payload
    _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads=payloads,
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol=symbol)

    assert response["success"] is False
    assert response["error"] == "kiwoom_mock_evidence_invalid"
    assert response["cash"] is None
    assert response["cash_source"] == cash_source
    assert response["broker_response"] == payload
    assert response["provenance"]["api_id"] == api_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "payload_key", "api_id", "cash_source", "return_code"),
    [
        ("005930", "orderable_amount", "kt00010", "orderable_amount_unavailable", 40),
        (
            "005930",
            "orderable_amount",
            "kt00010",
            "orderable_amount_unavailable",
            False,
        ),
        ("005930", "orderable_amount", "kt00010", "orderable_amount_unavailable", 0.5),
        (None, "deposit", "kt00001", "deposit_unavailable", 40),
        (None, "deposit", "kt00001", "deposit_unavailable", False),
        (None, "deposit", "kt00001", "deposit_unavailable", 0.5),
    ],
)
async def test_orderable_cash_broker_rejection_has_stable_failure_source(
    monkeypatch, symbol, payload_key, api_id, cash_source, return_code
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    payloads = {
        "orderable_amount": {"return_code": 0, "ord_alowa": "1500000"},
        "deposit": {"return_code": 0, "ord_alow_amt": "987654"},
        "balance": {"return_code": 0},
        "order_status": {"return_code": 0},
    }
    payloads[payload_key] = {
        "return_code": return_code,
        "return_msg": "broker rejected",
    }
    _patch_fake_kiwoom_account_client(monkeypatch, mod, payloads=payloads)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol=symbol)

    assert response["success"] is False
    assert response["error"] == "kiwoom_mock_broker_error"
    assert response["cash"] is None
    assert response["cash_source"] == cash_source
    assert response["provenance"]["api_id"] == api_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "api_id"),
    [
        ("005930", "kt00010"),
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
            raise RuntimeError("boom")

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

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol=symbol)

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
    ("symbol", "payload_key", "api_id"),
    [
        ("005930", "orderable_amount", "kt00010"),
        (None, "deposit", "kt00001"),
    ],
)
async def test_orderable_cash_both_branches_fail_closed_on_live_provenance(
    monkeypatch, symbol, payload_key, api_id
):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    payloads = {
        "orderable_amount": {"return_code": 0, "ord_alowa": "1500000"},
        "deposit": {"return_code": 0, "ord_alow_amt": "987654"},
        "balance": {"return_code": 0},
        "order_status": {"return_code": 0},
    }
    payloads[payload_key]["provenance"] = {"environment": "live"}
    _patch_fake_kiwoom_account_client(monkeypatch, mod, payloads=payloads)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol=symbol)

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
        ("005930", "kt00010", "orderable_amount_unavailable"),
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
async def test_place_order_confirmed_reruns_preflight_right_before_post(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from app.services.brokers.kiwoom.order_preflight import (
        PREFLIGHT_OK,
        PreflightResult,
    )

    preflight_call_count = 0

    async def counting_preflight(**_kwargs):
        nonlocal preflight_call_count
        preflight_call_count += 1
        return PreflightResult(ok=True, error_code=PREFLIGHT_OK, checks=[])

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

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

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
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
    assert preflight_call_count == 2, (
        "confirmed place must run preflight twice (once before, once right before POST)"
    )


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

    async def failing_preflight(**_kwargs):
        return PreflightResult(
            ok=False,
            error_code=PREFLIGHT_SELLABLE_EXCEEDED,
            error_detail="Requested 10 exceeds sellable 5 for 005930",
            checks=[],
        )

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
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
                "estimated_pnl": -150000,
                "estimated_pnl_pct": -21.43,
            },
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
    assert response["estimated_evidence"]["estimated_pnl"] == -150000
