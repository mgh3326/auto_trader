"""
Screenshot Holdings Service Resolution Tests

Tests for symbol resolution and input validation in screenshot_holdings_service.
These tests mock external dependencies but exercise the actual resolve_and_update logic.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.screenshot_holdings_service import ScreenshotHoldingsService
from app.services.stock_alias_service import StockAliasService


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    db = MagicMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def mock_broker_account():
    """Create a mock broker account."""
    account = MagicMock()
    account.id = 1
    return account


@pytest.fixture
def service(mock_db):
    """Create service instance with mock DB."""
    return ScreenshotHoldingsService(mock_db)


def _setup_mocks(
    monkeypatch,
    mock_db,
    mock_broker_account,
    existing_holdings=None,
    crypto_maps=None,
    kospi_map=None,
    kosdaq_map=None,
    us_stocks=None,
    alias_ticker=None,
):
    """Helper to set up all required mocks."""
    existing_holdings = existing_holdings or []
    crypto_maps = crypto_maps or {"NAME_TO_PAIR_KR": {}, "COIN_TO_NAME_KR": {}}
    kospi_map = kospi_map or {}
    kosdaq_map = kosdaq_map or {}
    us_stocks = us_stocks or {"name_to_symbol": {}}

    mock_db.execute.return_value = MagicMock()
    mock_db.execute.return_value.scalars.return_value.all.return_value = (
        existing_holdings
    )

    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.get_or_refresh_maps",
        AsyncMock(return_value=crypto_maps),
    )
    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.get_kospi_name_to_code",
        lambda: kospi_map,
    )
    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.get_kosdaq_name_to_code",
        lambda: kosdaq_map,
    )
    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.get_us_stocks_data",
        lambda: us_stocks,
    )

    async def mock_get_ticker_by_alias(self, alias, market_type):
        return alias_ticker

    monkeypatch.setattr(
        StockAliasService, "get_ticker_by_alias", mock_get_ticker_by_alias
    )

    async def mock_get_account(self, user_id, broker_type, account_name):
        return mock_broker_account

    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.BrokerAccountService.get_account_by_user_and_broker",
        mock_get_account,
    )

    async def mock_create_holding(self, **kwargs):
        h = MagicMock()
        h.id = 999
        h.ticker = kwargs.get("ticker")
        h.market_type = kwargs.get("market_type")
        h.quantity = kwargs.get("quantity")
        h.avg_price = kwargs.get("avg_price")
        return h

    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.ManualHoldingsService.create_holding",
        mock_create_holding,
    )

    async def mock_update_holding(self, holding_id, **kwargs):
        return True

    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.ManualHoldingsService.update_holding",
        mock_update_holding,
    )

    async def mock_delete_holding(self, holding_id):
        return True

    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.ManualHoldingsService.delete_holding",
        mock_delete_holding,
    )


@pytest.mark.asyncio
async def test_crypto_korean_name_ethereum(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Scenario 1: '이더리움' + market_section='crypto' -> KRW-ETH, CRYPTO, crypto_name_kr"""
    crypto_maps = {
        "NAME_TO_PAIR_KR": {"이더리움": "KRW-ETH", "비트코인": "KRW-BTC"},
        "COIN_TO_NAME_KR": {"ETH": "이더리움", "BTC": "비트코인"},
    }
    _setup_mocks(monkeypatch, mock_db, mock_broker_account, crypto_maps=crypto_maps)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "이더리움",
                "quantity": 1,
                "eval_amount": 3000000,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["parsed_count"] == 1
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "KRW-ETH"
    assert holding["market_type"] == "CRYPTO"
    assert holding["resolution_method"] == "crypto_name_kr"


@pytest.mark.asyncio
async def test_crypto_korean_name_solana(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Scenario 2: '솔라나' -> KRW-SOL"""
    crypto_maps = {
        "NAME_TO_PAIR_KR": {"솔라나": "KRW-SOL"},
        "COIN_TO_NAME_KR": {"SOL": "솔라나"},
    }
    _setup_mocks(monkeypatch, mock_db, mock_broker_account, crypto_maps=crypto_maps)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "솔라나",
                "quantity": 10,
                "eval_amount": 2000000,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "KRW-SOL"
    assert holding["market_type"] == "CRYPTO"
    assert holding["resolution_method"] == "crypto_name_kr"


@pytest.mark.asyncio
async def test_avg_buy_price_direct_input(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Scenario 3: avg_buy_price 직접 입력 시 입력값 그대로 반영"""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "symbol": "KRW-ETH",
                "quantity": 1,
                "avg_buy_price": 3500000,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["avg_buy_price"] == 3500000.0
    assert holding["resolution_method"] == "direct"


@pytest.mark.asyncio
async def test_avg_buy_price_calculated_from_eval_profit_qty(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Scenario 4: avg_buy_price 미입력 + eval_amount/profit_loss/quantity로 역산 반영"""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "symbol": "KRW-ETH",
                "quantity": 2,
                "eval_amount": 6000000,
                "profit_loss": 200000,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["avg_buy_price"] == 2900000.0


@pytest.mark.asyncio
async def test_kr_stock_regression(service, mock_db, mock_broker_account, monkeypatch):
    """Scenario 5: KR 회귀 - '삼성전자' + market_section='kr' -> KRX 해석 유지"""
    kospi_map = {"삼성전자": "005930"}
    _setup_mocks(monkeypatch, mock_db, mock_broker_account, kospi_map=kospi_map)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "삼성전자",
                "quantity": 10,
                "eval_amount": 750000,
                "profit_loss": 50000,
                "market_section": "kr",
            }
        ],
        broker="toss",
        dry_run=True,
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "005930"
    assert holding["market_type"] == "KR"
    assert holding["resolution_method"] == "krx_master"


@pytest.mark.asyncio
async def test_symbol_direct_upsert(service, mock_db, mock_broker_account, monkeypatch):
    """Scenario 6: symbol='KRW-ETH' + market_section='crypto'에서 direct upsert 동작"""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "symbol": "KRW-ETH",
                "quantity": 1,
                "avg_buy_price": 3000000,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "KRW-ETH"
    assert holding["resolution_method"] == "direct"
    assert holding["stock_name"] == "KRW-ETH"


@pytest.mark.asyncio
async def test_symbol_direct_remove(service, mock_db, mock_broker_account, monkeypatch):
    """Scenario 7: remove + direct symbol에서 기존 보유 매칭/삭제 호출 검증"""
    existing = MagicMock()
    existing.id = 123
    existing.ticker = "KRW-ETH"
    existing.market_type = MagicMock()
    existing.market_type.value = "CRYPTO"
    existing.quantity = Decimal("1")
    existing.avg_price = Decimal("3000000")

    _setup_mocks(
        monkeypatch, mock_db, mock_broker_account, existing_holdings=[existing]
    )

    delete_calls = []

    async def mock_delete_holding(self, holding_id):
        delete_calls.append(holding_id)
        return True

    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.ManualHoldingsService.delete_holding",
        mock_delete_holding,
    )

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "symbol": "KRW-ETH",
                "market_section": "crypto",
                "action": "remove",
            }
        ],
        broker="upbit",
        dry_run=False,
    )

    assert result["success"] is True
    assert len(delete_calls) == 1
    assert delete_calls[0] == 123
    assert any(
        d["action"] == "removed" and d["ticker"] == "KRW-ETH"
        for d in result.get("diff", [])
    )


@pytest.mark.asyncio
async def test_stock_name_remove_resolves_and_deletes(
    service, mock_db, mock_broker_account, monkeypatch
):
    """stock_name 기반 remove에서 crypto 해석 후 기존 보유가 삭제된다."""
    existing = MagicMock()
    existing.id = 456
    existing.ticker = "KRW-ETH"
    existing.market_type = MagicMock()
    existing.market_type.value = "CRYPTO"
    existing.quantity = Decimal("1")
    existing.avg_price = Decimal("3000000")

    crypto_maps = {"NAME_TO_PAIR_KR": {"이더리움": "KRW-ETH"}, "COIN_TO_NAME_KR": {}}
    _setup_mocks(
        monkeypatch,
        mock_db,
        mock_broker_account,
        existing_holdings=[existing],
        crypto_maps=crypto_maps,
    )

    delete_calls = []

    async def mock_delete_holding(self, holding_id):
        delete_calls.append(holding_id)
        return True

    monkeypatch.setattr(
        "app.services.screenshot_holdings_service.ManualHoldingsService.delete_holding",
        mock_delete_holding,
    )

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "이더리움",
                "market_section": "crypto",
                "action": "remove",
            }
        ],
        broker="upbit",
        dry_run=False,
    )

    assert result["success"] is True
    assert delete_calls == [456]
    assert any(
        d["action"] == "removed" and d["ticker"] == "KRW-ETH"
        for d in result.get("diff", [])
    )


@pytest.mark.asyncio
async def test_alias_precedence_over_crypto_map(
    service, mock_db, mock_broker_account, monkeypatch
):
    """alias가 crypto map보다 우선되어 ticker를 결정한다."""
    crypto_maps = {
        "NAME_TO_PAIR_KR": {"이더리움": "KRW-ETH"},
        "COIN_TO_NAME_KR": {"ETH": "이더리움"},
    }
    _setup_mocks(
        monkeypatch,
        mock_db,
        mock_broker_account,
        crypto_maps=crypto_maps,
        alias_ticker="KRW-ETH-ALIAS",
    )

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "이더리움",
                "quantity": 1,
                "avg_buy_price": 3000000,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "KRW-ETH-ALIAS"
    assert holding["resolution_method"] == "alias"


@pytest.mark.asyncio
async def test_market_section_missing_skip_warning(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Scenario 8: market_section 누락/오입력 시 skip + warning 검증"""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "이더리움",
                "quantity": 1,
                "market_section": "",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    assert len(result["warnings"]) >= 1
    assert "market_section" in result["warnings"][0].lower()
    assert result["parsed_count"] == 0


@pytest.mark.asyncio
async def test_market_section_invalid_skip_warning(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Scenario 8 (continued): invalid market_section 값"""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "이더리움",
                "quantity": 1,
                "market_section": "invalid_market",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    assert len(result["warnings"]) >= 1
    assert "market_section" in result["warnings"][0].lower()
    assert result["parsed_count"] == 0


@pytest.mark.asyncio
async def test_both_stock_name_and_symbol_empty(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Scenario 9: stock_name/symbol 모두 없음 시 skip + warning 검증"""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "quantity": 1,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    assert len(result["warnings"]) >= 1
    assert (
        "stock_name" in result["warnings"][0].lower()
        or "symbol" in result["warnings"][0].lower()
    )
    assert result["parsed_count"] == 0


@pytest.mark.asyncio
async def test_dry_run_response_contract(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Scenario 10: dry_run=True에서 diff/count 미포함, dry_run=False에서 포함 검증"""
    crypto_maps = {"NAME_TO_PAIR_KR": {"이더리움": "KRW-ETH"}, "COIN_TO_NAME_KR": {}}
    _setup_mocks(monkeypatch, mock_db, mock_broker_account, crypto_maps=crypto_maps)

    result_dry = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "이더리움",
                "quantity": 1,
                "avg_buy_price": 3000000,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result_dry["success"] is True
    assert result_dry["dry_run"] is True
    assert "added_count" not in result_dry
    assert "updated_count" not in result_dry
    assert "removed_count" not in result_dry
    assert "unchanged_count" not in result_dry
    assert "diff" not in result_dry
    assert "Preview only" in result_dry["message"]

    result_real = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "이더리움",
                "quantity": 1,
                "avg_buy_price": 3000000,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=False,
    )

    assert result_real["success"] is True
    assert result_real["dry_run"] is False
    assert "added_count" in result_real
    assert "updated_count" in result_real
    assert "removed_count" in result_real
    assert "unchanged_count" in result_real
    assert "diff" in result_real
    assert "updated successfully" in result_real["message"].lower()


@pytest.mark.asyncio
async def test_calculate_avg_buy_price_zero_quantity(service):
    """avg_buy_price calculation with zero quantity returns 0."""
    avg_price = await service._calculate_avg_buy_price(
        eval_amount=1000000, profit_loss=100000, quantity=0
    )
    assert avg_price == 0.0


@pytest.mark.asyncio
async def test_calculate_avg_buy_price_normal(service):
    """avg_buy_price calculation with normal values."""
    avg_price = await service._calculate_avg_buy_price(
        eval_amount=1500000, profit_loss=100000, quantity=10
    )
    assert avg_price == 140000.0


@pytest.mark.asyncio
async def test_crypto_unknown_coin_fallback(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Unknown crypto name falls back to uppercase name as ticker."""
    crypto_maps = {"NAME_TO_PAIR_KR": {}, "COIN_TO_NAME_KR": {}}
    _setup_mocks(monkeypatch, mock_db, mock_broker_account, crypto_maps=crypto_maps)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "존재안하는코인",
                "quantity": 1,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "존재안하는코인"
    assert holding["resolution_method"] == "fallback"


@pytest.mark.asyncio
async def test_symbol_uppercase_conversion(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Symbol is converted to uppercase."""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "symbol": "krw-eth",
                "quantity": 1,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    holding = result["holdings"][0]
    assert holding["resolved_ticker"] == "KRW-ETH"


@pytest.mark.asyncio
async def test_display_name_uses_stock_name_if_available(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Display name uses stock_name if available, else symbol."""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "stock_name": "이더리움",
                "symbol": "KRW-ETH",
                "quantity": 1,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["holdings"][0]["stock_name"] == "이더리움"


@pytest.mark.asyncio
async def test_display_name_uses_symbol_when_no_stock_name(
    service, mock_db, mock_broker_account, monkeypatch
):
    """Display name falls back to symbol when stock_name is empty."""
    _setup_mocks(monkeypatch, mock_db, mock_broker_account)

    result = await service.resolve_and_update(
        user_id=1,
        holdings_data=[
            {
                "symbol": "KRW-ETH",
                "quantity": 1,
                "market_section": "crypto",
            }
        ],
        broker="upbit",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["holdings"][0]["stock_name"] == "KRW-ETH"
