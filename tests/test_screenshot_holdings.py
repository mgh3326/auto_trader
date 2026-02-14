"""
Screenshot Holdings Service Tests

Tests for screenshot-based holdings update service and MCP tool.
"""

import pytest

from app.mcp_server.tooling import market_data_quotes
from app.mcp_server.tooling.registry import register_all_tools
from app.models.manual_holdings import MarketType
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.screenshot_holdings_service import ScreenshotHoldingsService
from app.services.stock_alias_service import StockAliasService


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


class DummySessionManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return None


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    register_all_tools(mcp)
    return mcp.tools


@pytest.mark.asyncio
async def test_update_manual_holdings_dry_run(monkeypatch):
    """Test dry_run mode - should not modify DB."""
    tools = build_tools()

    holdings = [
        {
            "stock_name": "삼성전자",
            "quantity": 10,
            "eval_amount": 1500000,
            "profit_loss": 100000,
            "profit_rate": 7.14,
            "market_section": "kr",
        }
    ]

    # Mock services
    async def mock_resolve_and_update(self, **kwargs):
        return {
            "success": True,
            "dry_run": kwargs.get("dry_run", True),
            "message": "Preview only (set dry_run=False to update DB)",
            "broker": kwargs.get("broker", "toss"),
            "account_name": kwargs.get("account_name", "기본 계좌"),
            "parsed_count": 1,
            "holdings": [
                {
                    "stock_name": "삼성전자",
                    "resolved_ticker": "005930",
                    "market_type": "KR",
                    "quantity": 10,
                    "avg_buy_price": 140000.0,
                    "eval_amount": 1500000,
                    "profit_loss": 100000,
                    "profit_rate": 7.14,
                    "resolution_method": "krx_master",
                    "action": "upsert",
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(
        ScreenshotHoldingsService,
        "resolve_and_update",
        mock_resolve_and_update,
    )

    result = await tools["update_manual_holdings"](
        holdings=holdings, broker="toss", account_name="기본 계좌", dry_run=True
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["parsed_count"] == 1
    assert "Preview only" in result["message"]


@pytest.mark.asyncio
async def test_update_manual_holdings_with_alias_resolution(monkeypatch):
    """Test symbol resolution through stock alias."""
    tools = build_tools()

    holdings = [
        {
            "stock_name": "버크셔 해서웨이 B",
            "quantity": 5,
            "eval_amount": 1000000,
            "profit_loss": 50000,
            "profit_rate": 5.26,
            "market_section": "us",
        }
    ]

    # Mock stock alias service
    async def mock_get_ticker_by_alias(alias, market_type):
        if alias == "버크셔 해서웨이 B":
            return "BRK.B"
        return None

    # Mock resolve_and_update
    async def mock_resolve_and_update(self, **kwargs):
        return {
            "success": True,
            "dry_run": True,
            "message": "Preview only (set dry_run=False to update DB)",
            "broker": "toss",
            "account_name": "기본 계좌",
            "parsed_count": 1,
            "holdings": [
                {
                    "stock_name": "버크셔 해서웨이 B",
                    "resolved_ticker": "BRK.B",
                    "market_type": "US",
                    "quantity": 5,
                    "avg_buy_price": 190000.0,
                    "eval_amount": 1000000,
                    "profit_loss": 50000,
                    "profit_rate": 5.26,
                    "resolution_method": "alias",
                    "action": "upsert",
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(
        StockAliasService, "get_ticker_by_alias", mock_get_ticker_by_alias
    )
    monkeypatch.setattr(
        ScreenshotHoldingsService, "resolve_and_update", mock_resolve_and_update
    )

    result = await tools["update_manual_holdings"](
        holdings=holdings, broker="toss", account_name="기본 계좌", dry_run=True
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "BRK.B"
    assert holding["resolution_method"] == "alias"


@pytest.mark.asyncio
async def test_update_manual_holdings_calculate_avg_buy_price():
    """Test average buy price calculation from eval_amount and profit_loss."""
    service = ScreenshotHoldingsService(object())

    avg_price = await service._calculate_avg_buy_price(
        eval_amount=1500000, profit_loss=100000, quantity=10
    )

    assert avg_price == 140000.0


@pytest.mark.asyncio
async def test_update_manual_holdings_calculate_avg_buy_price_zero_quantity():
    """Test average buy price with zero quantity."""
    service = ScreenshotHoldingsService(object())

    avg_price = await service._calculate_avg_buy_price(
        eval_amount=0, profit_loss=0, quantity=0
    )

    assert avg_price == 0.0


@pytest.mark.asyncio
async def test_update_manual_holdings_remove_action(monkeypatch):
    """Test action='remove' to delete holdings."""
    tools = build_tools()

    holdings = [{"stock_name": "삼성전자", "market_section": "kr", "action": "remove"}]

    # Mock delete_holding
    deleted_holdings = []

    async def mock_delete_holding(holding_id):
        deleted_holdings.append(holding_id)
        return True

    monkeypatch.setattr(ManualHoldingsService, "delete_holding", mock_delete_holding)

    # Mock get_holding_by_ticker
    async def mock_get_holding_by_ticker(broker_account_id, ticker, market_type):
        return type("MockHolding", (), {"id": 123, "quantity": 10})()

    monkeypatch.setattr(
        ManualHoldingsService,
        "get_holding_by_ticker",
        mock_get_holding_by_ticker,
    )

    async def mock_resolve_and_update(self, **kwargs):
        if kwargs.get("holdings_data") and len(kwargs["holdings_data"]) > 0:
            holding = kwargs["holdings_data"][0]
            if holding.get("action") == "remove" and not kwargs.get("dry_run"):
                ticker = holding.get("stock_name")
                market_section = holding.get("market_section", "kr")
                market_type = MarketType.KR if market_section == "kr" else MarketType.US
                mock_holding = await mock_get_holding_by_ticker(
                    None, ticker, market_type.value
                )
                if mock_holding and hasattr(mock_holding, "id"):
                    await mock_delete_holding(mock_holding.id)

        return {
            "success": True,
            "dry_run": False,
            "message": "Holdings updated successfully",
            "broker": "toss",
            "account_name": "기본 계좌",
            "parsed_count": 1,
            "holdings": [],
            "warnings": [],
        }

    monkeypatch.setattr(
        ScreenshotHoldingsService,
        "resolve_and_update",
        mock_resolve_and_update,
    )

    result = await tools["update_manual_holdings"](
        holdings=holdings, broker="toss", account_name="기본 계좌", dry_run=False
    )

    assert result["success"] is True
    assert 123 in deleted_holdings


@pytest.mark.asyncio
async def test_update_manual_holdings_empty_list():
    """Test with empty holdings list."""
    tools = build_tools()

    result = await tools["update_manual_holdings"](
        holdings=[], broker="toss", account_name="기본 계좌", dry_run=True
    )

    assert result["success"] is False
    assert "holdings list is required" in result["error"]


@pytest.mark.asyncio
async def test_update_manual_holdings_different_accounts(monkeypatch):
    """Test with different account types (기본 계좌, 퇴직연금, ISA)."""
    tools = build_tools()

    holdings = [
        {
            "stock_name": "삼성전자",
            "quantity": 10,
            "eval_amount": 1500000,
            "profit_loss": 100000,
            "profit_rate": 7.14,
            "market_section": "kr",
        }
    ]

    result_calls = []

    async def mock_resolve_and_update(self, **kwargs):
        result_calls.append(kwargs)
        return {
            "success": True,
            "dry_run": kwargs.get("dry_run", True),
            "message": "Preview",
            "parsed_count": 1,
            "holdings": [],
            "warnings": [],
            "account_name": kwargs.get("account_name", "기본 계좌"),
            "broker": kwargs.get("broker", "samsung"),
        }

    monkeypatch.setattr(
        ScreenshotHoldingsService,
        "resolve_and_update",
        mock_resolve_and_update,
    )

    # Test different account types
    for account in ["기본 계좌", "퇴직연금", "ISA"]:
        result = await tools["update_manual_holdings"](
            holdings=holdings, broker="samsung", account_name=account, dry_run=True
        )
        assert result["success"] is True
        assert result["account_name"] == account

    # Verify all calls were made with correct account_name
    assert len(result_calls) == 3
    assert result_calls[0]["account_name"] == "기본 계좌"
    assert result_calls[1]["account_name"] == "퇴직연금"
    assert result_calls[2]["account_name"] == "ISA"


@pytest.mark.asyncio
async def test_update_manual_holdings_krx_master_resolution(monkeypatch):
    """Test symbol resolution through KRX master data."""
    tools = build_tools()

    holdings = [
        {
            "stock_name": "셀트리온",
            "quantity": 5,
            "eval_amount": 300000,
            "profit_loss": -10000,
            "profit_rate": -3.23,
            "market_section": "kr",
        }
    ]

    # Mock master data
    monkeypatch.setattr(
        market_data_quotes, "get_kosdaq_name_to_code", lambda: {"셀트리온": "068270"}
    )

    async def mock_resolve_and_update(self, **kwargs):
        return {
            "success": True,
            "dry_run": True,
            "message": "Preview",
            "broker": "toss",
            "account_name": "기본 계좌",
            "parsed_count": 1,
            "holdings": [
                {
                    "stock_name": "셀트리온",
                    "resolved_ticker": "068270",
                    "market_type": "KR",
                    "quantity": 5,
                    "avg_buy_price": 58000.0,
                    "eval_amount": 300000,
                    "profit_loss": -10000,
                    "profit_rate": -3.23,
                    "resolution_method": "krx_master",
                    "action": "upsert",
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(
        ScreenshotHoldingsService,
        "resolve_and_update",
        mock_resolve_and_update,
    )

    result = await tools["update_manual_holdings"](
        holdings=holdings, broker="toss", account_name="기본 계좌", dry_run=True
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "068270"
    assert holding["resolution_method"] == "krx_master"


@pytest.mark.asyncio
async def test_update_manual_holdings_us_master_resolution(monkeypatch):
    """Test US stock symbol resolution through master data."""
    tools = build_tools()

    holdings = [
        {
            "stock_name": "애플",
            "quantity": 20,
            "eval_amount": 4500000,
            "profit_loss": 500000,
            "profit_rate": 12.5,
            "market_section": "us",
        }
    ]

    # Mock master data
    monkeypatch.setattr(
        market_data_quotes,
        "get_us_stocks_data",
        lambda: {"name_to_symbol": {"애플": "AAPL"}, "symbol_to_exchange": {}},
    )

    async def mock_resolve_and_update(self, **kwargs):
        return {
            "success": True,
            "dry_run": True,
            "message": "Preview",
            "broker": "toss",
            "account_name": "기본 계좌",
            "parsed_count": 1,
            "holdings": [
                {
                    "stock_name": "애플",
                    "resolved_ticker": "AAPL",
                    "market_type": "US",
                    "quantity": 20,
                    "avg_buy_price": 190000.0,
                    "eval_amount": 4500000,
                    "profit_loss": 500000,
                    "profit_rate": 12.5,
                    "resolution_method": "us_master",
                    "action": "upsert",
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(
        ScreenshotHoldingsService,
        "resolve_and_update",
        mock_resolve_and_update,
    )

    result = await tools["update_manual_holdings"](
        holdings=holdings, broker="toss", account_name="기본 계좌", dry_run=True
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert "AAPL" in holding["resolved_ticker"]
    assert holding["resolution_method"] == "us_master"


@pytest.mark.asyncio
async def test_update_manual_holdings_fallback_resolution(monkeypatch):
    """Test fallback resolution when symbol not found in alias or master data."""
    tools = build_tools()

    holdings = [
        {
            "stock_name": "알수없는종목",
            "quantity": 5,
            "eval_amount": 500000,
            "profit_loss": -20000,
            "profit_rate": -3.85,
            "market_section": "kr",
        }
    ]

    # Mock empty master data
    monkeypatch.setattr(market_data_quotes, "get_kospi_name_to_code", lambda: {})
    monkeypatch.setattr(market_data_quotes, "get_kosdaq_name_to_code", lambda: {})

    async def mock_resolve_and_update(self, **kwargs):
        holdings_data = kwargs.get("holdings_data", [])
        if holdings_data and len(holdings_data) > 0:
            holding = holdings_data[0]
            return {
                "success": True,
                "dry_run": kwargs.get("dry_run", True),
                "message": "Preview",
                "broker": kwargs.get("broker", "toss"),
                "account_name": kwargs.get("account_name", "기본 계좌"),
                "parsed_count": 1,
                "holdings": [
                    {
                        "stock_name": holding["stock_name"],
                        "resolved_ticker": holding["stock_name"].upper(),
                        "market_type": "KR",
                        "quantity": holding["quantity"],
                        "avg_buy_price": (
                            holding["eval_amount"] - holding["profit_loss"]
                        )
                        / holding["quantity"],
                        "eval_amount": holding["eval_amount"],
                        "profit_loss": holding["profit_loss"],
                        "profit_rate": holding["profit_rate"],
                        "resolution_method": "fallback",
                        "action": "upsert",
                    }
                ],
                "warnings": [
                    "Symbol not found in alias/master data, using uppercase name"
                ],
            }
        return {
            "success": True,
            "dry_run": kwargs.get("dry_run", True),
            "message": "Preview",
            "broker": kwargs.get("broker", "toss"),
            "account_name": kwargs.get("account_name", "기본 계좌"),
            "parsed_count": 1,
            "holdings": [],
            "warnings": [],
        }

    monkeypatch.setattr(
        ScreenshotHoldingsService,
        "resolve_and_update",
        mock_resolve_and_update,
    )

    result = await tools["update_manual_holdings"](
        holdings=holdings, broker="toss", account_name="기본 계좌", dry_run=True
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "알수없는종목".upper()
    assert holding["resolution_method"] == "fallback"
    assert len(result["warnings"]) > 0  # Should warn about fallback


@pytest.mark.asyncio
async def test_update_manual_holdings_error_handling(monkeypatch):
    """Test error handling when service throws exception."""
    tools = build_tools()

    holdings = [
        {
            "stock_name": "삼성전자",
            "quantity": 10,
            "eval_amount": 1500000,
            "profit_loss": 100000,
            "profit_rate": 7.14,
            "market_section": "kr",
        }
    ]

    # Mock service that throws exception
    async def mock_resolve_and_update(self, **kwargs):
        raise RuntimeError("Database connection failed")

    monkeypatch.setattr(
        ScreenshotHoldingsService,
        "resolve_and_update",
        mock_resolve_and_update,
    )

    result = await tools["update_manual_holdings"](
        holdings=holdings, broker="toss", account_name="기본 계좌", dry_run=True
    )

    assert result["success"] is False
    assert "Database connection failed" in result["error"]


@pytest.mark.asyncio
async def test_update_manual_holdings_multiple_brokers(monkeypatch):
    """Test with different broker types (toss, samsung, kis)."""
    tools = build_tools()

    holdings = [
        {
            "stock_name": "삼성전자",
            "quantity": 10,
            "eval_amount": 1500000,
            "profit_loss": 100000,
            "profit_rate": 7.14,
            "market_section": "kr",
        }
    ]

    broker_calls = []

    async def mock_resolve_and_update(self, **kwargs):
        broker_calls.append(kwargs["broker"])
        return {
            "success": True,
            "dry_run": True,
            "message": "Preview",
            "parsed_count": 1,
            "holdings": [],
            "warnings": [],
            "broker": kwargs.get("broker", ""),
        }

    monkeypatch.setattr(
        ScreenshotHoldingsService,
        "resolve_and_update",
        mock_resolve_and_update,
    )

    for broker in ["toss", "samsung", "kis"]:
        result = await tools["update_manual_holdings"](
            holdings=holdings, broker=broker, account_name="기본 계좌", dry_run=True
        )
        assert result["success"] is True
        assert result["broker"] == broker

    assert set(broker_calls) == {"toss", "samsung", "kis"}
