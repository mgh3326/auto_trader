"""Unit tests for alpaca_paper_submit_order and alpaca_paper_cancel_order (ROB-73)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import alpaca_paper_orders as _orders_mod
from app.mcp_server.tooling.alpaca_paper_orders import (
    ALPACA_PAPER_MUTATING_TOOL_NAMES,
    SUBMIT_MAX_NOTIONAL_USD,
    SUBMIT_MAX_QTY,
    alpaca_paper_cancel_order,
    alpaca_paper_submit_order,
    reset_alpaca_paper_orders_service_factory,
    set_alpaca_paper_orders_service_factory,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.services.brokers.alpaca.schemas import Order
from tests._mcp_tooling_support import DummyMCP
from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService


def test_module_exposes_expected_surface() -> None:
    assert _orders_mod.ALPACA_PAPER_MUTATING_TOOL_NAMES == {
        "alpaca_paper_submit_order",
        "alpaca_paper_cancel_order",
    }
    assert callable(_orders_mod.alpaca_paper_submit_order)
    assert callable(_orders_mod.alpaca_paper_cancel_order)
    assert callable(_orders_mod.set_alpaca_paper_orders_service_factory)
    assert callable(_orders_mod.reset_alpaca_paper_orders_service_factory)
    assert callable(_orders_mod.register_alpaca_paper_orders_tools)
    assert _orders_mod.SUBMIT_MAX_QTY == Decimal("5")
    assert _orders_mod.SUBMIT_MAX_NOTIONAL_USD == Decimal("1000")


class FakeOrdersService(FakeAlpacaPaperService):
    """Fake service that records submit/cancel calls without raising."""

    async def submit_order(self, request: Any) -> Order:  # type: ignore[override]
        self.calls.append(("submit_order", {"request": request}))
        return Order(
            id="paper-order-123",
            client_order_id=getattr(request, "client_order_id", None),
            symbol=getattr(request, "symbol", "AAPL"),
            qty=getattr(request, "qty", None),
            filled_qty=Decimal("0"),
            side=getattr(request, "side", "buy"),
            type=getattr(request, "type", "limit"),
            time_in_force=getattr(request, "time_in_force", "day"),
            status="accepted",
            limit_price=getattr(request, "limit_price", None),
        )

    async def cancel_order(self, order_id: str) -> None:  # type: ignore[override]
        self.calls.append(("cancel_order", {"order_id": order_id}))

    async def get_order(self, order_id: str) -> Order:  # type: ignore[override]
        self.calls.append(("get_order", {"order_id": order_id}))
        return Order(
            id=order_id,
            symbol="AAPL",
            qty=Decimal("1"),
            filled_qty=Decimal("0"),
            side="buy",
            type="limit",
            time_in_force="day",
            status="canceled",
            limit_price=Decimal("1.00"),
        )


@pytest.fixture
def fake_orders_service() -> FakeOrdersService:
    service = FakeOrdersService()
    set_alpaca_paper_orders_service_factory(lambda: service)  # type: ignore[arg-type]
    yield service
    reset_alpaca_paper_orders_service_factory()


# ---------------------------------------------------------------------------
# Submit: confirm gate and validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_without_confirm_is_blocked_no_op(
    fake_orders_service: FakeOrdersService,
) -> None:
    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("1.00"),
    )
    assert payload["submitted"] is False
    assert payload["blocked_reason"] == "confirmation_required"
    assert payload["order_request"]["symbol"] == "AAPL"
    assert payload["client_order_id"].startswith("rob73-")
    assert fake_orders_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_with_confirm_calls_service_once(
    fake_orders_service: FakeOrdersService,
) -> None:
    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("1.00"),
        confirm=True,
    )
    assert payload["submitted"] is True
    assert payload["order"]["id"] == "paper-order-123"
    submit_calls = [c for c in fake_orders_service.calls if c[0] == "submit_order"]
    assert len(submit_calls) == 1
    sent = submit_calls[0][1]["request"]
    assert sent.symbol == "AAPL"
    assert sent.qty == Decimal("1")
    assert sent.limit_price == Decimal("1.00")
    assert sent.client_order_id.startswith("rob73-")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_submit_without_confirm_is_blocked_no_op(
    fake_orders_service: FakeOrdersService,
) -> None:
    payload = await alpaca_paper_submit_order(
        symbol="BTC/USD",
        side="buy",
        type="limit",
        notional=Decimal("10"),
        limit_price=Decimal("50000"),
        time_in_force="gtc",
        asset_class="crypto",
    )
    assert payload["submitted"] is False
    assert payload["blocked_reason"] == "confirmation_required"
    assert payload["order_request"]["asset_class"] == "crypto"
    assert payload["client_order_id"].startswith("rob74-crypto-")
    assert fake_orders_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_submit_with_confirm_calls_service_once(
    fake_orders_service: FakeOrdersService,
) -> None:
    payload = await alpaca_paper_submit_order(
        symbol="BTC/USD",
        side="buy",
        type="limit",
        notional=Decimal("10"),
        limit_price=Decimal("50000"),
        time_in_force="gtc",
        asset_class="crypto",
        confirm=True,
    )
    assert payload["submitted"] is True
    assert payload["client_order_id"].startswith("rob74-crypto-")
    sent = [c for c in fake_orders_service.calls if c[0] == "submit_order"][0][1][
        "request"
    ]
    assert sent.symbol == "BTC/USD"
    assert sent.notional == Decimal("10")
    assert sent.limit_price == Decimal("50000")
    assert sent.side == "buy"
    assert sent.type == "limit"
    assert sent.time_in_force == "gtc"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_submit_rejects_unsafe_shapes_before_service_call(
    fake_orders_service: FakeOrdersService,
) -> None:
    base = {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "limit",
        "notional": Decimal("10"),
        "limit_price": Decimal("50000"),
        "asset_class": "crypto",
        "confirm": True,
    }
    cases = [
        ({"symbol": "DOGE/USD"}, "crypto symbol"),
        ({"side": "sell"}, "buy-only"),
        ({"type": "market", "limit_price": None}, "limit-only"),
        ({"notional": Decimal("51")}, "crypto notional"),
    ]
    for kwargs, message in cases:
        payload = base | kwargs
        with pytest.raises(ValueError, match=message):
            await alpaca_paper_submit_order(**payload)
    assert fake_orders_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_caller_client_order_id_passes_through(
    fake_orders_service: FakeOrdersService,
) -> None:
    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("1.00"),
        client_order_id="dev-smoke-001",
        confirm=True,
    )
    assert payload["client_order_id"] == "dev-smoke-001"
    sent = [c for c in fake_orders_service.calls if c[0] == "submit_order"][0][1][
        "request"
    ]
    assert sent.client_order_id == "dev-smoke-001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_rejects_qty_exceeding_cap(
    fake_orders_service: FakeOrdersService,
) -> None:
    with pytest.raises(ValueError, match="exceeds submit cap"):
        await alpaca_paper_submit_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=SUBMIT_MAX_QTY + Decimal("1"),
            limit_price=Decimal("1.00"),
            confirm=True,
        )
    assert [c for c in fake_orders_service.calls if c[0] == "submit_order"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_rejects_notional_exceeding_cap(
    fake_orders_service: FakeOrdersService,
) -> None:
    with pytest.raises(ValueError, match="exceeds submit cap"):
        await alpaca_paper_submit_order(
            symbol="AAPL",
            side="buy",
            type="market",
            notional=SUBMIT_MAX_NOTIONAL_USD + Decimal("1"),
            confirm=True,
        )
    assert [c for c in fake_orders_service.calls if c[0] == "submit_order"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_rejects_estimated_cost_exceeding_cap(
    fake_orders_service: FakeOrdersService,
) -> None:
    with pytest.raises(ValueError, match="estimated_cost"):
        await alpaca_paper_submit_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=Decimal("5"),
            limit_price=Decimal("250"),  # 5 * 250 = 1250 > 1000
            confirm=True,
        )
    assert [c for c in fake_orders_service.calls if c[0] == "submit_order"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_propagates_preview_validation_errors(
    fake_orders_service: FakeOrdersService,
) -> None:
    with pytest.raises(ValueError):
        await alpaca_paper_submit_order(
            symbol="AAPL",
            side="hold",
            type="limit",
            qty=Decimal("1"),
            limit_price=Decimal("1.00"),
        )
    with pytest.raises(ValueError, match="limit_price is required"):
        await alpaca_paper_submit_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=Decimal("1"),
        )
    with pytest.raises(ValueError, match="exactly one"):
        await alpaca_paper_submit_order(
            symbol="AAPL",
            side="buy",
            type="market",
            qty=Decimal("1"),
            notional=Decimal("100"),
        )
    assert fake_orders_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_client_order_id_is_deterministic(
    fake_orders_service: FakeOrdersService,
) -> None:
    a = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("1.00"),
    )
    b = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("1.00"),
    )
    assert a["client_order_id"] == b["client_order_id"]
    c = await alpaca_paper_submit_order(
        symbol="MSFT",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("1.00"),
    )
    assert c["client_order_id"] != a["client_order_id"]


# ---------------------------------------------------------------------------
# Cancel: confirm gate and validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_without_confirm_is_blocked_no_op(
    fake_orders_service: FakeOrdersService,
) -> None:
    payload = await alpaca_paper_cancel_order(order_id="paper-order-123")
    assert payload["cancelled"] is False
    assert payload["blocked_reason"] == "confirmation_required"
    assert payload["target_order_id"] == "paper-order-123"
    assert fake_orders_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_with_confirm_calls_service_once_and_reads_back(
    fake_orders_service: FakeOrdersService,
) -> None:
    payload = await alpaca_paper_cancel_order(
        order_id="paper-order-123",
        confirm=True,
    )
    assert payload["cancelled"] is True
    assert payload["cancelled_order_id"] == "paper-order-123"
    assert payload["read_back_status"] == "ok"
    assert payload["order"]["status"] == "canceled"
    cancel_calls = [c for c in fake_orders_service.calls if c[0] == "cancel_order"]
    assert cancel_calls == [("cancel_order", {"order_id": "paper-order-123"})]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_strips_whitespace_from_order_id(
    fake_orders_service: FakeOrdersService,
) -> None:
    payload = await alpaca_paper_cancel_order(
        order_id="  paper-order-123  ",
        confirm=True,
    )
    assert payload["cancelled_order_id"] == "paper-order-123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_rejects_blank_order_id(
    fake_orders_service: FakeOrdersService,
) -> None:
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="order_id is required"):
            await alpaca_paper_cancel_order(order_id=bad, confirm=True)
    assert fake_orders_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_order_id",
    [
        "../orders",
        "/v2/orders",
        "orders",
        "all",
        "*",
        "paper-order-123?status=open",
        "paper-order-123#fragment",
        "paper/order/123",
        "paper-order-123,other-order",
        "paper order 123",
        "paper-order-123\nextra",
    ],
)
async def test_cancel_rejects_unsafe_order_id_path_segments(
    fake_orders_service: FakeOrdersService,
    bad_order_id: str,
) -> None:
    with pytest.raises(ValueError, match="order_id"):
        await alpaca_paper_cancel_order(order_id=bad_order_id, confirm=True)
    assert fake_orders_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_rejects_unsafe_order_id_before_confirm_gate(
    fake_orders_service: FakeOrdersService,
) -> None:
    with pytest.raises(ValueError, match="order_id"):
        await alpaca_paper_cancel_order(order_id="../orders", confirm=False)
    assert fake_orders_service.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_accepts_uuid_like_order_id(
    fake_orders_service: FakeOrdersService,
) -> None:
    order_id = "61e69015-8549-4bfd-b9c3-01e75843f47d"
    payload = await alpaca_paper_cancel_order(order_id=order_id, confirm=True)
    assert payload["cancelled_order_id"] == order_id
    assert ("cancel_order", {"order_id": order_id}) in fake_orders_service.calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_signature_has_no_bulk_or_filter_params() -> None:
    import inspect

    sig = inspect.signature(alpaca_paper_cancel_order)
    assert set(sig.parameters.keys()) == {"order_id", "confirm"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_read_back_failure_marks_unavailable_but_succeeds(
    fake_orders_service: FakeOrdersService,
) -> None:
    from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError

    async def _raise(_id: str) -> Order:
        raise AlpacaPaperRequestError("not found", status_code=404)

    fake_orders_service.get_order = _raise  # type: ignore[assignment]

    payload = await alpaca_paper_cancel_order(
        order_id="paper-order-123",
        confirm=True,
    )
    assert payload["cancelled"] is True
    assert payload["read_back_status"] == "unavailable"
    assert payload["order"] is None


# ---------------------------------------------------------------------------
# Endpoint guard (fail closed on live endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_fails_closed_on_live_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import alpaca_paper_orders as mod
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
    mod.reset_alpaca_paper_orders_service_factory()

    with pytest.raises(AlpacaPaperEndpointError):
        await alpaca_paper_submit_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=Decimal("1"),
            limit_price=Decimal("1.00"),
            confirm=True,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_fails_closed_on_live_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import alpaca_paper_orders as mod
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
    mod.reset_alpaca_paper_orders_service_factory()

    with pytest.raises(AlpacaPaperEndpointError):
        await alpaca_paper_cancel_order(order_id="paper-order-123", confirm=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_signature_has_no_endpoint_or_base_url_param() -> None:
    import inspect

    sig = inspect.signature(alpaca_paper_submit_order)
    param_names = set(sig.parameters.keys())
    forbidden = {"endpoint", "base_url", "live", "url", "host", "env"}
    assert forbidden.isdisjoint(param_names)


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registers_alpaca_paper_orders_tools_default_profile() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]
    assert ALPACA_PAPER_MUTATING_TOOL_NAMES <= mcp.tools.keys()


@pytest.mark.unit
def test_registers_alpaca_paper_orders_tools_paper_profile() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.HERMES_PAPER_KIS)  # type: ignore[arg-type]
    assert ALPACA_PAPER_MUTATING_TOOL_NAMES <= mcp.tools.keys()


@pytest.mark.unit
def test_no_alpaca_paper_place_replace_modify_or_bulk_cancel_tools() -> None:
    mcp = DummyMCP()
    register_all_tools(mcp, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]
    forbidden = {
        "alpaca_paper_place_order",
        "alpaca_paper_replace_order",
        "alpaca_paper_modify_order",
        "alpaca_paper_cancel_all_orders",
        "alpaca_paper_cancel_orders",
        "alpaca_paper_cancel_by_symbol",
    }
    assert forbidden.isdisjoint(mcp.tools.keys())


# ---------------------------------------------------------------------------
# Validation reuse: preview ↔ submit canonical payload parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_canonical_payload_matches_preview_order_request(
    fake_orders_service: FakeOrdersService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit's order_request must agree with preview's order_request on shared fields."""
    from app.mcp_server.tooling.alpaca_paper_preview import (
        alpaca_paper_preview_order,
        reset_alpaca_paper_preview_service_factory,
        set_alpaca_paper_preview_service_factory,
    )

    set_alpaca_paper_preview_service_factory(lambda: fake_orders_service)  # type: ignore[arg-type]
    try:
        preview = await alpaca_paper_preview_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=Decimal("1"),
            limit_price=Decimal("1.00"),
        )
    finally:
        reset_alpaca_paper_preview_service_factory()

    submit_blocked = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("1"),
        limit_price=Decimal("1.00"),
    )

    shared = (
        "symbol",
        "side",
        "type",
        "time_in_force",
        "qty",
        "notional",
        "limit_price",
        "asset_class",
    )
    for key in shared:
        assert preview["order_request"][key] == submit_blocked["order_request"][key], (
            f"preview/submit drift on '{key}'"
        )
