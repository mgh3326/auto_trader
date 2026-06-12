from __future__ import annotations

from decimal import Decimal

import pytest

from app.mcp_server.tooling.orders_toss_variants import (
    TOSS_LIVE_ORDER_TOOL_NAMES,
    register_toss_live_order_tools,
    toss_cancel_order,
    toss_get_order_history,
    toss_get_orderable_cash,
    toss_get_positions,
    toss_modify_order,
    toss_place_order,
)
from tests._mcp_tooling_support import DummyMCP


def test_all_seven_toss_tools_register():
    mcp = DummyMCP()
    register_toss_live_order_tools(mcp)
    assert set(mcp.tools.keys()) == TOSS_LIVE_ORDER_TOOL_NAMES


def test_toss_tool_descriptions_document_live_gates():
    class RecordingMCP:
        def __init__(self) -> None:
            self.descriptions: dict[str, str] = {}

        def tool(self, *, name: str, description: str = ""):
            self.descriptions[name] = description

            def decorator(func):
                return func

            return decorator

    mcp = RecordingMCP()
    register_toss_live_order_tools(mcp)  # type: ignore[arg-type]

    place_desc = mcp.descriptions["toss_place_order"]
    assert "account_mode='toss_live'" in place_desc
    assert "market='kr'|'us'" in place_desc
    assert "dry_run=True" in place_desc
    assert "confirm=True" in place_desc
    assert "confirm_high_value_order=True" in place_desc
    assert "opposite pending" in place_desc

    modify_desc = mcp.descriptions["toss_modify_order"]
    assert "KR" in modify_desc
    assert "new_price and new_quantity" in modify_desc
    assert "US" in modify_desc
    assert "rejects new_quantity" in modify_desc


@pytest.mark.asyncio
async def test_place_order_fails_closed_when_toss_disabled(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", False)

    result = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="10",
        price="150.0",
        account_mode="toss_live",
    )

    assert result["success"] is False
    assert result["account_mode"] == "toss_live"
    assert result["source"] == "toss"
    assert "TOSS_API_ENABLED" in result["error"]


@pytest.mark.asyncio
async def test_toss_tools_reject_wrong_account_mode(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    result = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="10",
        price="150.0",
        account_mode="kis_live",
    )

    assert result["success"] is False
    assert result["account_mode"] == "toss_live"
    assert "only support account_mode='toss_live'" in result["error"]


class MockTossClient:
    def __init__(self, monkeypatch=None):
        self.placed_payloads = []
        self.holdings_list = []
        self.orders_list = []
        self.prices_list = []
        self.get_order_calls = 0

        if monkeypatch:
            monkeypatch.setattr(
                "app.mcp_server.tooling.orders_toss_variants.TossReadClient.from_settings",
                lambda *args, **kwargs: self,
            )

    async def aclose(self):
        pass

    async def holdings(self, *, symbol=None):
        # returns simple dataclass mock
        from types import SimpleNamespace

        items = []
        for item in self.holdings_list:
            items.append(SimpleNamespace(**item))
        return SimpleNamespace(items=items, raw_overview={})

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

        return [
            SimpleNamespace(**item)
            for item in self.prices_list
            if item.get("symbol") in symbols
        ]

    async def place_order(self, payload):
        self.placed_payloads.append(payload)
        from types import SimpleNamespace

        return SimpleNamespace(
            order_id="new-ord-123", client_order_id=payload.get("clientOrderId")
        )

    async def get_order(self, order_id):
        from types import SimpleNamespace

        self.get_order_calls += 1
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

    async def buying_power(self, *, currency):
        from types import SimpleNamespace

        return SimpleNamespace(currency=currency, cash_buying_power=Decimal("10000.0"))


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
async def test_toss_pinned_tools_accept_omitted_account_mode(monkeypatch):
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
    )

    assert res["success"] is True
    assert res["account_mode"] == "toss_live"
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
    assert res["mutation_sent"] is False
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
            "currency": "USD",
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
            "execution": {},
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
            "execution": {},
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
            "execution": {},
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
            "execution": {},
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
            "execution": {},
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
    assert res["mutation_sent"] is False
    assert mock_client.get_order_calls == 0
    assert not mock_client.placed_payloads


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
    assert res["mutation_sent"] is False


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
            "execution": {},
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


@pytest.mark.asyncio
async def test_get_order_history_uses_closed_cursor_pagination_args(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    seen_args = {}

    async def fake_list_orders(
        status, symbol=None, from_date=None, to_date=None, cursor=None, limit=None
    ):
        seen_args["status"] = status
        seen_args["cursor"] = cursor
        seen_args["limit"] = limit
        from types import SimpleNamespace

        return SimpleNamespace(orders=[], next_cursor="cur456", has_next=True)

    monkeypatch.setattr(mock_client, "list_orders", fake_list_orders)

    res = await toss_get_order_history(
        status="closed",
        cursor="cur123",
        limit=20,
        account_mode="toss_live",
    )
    assert res["success"] is True
    assert seen_args["status"] == "CLOSED"
    assert seen_args["cursor"] == "cur123"
    assert seen_args["limit"] == 20
    assert res["next_cursor"] == "cur456"
    assert res["has_next"] is True


@pytest.mark.asyncio
async def test_get_order_history_stringifies_decimal_execution_fields(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "ord-1",
            "symbol": "AAPL",
            "side": "BUY",
            "status": "CLOSED",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("150.0"),
            "quantity": Decimal("10"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {
                "filledQuantity": Decimal("2.5"),
                "averageFilledPrice": Decimal("151.25"),
                "filledAmount": Decimal("378.125"),
            },
        }
    ]

    res = await toss_get_order_history(account_mode="toss_live")

    assert res["success"] is True
    execution = res["orders"][0]["execution"]
    assert execution["filledQuantity"] == "2.5"
    assert execution["averageFilledPrice"] == "151.25"
    assert execution["filledAmount"] == "378.125"


@pytest.mark.asyncio
async def test_get_positions_shapes_holdings(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.holdings_list = [
        {
            "symbol": "AAPL",
            "quantity": Decimal("10.5"),
            "average_purchase_price": Decimal("150.0"),
            "last_price": Decimal("155.0"),
            "name": "Apple",
            "market_country": "US",
            "currency": "USD",
            "market_value": {"amount": Decimal("1627.5")},
            "profit_loss": {"amount": Decimal("52.5")},
            "daily_profit_loss": {},
            "cost": {},
        }
    ]

    res = await toss_get_positions(
        account_mode="toss_live",
    )
    assert res["success"] is True
    item = res["items"][0]
    assert item["symbol"] == "AAPL"
    assert item["quantity"] == "10.5"
    assert item["average_purchase_price"] == "150"
    assert item["last_price"] == "155"
    assert item["currency"] == "USD"
    assert item["market_value"]["amount"] == "1627.5"
    assert item["profit_loss"]["amount"] == "52.5"


@pytest.mark.asyncio
async def test_get_orderable_cash_reads_currency(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    MockTossClient(monkeypatch)

    res = await toss_get_orderable_cash(
        currency="USD",
        account_mode="toss_live",
    )
    assert res["success"] is True
    assert res["cash_buying_power"] == "10000"
    assert res["currency"] == "USD"
