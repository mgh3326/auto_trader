from __future__ import annotations

from typing import Any

import pytest


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str = ""):
        del description

        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def _tools() -> dict[str, Any]:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    mcp = DummyMCP()
    module.register(mcp)
    return mcp.tools


def test_registers_exact_seven_us_tools() -> None:
    from app.mcp_server.tooling.orders_kiwoom_us_variants import (
        KIWOOM_MOCK_US_MUTATION_TOOL_NAMES,
        KIWOOM_MOCK_US_READ_TOOL_NAMES,
        KIWOOM_MOCK_US_TOOL_NAMES,
    )

    assert set(_tools()) == KIWOOM_MOCK_US_TOOL_NAMES
    assert len(KIWOOM_MOCK_US_TOOL_NAMES) == 7
    assert KIWOOM_MOCK_US_READ_TOOL_NAMES.isdisjoint(KIWOOM_MOCK_US_MUTATION_TOOL_NAMES)


@pytest.mark.asyncio
async def test_rejects_advanced_trde_tp_before_lookup_or_client(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls = {"lookup": 0, "client": 0}

    async def fake_lookup(symbol: str) -> str:
        del symbol
        calls["lookup"] += 1
        return "NASD"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)

    result = await _tools()["kiwoom_mock_us_place_order"](
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=200.0,
        trde_tp="26",
        dry_run=False,
        confirm=True,
    )

    assert result["success"] is False
    assert result["error_code"] == "unsupported_trde_tp"
    assert result["rejected_trde_tp"] == "26"
    assert result["supported_trde_tp"] == ["00", "03"]
    assert calls == {"lookup": 0, "client": 0}


@pytest.mark.asyncio
async def test_preview_resolves_db_exchange_and_renders_payload(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    async def fake_lookup(symbol: str) -> str:
        assert symbol == "NVDA"
        return "NASDAQ"

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)

    result = await _tools()["kiwoom_mock_us_preview_order"](
        symbol="NVDA", side="buy", quantity=2, trde_tp="00", price=213.04
    )

    assert result["success"] is True
    assert result["stex_tp"] == "ND"
    assert result["request_body"] == {
        "stex_tp": "ND",
        "stk_cd": "NVDA",
        "ord_qty": "2",
        "ord_uv": "213.04",
        "trde_tp": "00",
    }
    assert str(result["requested_notional"]) == "426.08"


@pytest.mark.asyncio
async def test_limit_requires_price_and_market_rejects_price(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    tools = _tools()
    limit = await tools["kiwoom_mock_us_preview_order"](
        symbol="NVDA", side="buy", quantity=1, trde_tp="00", price=None
    )
    market = await tools["kiwoom_mock_us_preview_order"](
        symbol="NVDA", side="buy", quantity=1, trde_tp="03", price=1.0
    )
    assert limit["success"] is False
    assert market["success"] is False


@pytest.mark.asyncio
async def test_confirmed_limit_resolves_exchange_and_calls_broker(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls: list[dict[str, Any]] = []

    async def fake_lookup(symbol: str) -> str:
        assert symbol == "NVDA"
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            calls.append({"client": type(client).__name__})

        async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"return_code": 0, "ord_no": "000000282"}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsOrderClient", FakeOrders)

    result = await _tools()["kiwoom_mock_us_place_order"](
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=213.04,
        trde_tp="00",
        dry_run=False,
        confirm=True,
    )

    assert result["success"] is True
    assert result["account_mode"] == "kiwoom_mock_us"
    assert calls[-1]["stex_tp"] == "ND"
    assert calls[-1]["trde_tp"] == "00"


@pytest.mark.asyncio
async def test_live_place_without_confirm_never_constructs_client(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls = {"client": 0}

    async def fake_lookup(symbol: str) -> str:
        del symbol
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    result = await _tools()["kiwoom_mock_us_place_order"](
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=1.0,
        trde_tp="00",
        dry_run=False,
        confirm=False,
    )
    assert result["success"] is False
    assert calls["client"] == 0


@pytest.mark.asyncio
async def test_unsupported_exchange_and_unsafe_id_stop_before_client(
    monkeypatch,
) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls = {"client": 0}

    async def fake_lookup(symbol: str) -> str:
        return "OTC" if symbol == "OTCM" else "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    preview = await _tools()["kiwoom_mock_us_preview_order"](
        symbol="OTCM", side="buy", quantity=1, price=1.0, trde_tp="00"
    )
    cancel = await _tools()["kiwoom_mock_us_cancel_order"](
        order_id="../282", symbol="NVDA", dry_run=False, confirm=True
    )
    assert preview["success"] is False
    assert cancel["success"] is False
    assert calls["client"] == 0


@pytest.mark.asyncio
async def test_modify_and_cancel_do_not_invent_quantity(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls: list[dict[str, Any]] = []

    async def fake_lookup(symbol: str) -> str:
        del symbol
        return "NYSE"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            del client

        async def modify_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"return_code": 0, "ord_no": "000000284"}

        async def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"return_code": 0, "ord_no": "000000285"}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsOrderClient", FakeOrders)
    tools = _tools()
    await tools["kiwoom_mock_us_modify_order"](
        order_id="000000282",
        symbol="TSM",
        new_price=100.0,
        dry_run=False,
        confirm=True,
    )
    await tools["kiwoom_mock_us_cancel_order"](
        order_id="000000284", symbol="TSM", dry_run=False, confirm=True
    )
    assert all("quantity" not in call for call in calls)
    assert calls[0]["new_price"] == 100.0


@pytest.mark.asyncio
async def test_orderable_cash_is_labeled_as_deposit(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_us_deposit_detail(self) -> dict[str, Any]:
            return {"return_code": 0, "d0_usd_fx_entr": "1234.5000"}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsAccountClient", FakeAccount)

    result = await _tools()["kiwoom_mock_us_get_orderable_cash"]()
    assert result["cash"] == "1234.5000"
    assert result["currency"] == "USD"
    assert result["cash_semantics"] == "deposit_not_broker_orderable"
    assert result["orderable_quantity_supported"] is False


@pytest.mark.asyncio
async def test_history_scope_selects_open_or_today(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls: list[str] = []

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_open_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            calls.append("open")
            return {"return_code": 0, "result_list": []}

        async def get_today_orders(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            calls.append("today")
            return {"return_code": 0, "result_list": []}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsAccountClient", FakeAccount)

    tools = _tools()
    await tools["kiwoom_mock_us_get_order_history"](scope="open")
    await tools["kiwoom_mock_us_get_order_history"](scope="today")
    assert calls == ["open", "today"]


@pytest.mark.asyncio
async def test_disabled_config_reports_only_us_keys(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    monkeypatch.setattr(
        module,
        "validate_kiwoom_mock_us_config",
        lambda: ["KIWOOM_MOCK_US_APP_KEY"],
    )
    result = await _tools()["kiwoom_mock_us_get_positions"]()
    assert result["success"] is False
    assert "KIWOOM_MOCK_US_APP_KEY" in result["error"]
    assert "KIWOOM_MOCK_APP_KEY" not in result["error"]


@pytest.mark.asyncio
async def test_unparseable_deposit_is_null_and_nonzero_is_failure(
    monkeypatch,
) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            del client

        async def get_us_deposit_detail(self) -> dict[str, Any]:
            return {
                "return_code": 20,
                "return_msg": "RC9000",
                "d0_usd_fx_entr": "invalid",
            }

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsAccountClient", FakeAccount)
    result = await _tools()["kiwoom_mock_us_get_orderable_cash"]()
    assert result["success"] is False
    assert result["cash"] is None
    assert result["cash_source"] == "ust21160.d0_usd_fx_entr_unparsed"


@pytest.mark.asyncio
async def test_confirmed_order_carries_us_mock_provenance(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    async def fake_lookup(symbol: str) -> str:
        del symbol
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            del client

        async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {"return_code": 0, "ord_no": "000000282"}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsOrderClient", FakeOrders)

    result = await _tools()["kiwoom_mock_us_place_order"](
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=213.04,
        trde_tp="00",
        dry_run=False,
        confirm=True,
    )

    assert result["provenance"] == {
        "broker": "kiwoom",
        "environment": "mock",
        "account_mode": "kiwoom_mock_us",
        "host": "mockapi.kiwoom.com",
        "api_id": "ust20000",
    }


@pytest.mark.asyncio
async def test_spoofed_live_provenance_in_broker_payload_fails_closed(
    monkeypatch,
) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    async def fake_lookup(symbol: str) -> str:
        del symbol
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            del client

        async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            return {
                "return_code": 0,
                "ord_no": "000000282",
                "environment": "live",
            }

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsOrderClient", FakeOrders)

    result = await _tools()["kiwoom_mock_us_place_order"](
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=213.04,
        trde_tp="00",
        dry_run=False,
        confirm=True,
    )

    assert result["success"] is False
    assert result["error_code"] == "kiwoom_mock_provenance_conflict"
    assert "provenance" not in result


@pytest.mark.asyncio
async def test_nan_price_and_nan_new_price_fail_before_any_client(
    monkeypatch,
) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls = {"client": 0}

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    async def fake_lookup(symbol: str) -> str:
        del symbol
        return "NASDAQ"

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    tools = _tools()

    preview = await tools["kiwoom_mock_us_preview_order"](
        symbol="NVDA", side="buy", quantity=1, trde_tp="00", price=float("nan")
    )
    assert preview["success"] is False

    modify = await tools["kiwoom_mock_us_modify_order"](
        order_id="000000282",
        symbol="NVDA",
        new_price=float("nan"),
        dry_run=True,
    )
    assert modify["success"] is False
    assert calls["client"] == 0
