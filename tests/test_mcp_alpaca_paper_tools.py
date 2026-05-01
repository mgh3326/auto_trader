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
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.registry import register_all_tools
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
        "alpaca_paper_submit_order",
        "alpaca_paper_place_order",
        "alpaca_paper_cancel_order",
        "alpaca_paper_replace_order",
        "alpaca_paper_modify_order",
    }
    assert forbidden_names.isdisjoint(mcp.tools.keys())
    assert {name for name in mcp.tools if name.startswith("alpaca_live_")} == set()
    assert {
        name
        for name in mcp.tools
        if name.startswith("alpaca_paper_")
        and any(
            verb in name for verb in ("submit", "place", "cancel", "replace", "modify")
        )
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
