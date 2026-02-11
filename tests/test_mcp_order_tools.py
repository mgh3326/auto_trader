"""MCP order tool tests for get_order_history and modify_order."""

from unittest.mock import AsyncMock

import pytest

from app.mcp_server import tools as mcp_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    mcp_tools.register_tools(mcp)
    return mcp.tools


@pytest.mark.asyncio
async def test_get_order_history_crypto_uses_closed_orders(monkeypatch):
    tools = build_tools()

    mock_closed_orders = AsyncMock(
        return_value=[
            {
                "uuid": "order-1",
                "market": "KRW-BTC",
                "side": "bid",
                "ord_type": "limit",
                "price": "50000000",
                "remaining_volume": "0",
                "executed_volume": "0.001",
                "state": "done",
                "avg_price": "49900000",
                "created_at": "2025-02-10T09:30:00",
                "done_at": "2025-02-10T09:31:00",
            }
        ]
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service, "fetch_closed_orders", mock_closed_orders
    )

    result = await tools["get_order_history"](
        symbol="KRW-BTC", market="crypto", days=7, limit=20
    )

    assert result["market"] == "crypto"
    assert len(result["orders"]) == 1
    assert result["orders"][0]["order_id"] == "order-1"
    assert result["orders"][0]["side"] == "buy"
    assert result["summary"]["filled"] == 1
    mock_closed_orders.assert_awaited_once_with(market="KRW-BTC", limit=100)


@pytest.mark.asyncio
async def test_get_order_history_side_filter_applies_before_limit(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_closed_orders",
        AsyncMock(
            return_value=[
                {
                    "uuid": "sell-first",
                    "market": "KRW-BTC",
                    "side": "ask",
                    "ord_type": "limit",
                    "price": "50000000",
                    "remaining_volume": "1",
                    "executed_volume": "0",
                    "state": "wait",
                },
                {
                    "uuid": "buy-second",
                    "market": "KRW-BTC",
                    "side": "bid",
                    "ord_type": "limit",
                    "price": "49000000",
                    "remaining_volume": "0",
                    "executed_volume": "1",
                    "state": "done",
                },
            ]
        ),
    )

    result = await tools["get_order_history"](
        symbol="KRW-BTC",
        market="crypto",
        side="buy",
        limit=1,
    )

    assert len(result["orders"]) == 1
    assert result["orders"][0]["order_id"] == "buy-second"
    assert result["orders"][0]["side"] == "buy"


@pytest.mark.asyncio
async def test_get_order_history_kr_order_id_normalizes_list_response(monkeypatch):
    tools = build_tools()

    class FakeKIS:
        async def inquire_daily_order_domestic(self, **kwargs):
            assert kwargs["order_number"] == "KR-OD-1"
            return [
                {
                    "ord_no": "KR-OD-1",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "005930",
                    "ord_qty": "2",
                    "ccld_qty": "2",
                    "ord_unpr": "80000",
                    "ccld_unpr": "79900",
                    "ord_dt": "20250210",
                    "ord_tmd": "093000",
                    "prcs_stat_name": "체결",
                }
            ]

    monkeypatch.setattr(mcp_tools, "KISClient", lambda: FakeKIS())

    result = await tools["get_order_history"](
        symbol="005930",
        market="kr",
        order_id="KR-OD-1",
        limit=20,
    )

    assert result["market"] == "kr"
    assert len(result["orders"]) == 1
    assert result["orders"][0]["order_id"] == "KR-OD-1"


@pytest.mark.asyncio
async def test_modify_order_dry_run_contract(monkeypatch):
    tools = build_tools()

    result = await tools["modify_order"](
        order_id="od-1",
        symbol="KRW-BTC",
        market="crypto",
        new_price=56000000,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["status"] == "simulated"
    assert result["market"] == "crypto"
    assert result["method"] == "dry_run"
    assert result["changes"]["price"]["to"] == 56000000


@pytest.mark.asyncio
async def test_modify_order_crypto_success(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "uuid": "od-1",
                "state": "wait",
                "ord_type": "limit",
                "price": "50000000",
                "remaining_volume": "0.001",
            }
        ),
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "cancel_and_reorder",
        AsyncMock(
            return_value={
                "cancel_result": {"uuid": "od-1"},
                "new_order": {"uuid": "od-2"},
            }
        ),
    )

    result = await tools["modify_order"](
        order_id="od-1",
        symbol="KRW-BTC",
        market="crypto",
        new_price=49000000,
        new_quantity=0.002,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["status"] == "modified"
    assert result["method"] == "cancel_reorder"
    assert result["new_order_id"] == "od-2"


@pytest.mark.asyncio
async def test_modify_order_us_falls_back_exchange(monkeypatch):
    tools = build_tools()

    class FakeKIS:
        def __init__(self) -> None:
            self.modify_exchange: str | None = None

        async def inquire_overseas_orders(
            self, exchange_code: str = "NASD", is_mock=False
        ):
            if exchange_code == "NYSE":
                return [
                    {
                        "odno": "US-OD-1",
                        "ft_ord_unpr3": "207.0",
                        "ft_ord_qty": "2",
                    }
                ]
            return []

        async def modify_overseas_order(
            self,
            order_number: str,
            symbol: str,
            exchange_code: str,
            quantity: int,
            new_price: float,
            is_mock: bool = False,
        ):
            self.modify_exchange = exchange_code
            return {"odno": "US-OD-2", "msg": "ok"}

    fake_kis = FakeKIS()
    monkeypatch.setattr(mcp_tools, "KISClient", lambda: fake_kis)
    monkeypatch.setattr(mcp_tools, "get_exchange_by_symbol", lambda _symbol: "NASD")

    result = await tools["modify_order"](
        order_id="US-OD-1",
        symbol="AMZN",
        market="us",
        new_price=195.0,
        new_quantity=2,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["status"] == "modified"
    assert result["new_order_id"] == "US-OD-2"
    assert fake_kis.modify_exchange == "NYSE"


@pytest.mark.asyncio
async def test_get_order_history_us_deduplicates_by_order_id(monkeypatch, caplog):
    """Test that duplicate order IDs in US order history are deduplicated."""
    tools = build_tools()

    class FakeKIS:
        async def inquire_daily_order_overseas(self, **kwargs):
            return [
                {
                    "odno": "US-OD-1",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "QQQ",
                    "ft_ord_qty": "2",
                    "ft_ccld_qty": "2",
                    "ft_ord_unpr3": "500.0",
                    "ft_ccld_unpr3": "500.0",
                    "ord_dt": "20250210",
                    "ord_tmd": "093000",
                    "prcs_stat_name": "체결",
                },
                {
                    "odno": "US-OD-1",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "QQQ",
                    "ft_ord_qty": "2",
                    "ft_ccld_qty": "2",
                    "ft_ord_unpr3": "500.0",
                    "ft_ccld_unpr3": "500.0",
                    "ord_dt": "20250210",
                    "ord_tmd": "093000",
                    "prcs_stat_name": "체결",
                },
                {
                    "odno": "US-OD-2",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "QQQ",
                    "ft_ord_qty": "1",
                    "ft_ccld_qty": "1",
                    "ft_ord_unpr3": "495.0",
                    "ft_ccld_unpr3": "495.0",
                    "ord_dt": "20250210",
                    "ord_tmd": "092000",
                    "prcs_stat_name": "체결",
                },
            ]

    monkeypatch.setattr(mcp_tools, "KISClient", lambda: FakeKIS())

    caplog.set_level("INFO")
    result = await tools["get_order_history"](
        symbol="QQQ", market="us", days=7, limit=20
    )

    assert result["market"] == "us"
    assert len(result["orders"]) == 2
    order_ids = [o["order_id"] for o in result["orders"]]
    assert order_ids == ["US-OD-1", "US-OD-2"]
    assert result["summary"]["filled"] == 2


@pytest.mark.asyncio
async def test_get_open_orders_kr_order_id_uppercase(monkeypatch, caplog):
    """Test that get_open_orders handles uppercase field names for KR orders."""
    tools = build_tools()

    class FakeKIS:
        async def inquire_korea_orders(self):
            return [
                {
                    "ORD_NO": "KR-OD-UPPER",
                    "SLL_BUY_DVSN_CD": "02",
                    "PDNO": "005930",
                    "ORD_QTY": "2",
                    "ORD_UNPR": "80000",
                    "ORD_TMD": "093000",
                }
            ]

    monkeypatch.setattr(mcp_tools, "KISClient", lambda: FakeKIS())

    result = await tools["get_open_orders"](market="kr")

    assert result["total_count"] == 1
    assert result["orders"][0]["order_id"] == "KR-OD-UPPER"
    assert result["orders"][0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_get_open_orders_kr_order_id_lowercase(monkeypatch, caplog):
    """Test that get_open_orders handles lowercase field names for KR orders."""
    tools = build_tools()

    class FakeKIS:
        async def inquire_korea_orders(self):
            return [
                {
                    "ord_no": "KR-OD-LOWER",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "005930",
                    "ord_qty": "2",
                    "ord_unpr": "80000",
                    "ord_tmd": "093000",
                }
            ]

    monkeypatch.setattr(mcp_tools, "KISClient", lambda: FakeKIS())

    result = await tools["get_open_orders"](market="kr")

    assert result["total_count"] == 1
    assert result["orders"][0]["order_id"] == "KR-OD-LOWER"


@pytest.mark.asyncio
async def test_cancel_order_kr_uppercase_fields(monkeypatch):
    """Test that cancel_order handles uppercase field names for KR orders."""
    tools = build_tools()

    class FakeKIS:
        async def inquire_korea_orders(self):
            return [
                {
                    "ORD_NO": "KR-OD-UPPER",
                    "PDNO": "005930",
                    "SLL_BUY_DVSN_CD": "02",
                    "ORD_UNPR": "80000",
                    "ORD_QTY": "2",
                }
            ]

        async def cancel_korea_order(
            self, order_number, stock_code, quantity, price, order_type
        ):
            return {"odno": "KR-OD-UPPER", "ord_tmd": "093000"}

    monkeypatch.setattr(mcp_tools, "KISClient", lambda: FakeKIS())

    result = await tools["cancel_order"](order_id="KR-OD-UPPER", market="kr")

    assert result["success"] is True
    assert result["order_id"] == "KR-OD-UPPER"
    assert result["symbol"] == "005930"


@pytest.mark.asyncio
async def test_modify_order_kr_uppercase_fields(monkeypatch):
    """Test that modify_order handles uppercase field names for KR orders."""
    tools = build_tools()

    class FakeKIS:
        async def inquire_korea_orders(self):
            return [
                {
                    "ORD_NO": "KR-OD-UPPER",
                    "PDNO": "005930",
                    "SLL_BUY_DVSN_CD": "02",
                    "ORD_UNPR": "80000",
                    "ORD_QTY": "2",
                }
            ]

        async def modify_korea_order(self, order_id, stock_code, quantity, price):
            return {"odno": "KR-OD-UPPER"}

    monkeypatch.setattr(mcp_tools, "KISClient", lambda: FakeKIS())

    result = await tools["modify_order"](
        order_id="KR-OD-UPPER",
        symbol="005930",
        market="kr",
        new_price=79000,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["order_id"] == "KR-OD-UPPER"
    assert result["status"] == "modified"
