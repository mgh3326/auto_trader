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


# ---------------------------------------------------------------------------
# ROB-319: account read tools call the broker client (no stub-success)
# ---------------------------------------------------------------------------


def _patch_fake_kiwoom_account_client(monkeypatch, mod, payloads):
    """payloads keyed by method name: 'orderable_amount' | 'balance' | 'order_status'."""

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
            return payloads["orderable_amount"]

        async def get_balance(self, **kwargs):
            calls.append({"method": "balance", **kwargs})
            return payloads["balance"]

        async def get_order_status(self, **kwargs):
            calls.append({"method": "order_status", **kwargs})
            return payloads["order_status"]

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    return calls


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
                "ord_psbl_cash": "1500000",
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
    assert response["broker_response"]["ord_psbl_cash"] == "1500000"
    assert response["cash"] == 1500000
    assert response["cash_source"] == "orderable_amount"
    assert response["symbol"] == "005930"
    # balance must NOT have been called
    assert all(c.get("method") != "balance" for c in calls)


@pytest.mark.asyncio
async def test_orderable_cash_without_symbol_calls_balance(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {"return_code": 0, "return_msg": "정상", "entr": "987654"},
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"]()

    assert response["success"] is True
    assert response["broker_response"]["entr"] == "987654"
    assert response["cash"] == 987654
    assert response["cash_source"] == "balance"
    assert any(c.get("method") == "balance" for c in calls)
    assert all(c.get("method") != "orderable_amount" for c in calls)


@pytest.mark.asyncio
async def test_orderable_cash_unparseable_returns_null_cash_with_source(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0, "some_unknown_field": "x"},
            "balance": {"return_code": 0},
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol="005930")

    assert response["success"] is True  # broker returned 0
    assert response["cash"] is None
    assert response["cash_source"] == "orderable_amount_unparsed"
    assert response["broker_response"]["some_unknown_field"] == "x"


@pytest.mark.asyncio
async def test_orderable_cash_broker_error_is_fail_closed(monkeypatch):
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

        async def get_balance(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol="005930")

    assert response["success"] is False
    assert "RuntimeError" in response["error"]
    assert response["account_mode"] == "kiwoom_mock"


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
    assert "RuntimeError" in response["error"]


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
