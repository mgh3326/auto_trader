from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from app.mcp_server.tooling.orders_toss_variants import (
    TOSS_LIVE_ORDER_TOOL_NAMES,
    _high_value_uncheckable,
    register_toss_live_order_tools,
    toss_cancel_order,
    toss_get_order_history,
    toss_get_orderable_cash,
    toss_get_positions,
    toss_modify_order,
    toss_place_order,
    toss_preview_order,
)
from app.services.toss_sellable_cache import TossSellableCache
from tests._mcp_tooling_support import DummyMCP


def test_high_value_uncheckable_true_for_kr_market_order() -> None:
    """ROB-547: a KR market order has no price -> notional cannot be estimated,
    so the local 1억 gate cannot evaluate it (must be surfaced, not silently skipped)."""
    assert _high_value_uncheckable("kr", Decimal("10"), None, None) is True


def test_high_value_uncheckable_false_for_kr_limit_order() -> None:
    assert _high_value_uncheckable("kr", Decimal("10"), Decimal("70000"), None) is False


def test_high_value_uncheckable_false_for_us_market_order() -> None:
    # US notional is in USD; the KRW 1억 gate does not apply (broker-side check).
    assert _high_value_uncheckable("us", Decimal("10"), None, None) is False


@pytest.fixture(autouse=True)
def _enable_toss_live_order_mutations_for_existing_tests(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)


def test_all_eight_toss_tools_register():
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

    preview_desc = mcp.descriptions["toss_preview_order"]
    assert "current_price" in preview_desc
    assert "fill_distance/order_warnings" in preview_desc
    assert "fee/FX full-conversion costs" in preview_desc

    place_desc = mcp.descriptions["toss_place_order"]
    assert "account_mode='toss_live'" in place_desc
    assert "market='kr'|'us'" in place_desc
    assert "dry_run=True" in place_desc
    assert "confirm=True" in place_desc
    assert "TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true" in place_desc
    assert "confirm_high_value_order=True" in place_desc
    assert "opposite pending" in place_desc

    modify_desc = mcp.descriptions["toss_modify_order"]
    assert "KR" in modify_desc
    assert "new_price and new_quantity" in modify_desc
    assert "US" in modify_desc
    assert "rejects new_quantity" in modify_desc

    reconcile_desc = mcp.descriptions["toss_reconcile_orders"]
    assert "Reconcile Toss Securities live KR/US orders" in reconcile_desc
    assert "dry_run=True" in reconcile_desc
    assert "delta-idempotent" in reconcile_desc
    assert "US FX PnL" in reconcile_desc
    assert "fx_pnl_accuracy" in reconcile_desc


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
        self.warnings_list = []
        self.get_order_calls = 0

        if monkeypatch:
            monkeypatch.setattr(
                "app.mcp_server.tooling.orders_toss_variants.TossReadClient.from_settings",
                lambda *args, **kwargs: self,
            )

    async def aclose(self):
        pass

    async def warnings(self, symbol: str):
        from app.services.brokers.toss.dto import TossWarningInfo

        return [
            TossWarningInfo(
                warning_type=w.get("warning_type", "VI_STATIC"),
                exchange=w.get("exchange", "KRX"),
                start_date=w.get("start_date"),
                end_date=w.get("end_date"),
            )
            for w in self.warnings_list
        ]

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
        return SimpleNamespace(currency=currency, cash_buying_power=Decimal("10000.0"))


def _enable_toss_preview(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    return otv


def _stub_toss_costs(commission_bps: float = 10.0, fx_spread_bps: float = 1.7):
    return {
        "version": 1,
        "accounts": {
            "toss": {
                "broker": "toss",
                "markets": {
                    "kr": {"commission_bps": 0.0, "fx_spread_bps": 0.0},
                    "us": {
                        "commission_bps": commission_bps,
                        "fx_spread_bps": fx_spread_bps,
                    },
                },
            }
        },
    }


def _stub_usd_krw_quote(rate: float = 1360.0):
    return SimpleNamespace(
        rate=rate,
        mid_rate=rate,
        default_rate=rate,
        source="toss",
        valid_from=None,
        valid_until=None,
        basis_point=None,
        rate_change_type=None,
    )


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
async def test_place_order_requires_live_mutation_gate_before_broker_post(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", False)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="1",
        price="150",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert res["mutation_sent"] is False
    assert "TOSS_LIVE_ORDER_MUTATIONS_ENABLED" in res["error"]
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
async def test_modify_requires_live_mutation_gate_before_broker_post(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", False)
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
        new_price="155",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert res["mutation_sent"] is False
    assert "TOSS_LIVE_ORDER_MUTATIONS_ENABLED" in res["error"]
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
async def test_cancel_requires_live_mutation_gate_before_broker_post(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", False)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    res = await toss_cancel_order(
        order_id="orig-ord-123",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert res["mutation_sent"] is False
    assert "TOSS_LIVE_ORDER_MUTATIONS_ENABLED" in res["error"]
    assert not mock_client.placed_payloads


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

    monkeypatch.setattr(
        otv,
        "record_toss_replacement_order",
        AsyncMock(return_value={"ledger_id": 123}),
    )

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


@pytest.mark.asyncio
async def test_preview_order_shapes_payload_and_rejects_invalid_inputs(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    res = await toss_preview_order(
        symbol="005930",
        side="buy",
        quantity=3,
        price="50000",
        order_amount="150000",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["market"] == "kr"
    assert res["payload_preview"]["quantity"] == "3"
    assert res["payload_preview"]["price"] == "50000"
    assert res["payload_preview"]["orderAmount"] == "150000"

    with pytest.raises(ValueError, match="Invalid market"):
        await toss_preview_order(
            symbol="AAPL",
            side="buy",
            quantity="1",
            market="jp",  # type: ignore[arg-type]
            account_mode="toss_live",
        )

    with pytest.raises(TypeError, match="price must be str or int"):
        await toss_preview_order(
            symbol="AAPL",
            side="buy",
            quantity="1",
            price=150.5,  # type: ignore[arg-type]
            account_mode="toss_live",
        )

    with pytest.raises(ValueError, match="Invalid decimal value"):
        await toss_preview_order(
            symbol="AAPL",
            side="buy",
            quantity="not-a-number",
            account_mode="toss_live",
        )


@pytest.mark.asyncio
async def test_toss_preview_buy_limit_above_market_returns_price_distance_and_costs(
    monkeypatch,
):
    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "AVGO", "last_price": Decimal("390"), "currency": "USD"}
    ]
    monkeypatch.setattr(
        otv,
        "get_account_costs_setting",
        AsyncMock(return_value=_stub_toss_costs()),
        raising=False,
    )
    monkeypatch.setattr(
        otv,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=_stub_usd_krw_quote()),
        raising=False,
    )

    res = await toss_preview_order(
        symbol="AVGO",
        side="buy",
        order_type="limit",
        quantity="1",
        price="394",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["current_price"] == "390"
    assert res["current_price_currency"] == "USD"
    assert res["order_warnings"] == ["buy_limit_above_market"]
    assert res["fill_distance"] == {
        "distance_usd": "4",
        "distance_pct": "1.0256",
        "currency": "USD",
        "marketable": True,
        "direction": "above_market",
    }
    assert res["estimated_value"] == "394"
    assert res["estimated_value_currency"] == "USD"
    assert res["fee"] == "0.394"
    assert res["fee_currency"] == "USD"
    assert res["fx_cost_full_conversion"] == "91.0928"
    assert res["fx_cost_full_conversion_currency"] == "KRW"
    assert res["estimated_costs"] == {
        "notional": "394",
        "notional_currency": "USD",
        "fee": "0.394",
        "fee_currency": "USD",
        "commission_bps": 10.0,
        "fx_spread_bps": 1.7,
        "fx_cost_full_conversion": "91.0928",
        "fx_cost_full_conversion_currency": "KRW",
        "fx_rate_usd_krw": "1360",
        "fx_rate_source": "toss",
        "fx_assumption": "full_notional_krw_conversion",
        "cost_profile_source": "user_setting",
        "cost_profile_review_required": False,
    }


@pytest.mark.asyncio
async def test_toss_preview_sell_limit_below_market_returns_marketable_warning(
    monkeypatch,
):
    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "AVGO", "last_price": Decimal("390"), "currency": "USD"}
    ]
    monkeypatch.setattr(
        otv,
        "get_account_costs_setting",
        AsyncMock(return_value=_stub_toss_costs()),
        raising=False,
    )
    monkeypatch.setattr(
        otv,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=_stub_usd_krw_quote()),
        raising=False,
    )

    res = await toss_preview_order(
        symbol="AVGO",
        side="sell",
        order_type="limit",
        quantity="1",
        price="380",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["current_price"] == "390"
    assert res["order_warnings"] == ["sell_limit_below_market"]
    assert res["fill_distance"] == {
        "distance_usd": "10",
        "distance_pct": "2.5641",
        "currency": "USD",
        "marketable": True,
        "direction": "below_market",
    }


@pytest.mark.asyncio
async def test_toss_preview_order_degrades_when_price_context_unavailable(monkeypatch):
    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = []
    monkeypatch.setattr(
        otv,
        "get_account_costs_setting",
        AsyncMock(return_value=_stub_toss_costs()),
        raising=False,
    )
    monkeypatch.setattr(
        otv,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=_stub_usd_krw_quote()),
        raising=False,
    )

    res = await toss_preview_order(
        symbol="AVGO",
        side="buy",
        order_type="limit",
        quantity="1",
        price="394",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["current_price"] is None
    assert res["current_price_currency"] is None
    assert "price_context_unavailable" in res["order_warnings"]
    assert (
        "Could not resolve latest price for symbol: AVGO"
        in res["price_context_message"]
    )
    assert "fill_distance" not in res
    assert res["estimated_value"] == "394"
    assert res["fee"] == "0.394"
    assert res["warnings"] == []


@pytest.mark.asyncio
async def test_toss_tools_reject_invalid_account_selector(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    res = await toss_get_orderable_cash(account_mode="bogus")

    assert res["success"] is False
    assert res["account_mode"] == "toss_live"
    assert "account_mode must be one of" in res["error"]


@pytest.mark.asyncio
async def test_place_order_high_value_order_amount_requires_explicit_confirm(
    monkeypatch,
):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    res = await toss_place_order(
        symbol="005930",
        side="buy",
        order_type="market",
        order_amount="100000000",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "requires confirm_high_value_order=True" in res["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_place_sell_guard_fails_closed_on_missing_holding_invalid_average_and_price_lookup(
    monkeypatch,
):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    missing = await toss_place_order(
        symbol="AAPL",
        side="sell",
        quantity="1",
        price="150",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )
    assert missing["success"] is False
    assert "No holding found" in missing["error"]

    mock_client.holdings_list = [
        {
            "symbol": "AAPL",
            "quantity": Decimal("1"),
            "average_purchase_price": Decimal("0"),
            "last_price": Decimal("150"),
            "name": "Apple",
            "market_country": "US",
            "currency": "USD",
            "market_value": {},
            "profit_loss": {},
            "daily_profit_loss": {},
            "cost": {},
        }
    ]
    invalid_average = await toss_place_order(
        symbol="AAPL",
        side="sell",
        quantity="1",
        price="150",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )
    assert invalid_average["success"] is False
    assert "Invalid holding average purchase price" in invalid_average["error"]

    mock_client.holdings_list[0]["average_purchase_price"] = Decimal("100")
    mock_client.prices_list = []
    missing_price = await toss_place_order(
        symbol="AAPL",
        side="sell",
        order_type="market",
        quantity="1",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )
    assert missing_price["success"] is False
    assert "Failed to retrieve current price" in missing_price["error"]


@pytest.mark.asyncio
async def test_place_order_surfaces_toss_api_response_error(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    async def raise_toss_error(payload):
        raise TossApiResponseError(
            TossErrorEnvelope(
                request_id="req-123",
                code="order-rejected",
                message="Rejected by Toss",
                data={"reason": "limit"},
            ),
            status_code=400,
        )

    monkeypatch.setattr(mock_client, "place_order", raise_toss_error)

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="1",
        price="150",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert res["mutation_sent"] is True
    assert res["status_code"] == 400
    assert res["code"] == "order-rejected"
    assert res["request_id"] == "req-123"
    assert res["message"] == "Rejected by Toss"
    assert res["data"] == {"reason": "limit"}


@pytest.mark.asyncio
async def test_place_order_fails_closed_when_pending_order_lookup_errors(
    monkeypatch,
):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    async def raise_lookup_error(**kwargs):
        raise RuntimeError("orders unavailable")

    monkeypatch.setattr(mock_client, "list_orders", raise_lookup_error)

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="1",
        price="150",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "Failed to check pending orders" in res["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_place_order_checks_all_pending_order_pages_before_post(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    seen_cursors = []

    async def paged_orders(*, status, symbol=None, cursor=None, **kwargs):
        from types import SimpleNamespace

        seen_cursors.append(cursor)
        if cursor is None:
            return SimpleNamespace(orders=[], next_cursor="next-page", has_next=True)
        return SimpleNamespace(
            orders=[
                SimpleNamespace(
                    order_id="ord-opposite",
                    symbol=symbol,
                    side="SELL",
                    status=status,
                    order_type="LIMIT",
                    time_in_force="DAY",
                    price=Decimal("150"),
                    quantity=Decimal("1"),
                    order_amount=None,
                    currency="USD",
                    ordered_at="2026-06-12T00:00:00Z",
                    canceled_at=None,
                    execution={},
                )
            ],
            next_cursor=None,
            has_next=False,
        )

    monkeypatch.setattr(mock_client, "list_orders", paged_orders)

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="1",
        price="150",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "opposite pending order exists" in res["error"]
    assert seen_cursors == [None, "next-page"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_place_order_fails_closed_when_pending_order_page_has_no_cursor(
    monkeypatch,
):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    async def broken_paged_orders(**kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(orders=[], next_cursor=None, has_next=True)

    monkeypatch.setattr(mock_client, "list_orders", broken_paged_orders)

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="1",
        price="150",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "pagination cursor" in res["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_place_sell_blocks_pending_buy_order_before_post(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "ord-existing",
            "symbol": "AAPL",
            "side": "BUY",
            "status": "OPEN",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("150"),
            "quantity": Decimal("1"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {},
        }
    ]

    res = await toss_place_order(
        symbol="AAPL",
        side="sell",
        quantity="1",
        price="150",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "opposite pending order exists" in res["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_modify_rejects_unsafe_id_and_missing_original_order(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    unsafe = await toss_modify_order(
        order_id="../bad",
        new_price="155",
        account_mode="toss_live",
    )
    assert unsafe["success"] is False
    assert "Unsafe order id rejected" in unsafe["error"]
    assert mock_client.get_order_calls == 0

    missing = await toss_modify_order(
        order_id="missing-order",
        new_price="155",
        account_mode="toss_live",
    )
    assert missing["success"] is False
    assert "Order not found" in missing["error"]


@pytest.mark.asyncio
async def test_modify_us_requires_price_and_returns_dry_run_payload(monkeypatch):
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

    missing_price = await toss_modify_order(
        order_id="orig-ord-123",
        market="us",
        dry_run=True,
        account_mode="toss_live",
    )
    assert missing_price["success"] is False
    assert "requires new_price" in missing_price["error"]

    preview = await toss_modify_order(
        order_id="orig-ord-123",
        new_price="155",
        market="us",
        dry_run=True,
        account_mode="toss_live",
    )
    assert preview["success"] is True
    assert preview["mutation_sent"] is False
    assert preview["payload_preview"] == {"orderType": "LIMIT", "price": "155"}


@pytest.mark.asyncio
async def test_modify_kr_high_value_order_requires_explicit_confirm(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "kr-ord-123",
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
        order_id="kr-ord-123",
        new_price="50000",
        new_quantity="2000",
        market="kr",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "requires confirm_high_value_order=True" in res["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_cancel_dry_run_and_broker_error_paths(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    preview = await toss_cancel_order(
        order_id="orig-ord-123",
        account_mode="toss_live",
    )
    assert preview["success"] is True
    assert preview["mutation_sent"] is False
    assert preview["original_order_id"] == "orig-ord-123"

    unsafe = await toss_cancel_order(
        order_id="bad/id",
        account_mode="toss_live",
    )
    assert unsafe["success"] is False
    assert "Unsafe order id rejected" in unsafe["error"]

    async def raise_cancel_error(order_id):
        raise RuntimeError(f"cannot cancel {order_id}")

    monkeypatch.setattr(mock_client, "cancel_order", raise_cancel_error)

    # Seed the order so get_order succeeds before cancel_order is called
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

    failed = await toss_cancel_order(
        order_id="orig-ord-123",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )
    assert failed["success"] is False
    assert failed["mutation_sent"] is True
    assert "cannot cancel orig-ord-123" in failed["error"]


@pytest.mark.asyncio
async def test_read_tools_surface_broker_errors(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    async def raise_list_orders(**kwargs):
        raise RuntimeError("history down")

    async def raise_holdings(**kwargs):
        raise RuntimeError("positions down")

    async def raise_buying_power(**kwargs):
        raise RuntimeError("cash down")

    monkeypatch.setattr(mock_client, "list_orders", raise_list_orders)
    history = await toss_get_order_history(account_mode="toss_live")
    assert history["success"] is False
    assert "history down" in history["error"]

    monkeypatch.setattr(mock_client, "holdings", raise_holdings)
    positions = await toss_get_positions(account_mode="toss_live")
    assert positions["success"] is False
    assert "positions down" in positions["error"]

    monkeypatch.setattr(mock_client, "buying_power", raise_buying_power)
    cash = await toss_get_orderable_cash(account_mode="toss_live")
    assert cash["success"] is False
    assert "cash down" in cash["error"]


@pytest.mark.asyncio
async def test_order_history_json_safes_list_tuple_and_none_decimals(monkeypatch):
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
            "status": "OPEN",
            "order_type": "MARKET",
            "time_in_force": "DAY",
            "price": None,
            "quantity": Decimal("1"),
            "order_amount": Decimal("150"),
            "currency": "USD",
            "ordered_at": "2026-06-12T00:00:00Z",
            "canceled_at": None,
            "execution": {
                "fills": [Decimal("0.5"), (Decimal("0.25"), Decimal("0.25"))],
            },
        }
    ]

    res = await toss_get_order_history(status="open", account_mode="toss_live")

    assert res["success"] is True
    order = res["orders"][0]
    assert order["price"] is None
    assert order["order_amount"] == "150"
    assert order["execution"]["fills"] == ["0.5", ["0.25", "0.25"]]


@pytest.mark.asyncio
async def test_order_history_json_safes_datetime_timestamps(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    ordered_at = datetime(2026, 6, 12, 3, 0, tzinfo=UTC)
    canceled_at = datetime(2026, 6, 12, 3, 5, tzinfo=UTC)
    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "ord-1",
            "symbol": "AAPL",
            "side": "BUY",
            "status": "CLOSED",
            "order_type": "LIMIT",
            "time_in_force": "DAY",
            "price": Decimal("150"),
            "quantity": Decimal("1"),
            "order_amount": None,
            "currency": "USD",
            "ordered_at": ordered_at,
            "canceled_at": canceled_at,
            "execution": {},
        }
    ]

    res = await toss_get_order_history(account_mode="toss_live")

    assert res["success"] is True
    order = res["orders"][0]
    assert order["ordered_at"] == ordered_at.isoformat()
    assert order["canceled_at"] == canceled_at.isoformat()


@pytest.mark.asyncio
async def test_preview_order_includes_warnings(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.warnings_list = [
        {
            "warning_type": "OVERHEATED",
            "exchange": "KRX",
            "start_date": "2026-06-12",
            "end_date": None,
        }
    ]

    res = await toss_preview_order(
        symbol="005930",
        side="buy",
        quantity="10",
        price="70000",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert len(res["warnings"]) == 1
    assert res["warnings"][0]["warning_type"] == "OVERHEATED"


@pytest.mark.asyncio
async def test_place_order_blocked_by_liquidation_trading(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.warnings_list = [
        {
            "warning_type": "LIQUIDATION_TRADING",
            "exchange": "KRX",
            "start_date": "2026-06-12",
            "end_date": None,
        }
    ]

    res = await toss_place_order(
        symbol="005930",
        side="buy",
        quantity="10",
        price="70000",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert "blocked" in res["error"].lower()
    assert len(res["warnings"]) == 1
    assert res["warnings"][0]["warning_type"] == "LIQUIDATION_TRADING"


@pytest.mark.asyncio
async def test_place_order_records_accepted_only_toss_ledger(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    _ = MockTossClient(monkeypatch)

    recorded = {}

    async def fake_record_toss_place_order(**kwargs):
        recorded.update(kwargs)
        return {
            "ledger_id": 538,
            "broker_status": "accepted",
            "fill_recorded": False,
            "journal_created": False,
        }

    monkeypatch.setattr(
        otv,
        "record_toss_place_order",
        fake_record_toss_place_order,
    )

    res = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="2",
        price="190",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
        thesis="entry thesis",
        strategy="swing",
        report_item_uuid="11111111-1111-1111-1111-111111111111",
    )

    assert res["success"] is True
    assert res["mutation_sent"] is True
    assert res["ledger_id"] == 538
    assert res["broker_status"] == "accepted"
    assert res["fill_recorded"] is False
    assert res["warnings"] == []
    assert recorded["client_order_id"] == res["client_order_id"]
    assert recorded["broker_order_id"] == res["order_id"]
    assert recorded["thesis"] == "entry thesis"
    assert recorded["strategy"] == "swing"
    assert recorded["report_item_uuid"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_modify_order_records_replacement_chain(monkeypatch):
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

    recorded = []

    async def fake_record(**kwargs):
        recorded.append(kwargs)
        return {"ledger_id": 538}

    monkeypatch.setattr(otv, "record_toss_replacement_order", fake_record)
    res = await toss_modify_order(
        order_id="orig-ord-123",
        new_price="155.0",
        market="us",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert len(recorded) == 1
    call = recorded[0]
    assert call["original_order_id"] == "orig-ord-123"
    assert call["replacement_order_id"] == "mod-ord-456"
    assert call["operation_kind"] == "modify"
    assert call["symbol"] == "AAPL"
    assert call["side"] == "buy"


@pytest.mark.asyncio
async def test_cancel_order_records_audit_replacement_chain(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    mock_client.orders_list = [
        {
            "order_id": "orig-ord-789",
            "symbol": "005930",
            "side": "SELL",
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

    recorded = []

    async def fake_record(**kwargs):
        recorded.append(kwargs)
        return {"ledger_id": 538}

    monkeypatch.setattr(otv, "record_toss_replacement_order", fake_record)
    res = await toss_cancel_order(
        order_id="orig-ord-789",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert len(recorded) == 1
    call = recorded[0]
    assert call["original_order_id"] == "orig-ord-789"
    assert call["replacement_order_id"] == "can-ord-789"
    assert call["operation_kind"] == "cancel"
    assert call["symbol"] == "005930"
    assert call["side"] == "sell"


async def _warm_sellable_cache(monkeypatch, otv, symbol: str) -> TossSellableCache:
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = TossSellableCache(ttl_seconds=600, redis_client=redis_client)
    await cache.put(symbol, Decimal("10"))
    monkeypatch.setattr(
        otv,
        "get_shared_sellable_cache",
        lambda: cache,
        raising=False,
    )
    return cache


def _sell_order(order_id: str, symbol: str = "AAPL") -> dict:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": "SELL",
        "status": "OPEN",
        "order_type": "LIMIT",
        "time_in_force": "DAY",
        "price": Decimal("150"),
        "quantity": Decimal("10"),
        "order_amount": None,
        "currency": "USD",
        "ordered_at": "2026-06-12T00:00:00Z",
        "canceled_at": None,
        "execution": {},
    }


@pytest.mark.asyncio
async def test_sellable_cache_invalidated_after_sell_place_even_if_ledger_fails(
    monkeypatch,
):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    client = MockTossClient(monkeypatch)
    client.holdings_list = [
        {
            "symbol": "AAPL",
            "quantity": Decimal("10"),
            "average_purchase_price": Decimal("100"),
            "last_price": Decimal("190"),
            "name": "Apple",
            "market_country": "US",
            "currency": "USD",
            "market_value": {},
            "profit_loss": {},
            "daily_profit_loss": {},
            "cost": {},
        }
    ]
    cache = await _warm_sellable_cache(monkeypatch, otv, "AAPL")

    async def fail_ledger(**kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(otv, "record_toss_place_order", fail_ledger)

    result = await toss_place_order(
        symbol="AAPL",
        side="sell",
        quantity="1",
        price="190",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert result["success"] is False
    assert result["order_id"] == "new-ord-123"
    assert await cache.get("AAPL") is None


@pytest.mark.asyncio
async def test_sellable_cache_not_invalidated_after_buy_place(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    MockTossClient(monkeypatch)
    cache = await _warm_sellable_cache(monkeypatch, otv, "AAPL")
    monkeypatch.setattr(
        otv,
        "record_toss_place_order",
        AsyncMock(return_value={"ledger_id": 1}),
    )

    result = await toss_place_order(
        symbol="AAPL",
        side="buy",
        quantity="1",
        price="190",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert result["success"] is True
    assert await cache.get("AAPL") == Decimal("10")


@pytest.mark.asyncio
async def test_sellable_cache_invalidated_after_sell_modify(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    client = MockTossClient(monkeypatch)
    client.orders_list = [_sell_order("orig-ord-123")]
    client.holdings_list = [
        {
            "symbol": "AAPL",
            "quantity": Decimal("10"),
            "average_purchase_price": Decimal("100"),
            "last_price": Decimal("155"),
            "name": "Apple",
            "market_country": "US",
            "currency": "USD",
            "market_value": {},
            "profit_loss": {},
            "daily_profit_loss": {},
            "cost": {},
        }
    ]
    cache = await _warm_sellable_cache(monkeypatch, otv, "AAPL")
    monkeypatch.setattr(
        otv,
        "record_toss_replacement_order",
        AsyncMock(return_value={"ledger_id": 2}),
    )

    result = await toss_modify_order(
        order_id="orig-ord-123",
        new_price="155",
        market="us",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert result["success"] is True
    assert await cache.get("AAPL") is None


@pytest.mark.asyncio
async def test_sellable_cache_invalidated_after_sell_cancel(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    client = MockTossClient(monkeypatch)
    client.orders_list = [_sell_order("orig-ord-789")]
    cache = await _warm_sellable_cache(monkeypatch, otv, "AAPL")
    monkeypatch.setattr(
        otv,
        "record_toss_replacement_order",
        AsyncMock(return_value={"ledger_id": 3}),
    )

    result = await toss_cancel_order(
        order_id="orig-ord-789",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert result["success"] is True
    assert await cache.get("AAPL") is None


@pytest.mark.asyncio
async def test_private_place_impl_accepts_client_order_id_override(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    recorded: dict[str, object] = {}

    async def fake_record_toss_place_order(**kwargs):
        recorded.update(kwargs)
        return {
            "ledger_id": 777,
            "broker_status": "accepted",
            "fill_recorded": False,
            "journal_created": False,
        }

    monkeypatch.setattr(otv, "record_toss_place_order", fake_record_toss_place_order)

    result = await otv._toss_place_order_impl(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="1",
        price="50000",
        order_amount=None,
        market="kr",
        time_in_force="DAY",
        dry_run=False,
        confirm=True,
        confirm_high_value_order=False,
        reason="ROB-539 smoke",
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        report_item_uuid=None,
        account_mode="toss_live",
        account_type=None,
        client_order_id_override="abc123def456abc123def456abc123de",
    )

    assert result["success"] is True
    assert (
        mock_client.placed_payloads[0]["clientOrderId"]
        == "abc123def456abc123def456abc123de"
    )
    assert recorded["client_order_id"] == "abc123def456abc123def456abc123de"
    assert result["approval_hash_digest"] == recorded["approval_hash"]


@pytest.mark.asyncio
async def test_public_place_order_uses_private_proposal_binding(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv

    seen: dict[str, object] = {}

    async def fake_impl(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(otv, "_toss_place_order_impl", fake_impl)
    with otv._bind_order_proposal_context(
        client_order_id="tosprop-0123456789abcdef",
        correlation_id="proposal-correlation-r1",
        rung=1,
    ):
        result = await otv.toss_place_order(
            symbol="005930",
            side="buy",
            quantity=1,
            price=50000,
            market="kr",
        )

    assert result["success"] is True
    assert seen["client_order_id_override"] == "tosprop-0123456789abcdef"
    assert otv._order_proposal_context.get() is None


def test_public_place_order_does_not_expose_client_order_id_override():
    assert (
        "client_order_id_override" not in inspect.signature(toss_place_order).parameters
    )


def test_private_proposal_binding_restores_nested_contexts():
    import app.mcp_server.tooling.orders_toss_variants as otv

    with otv._bind_order_proposal_context(
        client_order_id="tosprop-outer",
        correlation_id="corr-outer",
        rung=0,
    ):
        assert otv._order_proposal_context.get().client_order_id == "tosprop-outer"
        with otv._bind_order_proposal_context(
            client_order_id="tosprop-inner",
            correlation_id="corr-inner",
            rung=1,
        ):
            assert otv._order_proposal_context.get().client_order_id == (
                "tosprop-inner"
            )
        assert otv._order_proposal_context.get().client_order_id == "tosprop-outer"
    assert otv._order_proposal_context.get() is None


def test_private_proposal_binding_restores_after_exception():
    import app.mcp_server.tooling.orders_toss_variants as otv

    with pytest.raises(RuntimeError, match="binding failure"):
        with otv._bind_order_proposal_context(
            client_order_id="tosprop-exception",
            correlation_id="corr-exception",
            rung=2,
        ):
            raise RuntimeError("binding failure")
    assert otv._order_proposal_context.get() is None


@pytest.mark.asyncio
async def test_private_proposal_binding_reaches_toss_ledger_for_rung_one(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    mock_client = MockTossClient(monkeypatch)
    recorded: dict[str, object] = {}

    async def fake_record_toss_place_order(**kwargs):
        recorded.update(kwargs)
        return {
            "ledger_id": 778,
            "broker_status": "accepted",
            "fill_recorded": False,
            "journal_created": False,
            "correlation_id": kwargs["correlation_id_override"],
        }

    monkeypatch.setattr(otv, "record_toss_place_order", fake_record_toss_place_order)
    with otv._bind_order_proposal_context(
        client_order_id="tosprop-fedcba9876543210",
        correlation_id="proposal-correlation-r1",
        rung=1,
    ):
        result = await otv.toss_place_order(
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity="1",
            price="50000",
            market="kr",
            dry_run=False,
            confirm=True,
            account_mode="toss_live",
        )

    assert result["success"] is True
    assert result["correlation_id"] == "proposal-correlation-r1"
    assert recorded["correlation_id_override"] == "proposal-correlation-r1"
    assert recorded["rung"] == 1
    assert mock_client.placed_payloads[0]["clientOrderId"] == (
        "tosprop-fedcba9876543210"
    )


@pytest.mark.asyncio
async def test_private_place_impl_rejects_unsafe_client_order_id_override(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    result = await otv._toss_place_order_impl(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="1",
        price="50000",
        order_amount=None,
        market="kr",
        time_in_force="DAY",
        dry_run=False,
        confirm=True,
        confirm_high_value_order=False,
        reason=None,
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        report_item_uuid=None,
        account_mode="toss_live",
        account_type=None,
        client_order_id_override="../bad",
    )

    assert result["success"] is False
    assert "Unsafe client order id rejected" in result["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_private_place_impl_rejects_whitespace_padded_client_order_id_override(
    monkeypatch,
):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    result = await otv._toss_place_order_impl(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="1",
        price="50000",
        order_amount=None,
        market="kr",
        time_in_force="DAY",
        dry_run=False,
        confirm=True,
        confirm_high_value_order=False,
        reason=None,
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        report_item_uuid=None,
        account_mode="toss_live",
        account_type=None,
        client_order_id_override=" abc123def456abc123def456abc123de ",
    )

    assert result["success"] is False
    assert "Unsafe client order id rejected" in result["error"]
    assert not mock_client.placed_payloads


@pytest.mark.asyncio
async def test_preview_emits_approval_hash_and_deterministic_client_order_id(
    monkeypatch,
):
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    fixed = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    monkeypatch.setattr(otv, "now_kst", lambda: fixed)

    res1 = await otv.toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        account_mode="toss_live",
    )
    res2 = await otv.toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        account_mode="toss_live",
    )
    assert res1["success"] is True
    assert res1["approval_hash"].startswith("p6a1.")
    assert res1["approval_expires_at"]  # ISO string
    cid = res1["payload_preview"]["clientOrderId"]
    assert cid.startswith("tossp6-")
    # deterministic: identical params + same trading day -> identical id + token payload
    assert res2["payload_preview"]["clientOrderId"] == cid


@pytest.mark.asyncio
async def test_preview_uses_private_proposal_client_order_id(monkeypatch):
    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    with otv._bind_order_proposal_context(
        client_order_id="tosprop-0011223344556677",
        correlation_id=None,
        rung=1,
    ):
        result = await otv.toss_preview_order(
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity="10",
            price="70000",
            market="kr",
            account_mode="toss_live",
            rung=1,
        )

    assert result["payload_preview"]["clientOrderId"] == ("tosprop-0011223344556677")


@pytest.mark.asyncio
async def test_preview_rung_discriminator_changes_client_order_id(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))

    base = await otv.toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        account_mode="toss_live",
    )
    r2 = await otv.toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        rung=2,
        account_mode="toss_live",
    )
    assert (
        base["payload_preview"]["clientOrderId"]
        != r2["payload_preview"]["clientOrderId"]
    )


@pytest.mark.asyncio
async def test_place_dry_run_matching_hash_passes(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))

    prev = await otv.toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        account_mode="toss_live",
    )
    res = await otv.toss_place_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        dry_run=True,
        approval_hash=prev["approval_hash"],
        account_mode="toss_live",
    )
    assert res["success"] is True
    # placed clientOrderId matches previewed (idempotent)
    assert res["client_order_id"] == prev["payload_preview"]["clientOrderId"]


@pytest.mark.asyncio
async def test_place_mismatched_hash_fails_closed_with_diff(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))

    prev = await otv.toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        account_mode="toss_live",
    )
    res = await otv.toss_place_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70100",
        market="kr",  # price differs
        dry_run=True,
        approval_hash=prev["approval_hash"],
        account_mode="toss_live",
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_mismatch"
    assert "price" in res["diff"]


@pytest.mark.asyncio
async def test_place_expired_hash_requires_repreview(monkeypatch):
    from datetime import datetime, timedelta

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    issued = datetime(2026, 7, 2, 10, 0, tzinfo=KST)
    monkeypatch.setattr(otv, "now_kst", lambda: issued)
    prev = await otv.toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        account_mode="toss_live",
    )
    monkeypatch.setattr(otv, "now_kst", lambda: issued + timedelta(seconds=301))
    res = await otv.toss_place_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        dry_run=True,
        approval_hash=prev["approval_hash"],
        account_mode="toss_live",
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_expired"


@pytest.mark.asyncio
async def test_place_optional_mode_without_hash_passes(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    monkeypatch.setattr(
        otv.settings, "toss_approval_hash_mode", "optional", raising=False
    )
    res = await otv.toss_place_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        dry_run=True,
        account_mode="toss_live",
    )
    assert res["success"] is True


@pytest.mark.asyncio
async def test_place_required_mode_without_hash_fails_closed(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    monkeypatch.setattr(
        otv.settings, "toss_approval_hash_mode", "required", raising=False
    )
    res = await otv.toss_place_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        dry_run=True,
        account_mode="toss_live",
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_required"


@pytest.mark.asyncio
async def test_place_warn_mode_without_hash_passes_and_logs(monkeypatch, caplog):
    import logging
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    monkeypatch.setattr(otv.settings, "toss_approval_hash_mode", "warn", raising=False)

    with caplog.at_level(logging.WARNING, logger=otv.logger.name):
        res = await otv.toss_place_order(
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity="10",
            price="70000",
            market="kr",
            dry_run=True,
            account_mode="toss_live",
        )
    assert res["success"] is True
    assert any("without approval_hash" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_place_off_mode_ignores_mismatched_hash(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))

    # Preview under any mode to mint a valid token bound to price=70000.
    prev = await otv.toss_preview_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70000",
        market="kr",
        account_mode="toss_live",
    )

    # off mode: verification is skipped entirely, so a token bound to a
    # *different* order must NOT fail-close.
    monkeypatch.setattr(otv.settings, "toss_approval_hash_mode", "off", raising=False)
    res = await otv.toss_place_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="10",
        price="70100",  # differs from the previewed 70000
        market="kr",
        dry_run=True,
        approval_hash=prev["approval_hash"],
        account_mode="toss_live",
    )
    assert res["success"] is True
    assert "error_code" not in res


@pytest.mark.asyncio
async def test_client_order_id_same_day_stable_next_day_new(monkeypatch):
    from datetime import datetime

    from app.core.timezone import KST

    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "005930", "last_price": Decimal("70000"), "currency": "KRW"}
    ]

    def _prev():
        return otv.toss_preview_order(
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity="10",
            price="70000",
            market="kr",
            account_mode="toss_live",
        )

    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 10, 0, tzinfo=KST))
    day1_a = await _prev()
    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 2, 15, 0, tzinfo=KST))
    day1_b = await _prev()
    monkeypatch.setattr(otv, "now_kst", lambda: datetime(2026, 7, 3, 10, 0, tzinfo=KST))
    day2 = await _prev()

    cid1a = day1_a["payload_preview"]["clientOrderId"]
    cid1b = day1_b["payload_preview"]["clientOrderId"]
    cid2 = day2["payload_preview"]["clientOrderId"]
    assert cid1a == cid1b  # same trading day -> broker/ledger dedupe key
    assert cid1a != cid2  # next trading day -> new order allowed
