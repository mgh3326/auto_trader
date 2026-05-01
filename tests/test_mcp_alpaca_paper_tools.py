from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.alpaca_paper import (
    ALPACA_PAPER_READONLY_TOOL_NAMES,
    alpaca_paper_get_account,
    alpaca_paper_get_cash,
    alpaca_paper_get_order,
    alpaca_paper_list_assets,
    alpaca_paper_list_fills,
    alpaca_paper_list_orders,
    alpaca_paper_list_positions,
    reset_alpaca_paper_service_factory,
    set_alpaca_paper_service_factory,
)
from app.mcp_server.tooling.alpaca_paper_preview import (
    _FORBIDDEN_SERVICE_METHODS,
    alpaca_paper_preview_order,
    reset_alpaca_paper_preview_service_factory,
    set_alpaca_paper_preview_service_factory,
)
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.registry import register_all_tools
from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError
from app.services.brokers.alpaca.schemas import (
    AccountSnapshot,
    Asset,
    CashBalance,
    Fill,
    Order,
    Position,
)
from tests._mcp_tooling_support import DummyMCP


class FakeAlpacaPaperService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.submit_called: bool = False

    async def get_account(self) -> AccountSnapshot:
        self.calls.append(("get_account", {}))
        return AccountSnapshot(
            id="paper-account",
            buying_power=Decimal("200000"),
            cash=Decimal("100000"),
            portfolio_value=Decimal("100000"),
            status="ACTIVE",
        )

    async def get_cash(self) -> CashBalance:
        self.calls.append(("get_cash", {}))
        return CashBalance(cash=Decimal("100000"), buying_power=Decimal("200000"))

    async def list_positions(self) -> list[Position]:
        self.calls.append(("list_positions", {}))
        return [
            Position(
                asset_id="asset-aapl",
                symbol="AAPL",
                qty=Decimal("2"),
                avg_entry_price=Decimal("180.12"),
                current_price=Decimal("190.50"),
                market_value=Decimal("381.00"),
                unrealized_pl=Decimal("20.76"),
                side="long",
            )
        ]

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[Order]:
        self.calls.append(("list_orders", {"status": status, "limit": limit}))
        return [
            Order(
                id="order-1",
                client_order_id="client-1",
                symbol="AAPL",
                qty=Decimal("1"),
                filled_qty=Decimal("0"),
                side="buy",
                type="limit",
                time_in_force="day",
                status="open",
                limit_price=Decimal("180"),
            )
        ]

    async def get_order(self, order_id: str) -> Order:
        self.calls.append(("get_order", {"order_id": order_id}))
        return Order(
            id=order_id,
            symbol="MSFT",
            qty=Decimal("1"),
            filled_qty=Decimal("1"),
            side="buy",
            type="market",
            time_in_force="day",
            status="filled",
            filled_avg_price=Decimal("420.50"),
        )

    async def list_assets(
        self,
        *,
        status: str | None = None,
        asset_class: str | None = None,
    ) -> list[Asset]:
        self.calls.append(
            ("list_assets", {"status": status, "asset_class": asset_class})
        )
        return [
            Asset(
                id="asset-aapl",
                symbol="AAPL",
                name="Apple Inc.",
                status="active",
                tradable=True,
                asset_class="us_equity",
            )
        ]

    async def submit_order(self, request: Any) -> None:
        self.submit_called = True
        self.calls.append(("submit_order", {"request": request}))
        raise AssertionError("submit_order must not be called on the preview path")

    async def cancel_order(self, order_id: str) -> None:
        self.calls.append(("cancel_order", {"order_id": order_id}))
        raise AssertionError("cancel_order must not be called on the preview path")

    async def list_fills(
        self, *, after=None, until=None, limit: int | None = None
    ) -> list[Fill]:
        self.calls.append(
            ("list_fills", {"after": after, "until": until, "limit": limit})
        )
        return [
            Fill(
                id="fill-1",
                activity_type="FILL",
                symbol="AAPL",
                qty=Decimal("1"),
                price=Decimal("180.00"),
                side="buy",
                order_id="order-1",
            )
        ]


@pytest.fixture
def fake_service() -> FakeAlpacaPaperService:
    service = FakeAlpacaPaperService()
    set_alpaca_paper_service_factory(lambda: service)  # type: ignore[arg-type]
    yield service
    reset_alpaca_paper_service_factory()


@pytest.mark.unit
def test_registers_explicit_alpaca_paper_readonly_tools_default_profile() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]
    assert ALPACA_PAPER_READONLY_TOOL_NAMES <= mcp.tools.keys()


@pytest.mark.unit
def test_registers_explicit_alpaca_paper_readonly_tools_paper_profile() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.HERMES_PAPER_KIS)  # type: ignore[arg-type]
    assert ALPACA_PAPER_READONLY_TOOL_NAMES <= mcp.tools.keys()


@pytest.mark.unit
def test_no_alpaca_live_or_mutating_alpaca_order_tools_registered() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]

    forbidden_names = {
        "alpaca_live_get_account",
        "alpaca_live_list_orders",
        "alpaca_paper_place_order",
        "alpaca_paper_replace_order",
        "alpaca_paper_modify_order",
    }
    assert forbidden_names.isdisjoint(mcp.tools.keys())
    assert {name for name in mcp.tools if name.startswith("alpaca_live_")} == set()
    assert {
        name
        for name in mcp.tools
        if name.startswith("alpaca_paper_")
        and any(verb in name for verb in ("place", "replace", "modify"))
    } == set()


@pytest.mark.unit
def test_existing_generic_order_tools_are_not_alpaca_tools() -> None:
    # ROB-69 adds explicit read-only Alpaca paper names only; it does not route
    # Alpaca through legacy generic order mutation tools.
    assert not any(name.startswith("alpaca_") for name in ORDER_TOOL_NAMES)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_account_handler_uses_mocked_service(
    fake_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_get_account()
    assert payload["success"] is True
    assert payload["account_mode"] == "alpaca_paper"
    assert payload["account"]["status"] == "ACTIVE"
    assert payload["account"]["cash"] == "100000"
    assert fake_service.calls == [("get_account", {})]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_cash_handler_uses_mocked_service(
    fake_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_get_cash()
    assert payload["cash"] == {"cash": "100000", "buying_power": "200000"}
    assert fake_service.calls == [("get_cash", {})]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_positions_handler_serializes_positions(
    fake_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_list_positions()
    assert payload["count"] == 1
    assert payload["positions"][0]["symbol"] == "AAPL"
    assert payload["positions"][0]["qty"] == "2"
    assert fake_service.calls == [("list_positions", {})]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_orders_handler_forwards_filters(
    fake_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_list_orders(status="open", limit=10)
    assert payload["count"] == 1
    assert payload["orders"][0]["id"] == "order-1"
    assert fake_service.calls == [("list_orders", {"status": "open", "limit": 10})]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_order_handler_requires_non_blank_id(
    fake_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="order_id is required"):
        await alpaca_paper_get_order("   ")
    assert fake_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_order_handler_forwards_trimmed_id(
    fake_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_get_order(" order-123 ")
    assert payload["order"]["id"] == "order-123"
    assert fake_service.calls == [("get_order", {"order_id": "order-123"})]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_assets_handler_forwards_status_and_class(
    fake_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_list_assets(status="active", asset_class="crypto")
    assert payload["count"] == 1
    assert payload["assets"][0]["class"] == "us_equity"
    assert fake_service.calls == [
        ("list_assets", {"status": "active", "asset_class": "crypto"})
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_fills_handler_parses_window_and_forwards_limit(
    fake_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_list_fills(
        after="2026-01-01T00:00:00Z",
        until="2026-01-02T00:00:00+00:00",
        limit=10,
    )
    assert payload["count"] == 1
    assert payload["fills"][0]["id"] == "fill-1"
    call = fake_service.calls[0]
    assert call[0] == "list_fills"
    assert call[1]["limit"] == 10
    assert call[1]["after"].isoformat() == "2026-01-01T00:00:00+00:00"
    assert call[1]["until"].isoformat() == "2026-01-02T00:00:00+00:00"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_validation_happens_before_service_call(
    fake_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await alpaca_paper_list_orders(limit=0)
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await alpaca_paper_list_fills(limit=0)
    assert fake_service.calls == []


# ---------------------------------------------------------------------------
# ROB-70: alpaca_paper_preview_order tests
# ---------------------------------------------------------------------------


class FakeAlpacaPaperServiceWithCashError(FakeAlpacaPaperService):
    async def get_cash(self) -> CashBalance:
        self.calls.append(("get_cash", {}))
        raise AlpacaPaperRequestError("connection failed")


@pytest.fixture
def fake_preview_service() -> FakeAlpacaPaperService:
    service = FakeAlpacaPaperService()
    set_alpaca_paper_preview_service_factory(lambda: service)  # type: ignore[arg-type]
    yield service
    reset_alpaca_paper_preview_service_factory()


@pytest.fixture
def fake_preview_service_with_cash_error() -> FakeAlpacaPaperServiceWithCashError:
    service = FakeAlpacaPaperServiceWithCashError()
    set_alpaca_paper_preview_service_factory(lambda: service)  # type: ignore[arg-type]
    yield service
    reset_alpaca_paper_preview_service_factory()


# --- 1. Registration / discoverability ---


@pytest.mark.unit
def test_registers_alpaca_paper_preview_tool_default_profile() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]
    assert "alpaca_paper_preview_order" in mcp.tools


@pytest.mark.unit
def test_registers_alpaca_paper_preview_tool_paper_profile() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.HERMES_PAPER_KIS)  # type: ignore[arg-type]
    assert "alpaca_paper_preview_order" in mcp.tools


@pytest.mark.unit
def test_preview_tool_description_documents_dry_run_and_no_submit() -> None:
    from app.mcp_server.tooling.alpaca_paper_preview import (
        register_alpaca_paper_preview_tools,
    )

    class _DescCapture:
        def __init__(self) -> None:
            self.descriptions: dict[str, str] = {}

        def tool(self, name: str, description: str):
            self.descriptions[name] = description

            def decorator(func):
                return func

            return decorator

    cap = _DescCapture()
    register_alpaca_paper_preview_tools(cap)  # type: ignore[arg-type]
    desc = cap.descriptions["alpaca_paper_preview_order"]
    assert "preview" in desc.lower()
    assert any(
        kw in desc.lower()
        for kw in ("no submit", "does not submit", "side-effect-free", "side effects")
    )


# --- 2. Forbidden-write absence ---


@pytest.mark.unit
def test_no_alpaca_paper_place_or_replace_or_modify_tools() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]
    forbidden = {
        "alpaca_paper_preview_submit",
        "alpaca_paper_order_submit",
        "alpaca_paper_replace",
        "alpaca_paper_modify",
        "alpaca_paper_place_order",
        "alpaca_paper_cancel_all_orders",
        "alpaca_paper_cancel_orders",
    }
    assert forbidden.isdisjoint(mcp.tools.keys())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_handler_never_calls_service_submit_or_cancel(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    await alpaca_paper_preview_order(
        symbol="AAPL",
        side="buy",
        type="market",
        qty=Decimal("1"),
    )
    assert fake_preview_service.submit_called is False
    method_names = [c[0] for c in fake_preview_service.calls]
    assert "submit_order" not in method_names
    assert "cancel_order" not in method_names


@pytest.mark.unit
def test_forbidden_service_methods_constant_covers_submit_and_cancel() -> None:
    assert "submit_order" in _FORBIDDEN_SERVICE_METHODS
    assert "cancel_order" in _FORBIDDEN_SERVICE_METHODS


# --- 3. Validation (no service call) ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_invalid_side(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError):
        await alpaca_paper_preview_order(
            symbol="AAPL", side="hold", type="market", qty=Decimal("1")
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_invalid_order_type(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError):
        await alpaca_paper_preview_order(
            symbol="AAPL", side="buy", type="stop_limit", qty=Decimal("1")
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_non_positive_qty(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError):
        await alpaca_paper_preview_order(
            symbol="AAPL", side="buy", type="market", qty=Decimal("0")
        )
    with pytest.raises(ValueError):
        await alpaca_paper_preview_order(
            symbol="AAPL", side="buy", type="market", qty=Decimal("-1")
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_non_positive_notional(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError):
        await alpaca_paper_preview_order(
            symbol="AAPL", side="buy", type="market", notional=Decimal("0")
        )
    with pytest.raises(ValueError):
        await alpaca_paper_preview_order(
            symbol="AAPL", side="buy", type="market", notional=Decimal("-1")
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_both_qty_and_notional(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await alpaca_paper_preview_order(
            symbol="AAPL",
            side="buy",
            type="market",
            qty=Decimal("1"),
            notional=Decimal("100"),
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_neither_qty_nor_notional(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await alpaca_paper_preview_order(symbol="AAPL", side="buy", type="market")
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_limit_without_limit_price(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="limit_price is required"):
        await alpaca_paper_preview_order(
            symbol="AAPL", side="buy", type="limit", qty=Decimal("1")
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_market_with_limit_price(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="limit_price is not allowed"):
        await alpaca_paper_preview_order(
            symbol="AAPL",
            side="buy",
            type="market",
            qty=Decimal("1"),
            limit_price=Decimal("100"),
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_notional_with_limit_type(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="notional is not supported"):
        await alpaca_paper_preview_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            notional=Decimal("100"),
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_blank_symbol(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="blank"):
        await alpaca_paper_preview_order(
            symbol="   ", side="buy", type="market", qty=Decimal("1")
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"symbol": "TOO-LONG-SYMBOL"}, "1-10 characters"),
        ({"qty": Decimal("Infinity")}, "finite number"),
        ({"qty": Decimal("1000001")}, "maximum allowed"),
        ({"notional": Decimal("NaN"), "qty": None}, "finite number"),
        ({"notional": Decimal("10000001"), "qty": None}, "maximum allowed"),
        ({"time_in_force": "opg"}, "time_in_force"),
        ({"client_order_id": "   "}, "client_order_id"),
        ({"client_order_id": "x" * 49}, "client_order_id"),
        ({"asset_class": "option"}, "asset_class"),
        ({"type": "limit", "limit_price": Decimal("0")}, "limit_price"),
    ],
)
async def test_preview_rejects_additional_invalid_inputs_before_service_call(
    fake_preview_service: FakeAlpacaPaperService,
    kwargs: dict[str, object],
    message: str,
) -> None:
    base: dict[str, object] = {
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": Decimal("1"),
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        await alpaca_paper_preview_order(**base)
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_crypto_limit_notional_buy_returns_normalized_echo(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_preview_order(
        symbol="btc/usd",
        side="BUY",
        type="LIMIT",
        notional=Decimal("10"),
        limit_price=Decimal("50000"),
        time_in_force="gtc",
        asset_class="crypto",
    )

    assert payload["preview"] is True
    assert payload["submitted"] is False
    req = payload["order_request"]
    assert req["symbol"] == "BTC/USD"
    assert req["side"] == "buy"
    assert req["type"] == "limit"
    assert req["notional"] == "10"
    assert req["limit_price"] == "50000"
    assert req["time_in_force"] == "gtc"
    assert req["asset_class"] == "crypto"
    assert payload["estimated_cost"] == "10"
    assert payload["would_exceed_buying_power"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_crypto_omitted_time_in_force_defaults_to_gtc(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_preview_order(
        symbol="BTC/USD",
        side="buy",
        type="limit",
        notional=Decimal("10"),
        limit_price=Decimal("50000"),
        asset_class="crypto",
    )

    assert payload["order_request"]["time_in_force"] == "gtc"
    assert fake_preview_service.calls == [("get_cash", {})]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_tif", ["day", "fok"])
async def test_preview_crypto_rejects_invalid_time_in_force_before_service_call(
    fake_preview_service: FakeAlpacaPaperService,
    bad_tif: str,
) -> None:
    with pytest.raises(ValueError, match="crypto time_in_force"):
        await alpaca_paper_preview_order(
            symbol="BTC/USD",
            side="buy",
            type="limit",
            notional=Decimal("10"),
            limit_price=Decimal("50000"),
            time_in_force=bad_tif,
            asset_class="crypto",
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_equity_omitted_time_in_force_keeps_day_default(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_preview_order(
        symbol="AAPL",
        side="buy",
        type="market",
        qty=Decimal("1"),
    )

    assert payload["order_request"]["time_in_force"] == "day"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_crypto_rejects_non_allowlisted_symbol(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="crypto symbol"):
        await alpaca_paper_preview_order(
            symbol="DOGE/USD",
            side="buy",
            type="limit",
            notional=Decimal("10"),
            limit_price=Decimal("1"),
            asset_class="crypto",
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_crypto_rejects_missing_limit_price(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="limit_price is required"):
        await alpaca_paper_preview_order(
            symbol="BTC/USD",
            side="buy",
            type="limit",
            notional=Decimal("10"),
            asset_class="crypto",
        )
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"side": "sell"}, "buy-only"),
        ({"type": "market", "limit_price": None}, "limit-only"),
        ({"notional": Decimal("51")}, "crypto notional"),
        ({"qty": Decimal("0.002"), "notional": None}, "estimated_cost"),
    ],
)
async def test_preview_crypto_rejects_unsafe_order_shapes(
    fake_preview_service: FakeAlpacaPaperService,
    kwargs: dict[str, object],
    message: str,
) -> None:
    base = {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "limit",
        "notional": Decimal("10"),
        "limit_price": Decimal("50000"),
        "asset_class": "crypto",
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        await alpaca_paper_preview_order(**base)
    assert fake_preview_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rejects_stop_price(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    with pytest.raises(ValueError, match="stop_price not supported"):
        await alpaca_paper_preview_order(
            symbol="AAPL",
            side="buy",
            type="market",
            qty=Decimal("1"),
            stop_price=Decimal("100"),
        )
    assert fake_preview_service.calls == []


# --- 4. Happy paths ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_market_buy_qty_returns_normalized_echo(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_preview_order(
        symbol="aapl",
        side="BUY",
        type="MARKET",
        qty=Decimal("1"),
    )
    assert payload["preview"] is True
    assert payload["submitted"] is False
    assert payload["account_mode"] == "alpaca_paper"
    req = payload["order_request"]
    assert req["symbol"] == "AAPL"
    assert req["side"] == "buy"
    assert req["type"] == "market"
    assert req["qty"] == "1"
    assert req["notional"] is None
    assert req["limit_price"] is None
    assert req["time_in_force"] == "day"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_limit_buy_returns_estimated_cost_and_buying_power_check(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_preview_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("2"),
        limit_price=Decimal("180"),
    )
    assert payload["estimated_cost"] == "360"
    assert payload["would_exceed_buying_power"] is False
    assert payload["account_context"] is not None
    assert payload["account_context"]["buying_power"] == "200000"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_flags_buying_power_exceeded(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_preview_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("2000"),
        limit_price=Decimal("180"),
    )
    assert payload["would_exceed_buying_power"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_market_notional_buy(
    fake_preview_service: FakeAlpacaPaperService,
) -> None:
    payload = await alpaca_paper_preview_order(
        symbol="AAPL",
        side="buy",
        type="market",
        notional=Decimal("100"),
    )
    assert payload["order_request"]["notional"] == "100"
    assert payload["order_request"]["qty"] is None
    assert payload["estimated_cost"] is None
    assert payload["would_exceed_buying_power"] is None


# --- 5. Best-effort context fail-soft ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_when_get_cash_unavailable_returns_warning(
    fake_preview_service_with_cash_error: FakeAlpacaPaperServiceWithCashError,
) -> None:
    payload = await alpaca_paper_preview_order(
        symbol="AAPL",
        side="buy",
        type="market",
        qty=Decimal("1"),
    )
    assert payload["success"] is True
    assert payload["account_context"] is None
    assert "context_unavailable" in payload["warnings"]
    assert payload["would_exceed_buying_power"] is None


# --- 6. Live endpoint fails closed ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_fails_closed_on_live_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.brokers.alpaca.config import AlpacaPaperSettings
    from app.services.brokers.alpaca.endpoints import LIVE_TRADING_BASE_URL
    from app.services.brokers.alpaca.exceptions import AlpacaPaperEndpointError

    def fake_from_app_settings() -> AlpacaPaperSettings:
        return AlpacaPaperSettings(
            api_key="pk-test",
            api_secret="sk-test",
            base_url=LIVE_TRADING_BASE_URL,
        )

    monkeypatch.setattr(
        AlpacaPaperSettings, "from_app_settings", fake_from_app_settings
    )

    with pytest.raises(AlpacaPaperEndpointError):
        await alpaca_paper_preview_order(
            symbol="AAPL",
            side="buy",
            type="market",
            qty=Decimal("1"),
        )
