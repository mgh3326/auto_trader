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
async def test_get_order_history_validation_error():
    tools = build_tools()
    # status != pending and no symbol => error (even if order_id is present)
    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_order_history"](status="filled", order_id="some-id")


@pytest.mark.asyncio
async def test_get_order_history_filters(monkeypatch):
    tools = build_tools()

    # Mock data with mixed sides and order_ids
    orders_data = [
        # KRW-BTC orders
        {
            "uuid": "bid-1", "market": "KRW-BTC", "side": "bid", "state": "done",
            "created_at": "2025-01-02", "ord_type": "limit", "price": "100", "volume": "1", "remaining_volume": "0", "executed_volume": "1"
        },
        {
            "uuid": "ask-1", "market": "KRW-BTC", "side": "ask", "state": "done",
            "created_at": "2025-01-01", "ord_type": "limit", "price": "110", "volume": "1", "remaining_volume": "0", "executed_volume": "1"
        },
    ]

    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_closed_orders", AsyncMock(return_value=orders_data))
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_open_orders", AsyncMock(return_value=[]))

    # Test 1: Filter by side="buy"
    res_buy = await tools["get_order_history"](symbol="KRW-BTC", status="filled", side="buy")
    assert len(res_buy["orders"]) == 1
    assert res_buy["orders"][0]["order_id"] == "bid-1"
    assert res_buy["orders"][0]["side"] == "buy"

    # Test 2: Filter by side="sell"
    res_sell = await tools["get_order_history"](symbol="KRW-BTC", status="filled", side="sell")
    assert len(res_sell["orders"]) == 1
    assert res_sell["orders"][0]["order_id"] == "ask-1"
    assert res_sell["orders"][0]["side"] == "sell"

    # Test 3: Filter by order_id
    res_id = await tools["get_order_history"](symbol="KRW-BTC", status="filled", order_id="bid-1")
    assert len(res_id["orders"]) == 1
    assert res_id["orders"][0]["order_id"] == "bid-1"
    
    # Test 4: Filter by order_id without symbol (should attempt heuristic or all)
    # This requires fetch logic to run without symbol.
    # For crypto, our impl calls fetch_closed_orders ONLY if normalized_symbol is present for history.
    # So if we pass status="filled", order_id="bid-1" with NO symbol:
    # Logic: "if status in ... and normalized_symbol: ..." -> won't fetch history from Upbit if no symbol.
    # Wait, looking at my impl:
    # "if status in ("all", "filled", "cancelled") and normalized_symbol:"
    # So retrieving specific order history by ID without symbol is NOT supported for Upbit closed orders in this impl.
    # It IS supported for Pending (fetch_open_orders(market=None)).
    
    # Let's test pending by ID without symbol
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_open_orders", AsyncMock(return_value=[
        {
            "uuid": "pending-1", "market": "KRW-ETH", "side": "bid", "state": "wait",
            "created_at": "2025-01-03", "ord_type": "limit", "price": "200", "volume": "1", "remaining_volume": "1", "executed_volume": "0"
        }
    ]))
    
    res_pending_id = await tools["get_order_history"](status="pending", order_id="pending-1")
    assert len(res_pending_id["orders"]) == 1
    assert res_pending_id["orders"][0]["order_id"] == "pending-1"


@pytest.mark.asyncio
async def test_get_order_history_pending_without_symbol(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            return [{
                "uuid": "uuid-1", "side": "bid", "ord_type": "limit",
                "price": "50000000", "volume": "0.1", "remaining_volume": "0.1",
                "market": "KRW-BTC", "created_at": "2025-01-01T00:00:00",
                "state": "wait"
            }]

    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_open_orders", MockUpbitService().fetch_open_orders)
    
    class MockKIS:
        async def inquire_korea_orders(self): return []
        async def inquire_overseas_orders(self, exchange_code): return []
    
    monkeypatch.setattr(mcp_tools, "KISClient", lambda: MockKIS())

    result = await tools["get_order_history"](status="pending")
    
    assert len(result["orders"]) == 1
    assert result["orders"][0]["order_id"] == "uuid-1"
    # source check removed as per plan normalisation requirements



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
    # Mock open orders to return empty (since status="all" by default calls both)
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_open_orders", AsyncMock(return_value=[]))

    # explicitly status='filled'
    result = await tools["get_order_history"](
        symbol="KRW-BTC", status="filled", days=7, limit=20
    )

    assert result["market"] == "crypto"
    assert len(result["orders"]) == 1
    assert result["orders"][0]["order_id"] == "order-1"
    assert result["orders"][0]["side"] == "buy"
    assert result["summary"]["filled"] == 1
    mock_closed_orders.assert_awaited_once_with(market="KRW-BTC", limit=20)


@pytest.mark.asyncio
async def test_get_order_history_limit_logic(monkeypatch):
    tools = build_tools()
    
    # Mock valid response
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_closed_orders", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_open_orders", AsyncMock(return_value=[]))

    # Limit = 0 => Should pass limit=100 (or max) to service or similar logic
    # Our impl: limit=0 or -1 means unlimited (in our logic we used if limit > 0 else 100 for closed_orders)
    # Check if no error
    await tools["get_order_history"](symbol="KRW-BTC", limit=0)
    await tools["get_order_history"](symbol="KRW-BTC", limit=-1)
    
    with pytest.raises(ValueError, match="limit must be >= -1"):
         await tools["get_order_history"](symbol="KRW-BTC", limit=-2)


@pytest.mark.asyncio
async def test_get_order_history_truncated_response(monkeypatch):
    tools = build_tools()
    
    orders = []
    for i in range(10):
        orders.append({
            "uuid": f"id-{i}", "market": "KRW-BTC", "side": "bid", "state": "done",
            "created_at": f"2025-01-0{i+1}",
            "volume": "1", "remaining_volume": "0", "executed_volume": "1",
            "price": "100"
        })
        
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_closed_orders", AsyncMock(return_value=orders))
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_open_orders", AsyncMock(return_value=[]))
    
    # limit=5, should get 5 orders and truncated=True
    result = await tools["get_order_history"](symbol="KRW-BTC", status="filled", limit=5)
    
    
    assert len(result["orders"]) == 5
    assert result["truncated"] is True
    assert result["total_available"] == 10


@pytest.mark.asyncio
async def test_get_order_history_kr_order_id_normalizes_list_response(monkeypatch):
    tools = build_tools()

    class FakeKIS:
        async def inquire_daily_order_domestic(self, **kwargs):
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
        async def inquire_korea_orders(self): return []

    monkeypatch.setattr(mcp_tools, "KISClient", lambda: FakeKIS())

    # status="filled" to trigger inquire_daily_order_domestic
    result = await tools["get_order_history"](
        symbol="005930",
        status="filled",
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
        async def inquire_overseas_orders(self, *args, **kwargs): return []

    monkeypatch.setattr(mcp_tools, "KISClient", lambda: FakeKIS())

    caplog.set_level("INFO")
    result = await tools["get_order_history"](
        symbol="QQQ", status="filled", days=7, limit=20
    )

    assert result["market"] == "us"
    assert len(result["orders"]) == 2
    order_ids = [o["order_id"] for o in result["orders"]]
    assert "US-OD-1" in order_ids
    assert "US-OD-2" in order_ids
    assert result["summary"]["filled"] == 2


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
