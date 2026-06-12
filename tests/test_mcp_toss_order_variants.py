from __future__ import annotations

from decimal import Decimal

import pytest

from app.mcp_server.tooling.orders_toss_variants import (
    TOSS_LIVE_ORDER_TOOL_NAMES,
    register_toss_live_order_tools,
    toss_cancel_order,
    toss_modify_order,
    toss_place_order,
)
from app.services.brokers.toss import TossApiDisabled
from tests._mcp_tooling_support import DummyMCP


def test_all_seven_toss_tools_register():
    mcp = DummyMCP()
    register_toss_live_order_tools(mcp)
    assert set(mcp.tools.keys()) == TOSS_LIVE_ORDER_TOOL_NAMES


@pytest.mark.asyncio
async def test_place_order_fails_closed_when_toss_disabled(monkeypatch):
    # Mock validate_toss_api_config to return missing credentials (or simulator of disabled)
    # Actually, we want to mock it to fail when toss is disabled.
    # We will test validate_toss_api_config returning a missing flag or custom config mocking.

    # We can check how toss_api_enabled is set in settings.
    # Let's say settings.toss_api_enabled is False.
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", False)

    with pytest.raises(TossApiDisabled):
        await toss_place_order(
            symbol="AAPL",
            side="buy",
            quantity="10",
            price="150.0",
            account_mode="toss_live",
        )


@pytest.mark.asyncio
async def test_toss_tools_reject_wrong_account_mode(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    with pytest.raises(ValueError, match="Toss live tools only support account_mode"):
        await toss_place_order(
            symbol="AAPL",
            side="buy",
            quantity="10",
            price="150.0",
            account_mode="kis_live",
        )


class MockTossClient:
    def __init__(self, monkeypatch=None):
        self.placed_payloads = []
        self.holdings_list = []
        self.orders_list = []
        self.prices_list = []

        if monkeypatch:
            monkeypatch.setattr(
                "app.mcp_server.tooling.orders_toss_variants.TossReadClient.from_settings",
                lambda *args, **kwargs: self
            )

    async def aclose(self):
        pass

    async def holdings(self, *, symbol=None):
        # returns simple dataclass mock
        from types import SimpleNamespace
        items = []
        for item in self.holdings_list:
            items.append(SimpleNamespace(**item))
        return SimpleNamespace(items=items)

    async def list_orders(self, *, status, symbol=None, **kwargs):
        from types import SimpleNamespace
        orders = []
        for o in self.orders_list:
            if symbol and o.get("symbol") != symbol:
                continue
            if status and o.get("status") != status:
                continue
            orders.append(SimpleNamespace(**o))
        return SimpleNamespace(orders=orders, next_cursor=None, has_next=False)

    async def prices(self, symbols):
        from types import SimpleNamespace
        return [SimpleNamespace(**item) for item in self.prices_list if item.get("symbol") in symbols]

    async def place_order(self, payload):
        self.placed_payloads.append(payload)
        from types import SimpleNamespace
        return SimpleNamespace(order_id="new-ord-123", client_order_id=payload.get("clientOrderId"))

    async def get_order(self, order_id):
        from types import SimpleNamespace
        for o in self.orders_list:
            if o.get("order_id") == order_id:
                return SimpleNamespace(**o)
        raise ValueError(f"Order not found: {order_id}")

    async def modify_order(self, order_id, payload):
        self.placed_payloads.append(payload)
        from types import SimpleNamespace
        return SimpleNamespace(order_id="mod-ord-456")

    async def cancel_order(self, order_id):
        from types import SimpleNamespace
        return SimpleNamespace(order_id="can-ord-789")


@pytest.mark.asyncio
async def test_place_order_defaults_to_dry_run_and_does_not_call_broker(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="10",
        price="150.0",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["dry_run"] is True
    assert res["mutation_sent"] is False
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_place_order_requires_confirm_when_dry_run_false(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="10",
        price="150.0",
        dry_run=False,
        confirm=False,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "confirm=True" in res["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_high_value_kr_order_requires_explicit_confirm_high_value(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    # 005930 is 6 digits -> KR market. Notional = 2000 * 50000 = 100,000,000
    res = await toss_place_order(
        symbol="005930",
        side="buy",
        quantity="2000",
        price="50000",
        dry_run=False,
        confirm=True,
        confirm_high_value_order=False,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "high-value" in res["error"].lower()
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_place_sell_blocks_below_avg_floor_for_limit_and_market(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    from decimal import Decimal
    mock_client = MockTossClient(monkeypatch)
    mock_client.holdings_list = [
        {
            "symbol": "AAPL",
            "quantity": Decimal("10"),
            "average_purchase_price": Decimal("100.0"),
            "last_price": Decimal("102.0"),
            "name": "Apple",
            "market_country": "US",
            "currency": "USD",
            "market_value": {},
            "profit_loss": {},
            "daily_profit_loss": {},
            "cost": {},
        }
    ]
    # average_purchase_price * 1.01 = 101.0

    # Limit sell below 101.0 (e.g. 100.0) -> block
    res_limit = await toss_place_order(
        symbol="AAPL",
        side="sell",
        order_type="limit",
        quantity="5",
        price="100.0",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )
    assert res_limit["success"] is False
    assert "below average purchase price floor" in res_limit["error"]

    # Market sell when current price is below 101.0 (e.g. 100.0) -> block
    mock_client.prices_list = [
        {
            "symbol": "AAPL",
            "last_price": Decimal("100.0"),
            "timestamp": "2026-06-12T00:00:00Z",
            "currency": "USD"
        }
    ]
    res_market = await toss_place_order(
        symbol="AAPL",
        side="sell",
        order_type="market",
        quantity="5",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )
    assert res_market["success"] is False
    assert "below average purchase price floor" in res_market["error"]


@pytest.mark.asyncio
async def test_place_order_blocks_opposite_pending_before_post(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    # symbol AAPL has pending SELL order
    mock_client.orders_list = [
        {
            "order_id": "ord-existing",
            "symbol": "AAPL",
            "side": "SELL",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("150.0"),
            "quantity": Decimal("10"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {}
        }
    ]

    # Try to BUY AAPL -> block
    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="5",
        price="140.0",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "opposite pending order exists" in res["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_modify_kr_requires_price_and_quantity(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "orig-ord-123",
            "symbol": "005930",
            "side": "BUY",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("50000"),
            "quantity": Decimal("10"),
            "order_amount": None,
            "currency": "KRW",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {}
        }
    ]

    res = await toss_modify_order(
        order_id="orig-ord-123",
        new_price="51000",
        new_quantity=None,
        market="kr",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "both new_price and new_quantity" in res["error"].lower()


@pytest.mark.asyncio
async def test_modify_us_rejects_quantity(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "orig-ord-123",
            "symbol": "AAPL",
            "side": "BUY",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("150.0"),
            "quantity": Decimal("10"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {}
        }
    ]

    res = await toss_modify_order(
        order_id="orig-ord-123",
        new_price="155.0",
        new_quantity="12",
        market="us",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "rejects new_quantity" in res["error"]


@pytest.mark.asyncio
async def test_modify_sell_reprice_blocks_below_avg_floor(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "orig-ord-123",
            "symbol": "AAPL",
            "side": "SELL",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("102.0"),
            "quantity": Decimal("10"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {}
        }
    ]
    mock_client.holdings_list = [
        {
            "symbol": "AAPL",
            "quantity": Decimal("10"),
            "average_purchase_price": Decimal("100.0"),
            "last_price": Decimal("102.0"),
            "name": "Apple",
            "market_country": "US",
            "currency": "USD",
            "market_value": {},
            "profit_loss": {},
            "daily_profit_loss": {},
            "cost": {},
        }
    ]

    res = await toss_modify_order(
        order_id="orig-ord-123",
        new_price="100.0",
        market="us",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "below average purchase price floor" in res["error"]


@pytest.mark.asyncio
async def test_modify_requires_confirm_when_dry_run_false(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "orig-ord-123",
            "symbol": "AAPL",
            "side": "BUY",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("150.0"),
            "quantity": Decimal("10"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {}
        }
    ]

    res = await toss_modify_order(
        order_id="orig-ord-123",
        new_price="150.0",
        dry_run=False,
        confirm=False,
        account_mode="toss_live",
    )
    assert res["success"] is False
    assert "confirm=True" in res["error"]


@pytest.mark.asyncio
async def test_cancel_requires_confirm_when_dry_run_false(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    MockTossClient(monkeypatch)

    res = await toss_cancel_order(
        order_id="orig-ord-123",
        dry_run=False,
        confirm=False,
        account_mode="toss_live",
    )
    assert res["success"] is False
    assert "confirm=True" in res["error"]


@pytest.mark.asyncio
async def test_modify_and_cancel_surface_replacement_order_id(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "orig-ord-123",
            "symbol": "AAPL",
            "side": "BUY",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("150.0"),
            "quantity": Decimal("10"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {}
        }
    ]

    res_cancel = await toss_cancel_order(
        order_id="orig-ord-123",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )
    assert res_cancel["success"] is True
    assert res_cancel["original_order_id"] == "orig-ord-123"
    assert res_cancel["replacement_order_id"] == "can-ord-789"
    assert "newly issued orderId" in res_cancel["operation_semantics"]

    res_modify = await toss_modify_order(
        order_id="orig-ord-123",
        new_price="155.0",
        market="us",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )
    assert res_modify["success"] is True
    assert res_modify["original_order_id"] == "orig-ord-123"
    assert res_modify["replacement_order_id"] == "mod-ord-456"
    assert "newly issued orderId" in res_modify["operation_semantics"]
