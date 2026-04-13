"""Unit tests for PaperTradingService."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.paper_trading import PaperAccount, PaperPosition, PaperTrade
from app.models.trading import InstrumentType
from app.services.paper_trading_service import (
    FEE_RATES,
    PaperTradingService,
    calculate_fee,
)


class TestCalculateFee:
    def test_equity_kr_buy(self):
        # 1,000,000원 매수 → 0.015% = 150원
        fee = calculate_fee("equity_kr", "buy", Decimal("1000000"))
        assert fee == Decimal("150.0000")

    def test_equity_kr_sell_includes_tax(self):
        # 1,000,000원 매도 → 수수료 0.015% + 세금 0.18% = 1,950원
        fee = calculate_fee("equity_kr", "sell", Decimal("1000000"))
        assert fee == Decimal("1950.0000")

    def test_equity_us_buy_min_fee(self):
        # 작은 금액: 100 USD * 0.07% = $0.07 → min $1
        fee = calculate_fee("equity_us", "buy", Decimal("100"))
        assert fee == Decimal("1.0000")

    def test_equity_us_buy_above_min(self):
        # 10,000 USD * 0.07% = $7
        fee = calculate_fee("equity_us", "buy", Decimal("10000"))
        assert fee == Decimal("7.0000")

    def test_crypto_buy(self):
        # 1,000,000 KRW * 0.05% = 500 KRW
        fee = calculate_fee("crypto", "buy", Decimal("1000000"))
        assert fee == Decimal("500.0000")

    def test_crypto_sell(self):
        fee = calculate_fee("crypto", "sell", Decimal("2000000"))
        assert fee == Decimal("1000.0000")

    def test_unsupported_market_raises(self):
        with pytest.raises(ValueError, match="Unsupported instrument_type"):
            calculate_fee("forex", "buy", Decimal("100"))

    def test_fee_rates_structure(self):
        assert FEE_RATES["equity_kr"]["buy"] == 0.00015
        assert FEE_RATES["equity_kr"]["tax_sell"] == 0.0018
        assert FEE_RATES["equity_us"]["min_fee_usd"] == 1.0
        assert FEE_RATES["crypto"]["sell"] == 0.0005


class TestAccountManagement:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    @pytest.mark.asyncio
    async def test_create_account_defaults(self, service, mock_db):
        account = await service.create_account(
            name="Test",
            initial_capital_krw=Decimal("10000000"),
        )
        assert account.name == "Test"
        assert account.initial_capital == Decimal("10000000")
        assert account.cash_krw == Decimal("10000000")
        assert account.cash_usd == Decimal("0")
        assert account.is_active is True
        mock_db.add.assert_called_once_with(account)
        mock_db.commit.assert_awaited_once()
        mock_db.refresh.assert_awaited_once_with(account)

    @pytest.mark.asyncio
    async def test_create_account_with_usd_and_meta(self, service, mock_db):
        account = await service.create_account(
            name="US Bot",
            initial_capital_krw=Decimal("0"),
            initial_capital_usd=Decimal("5000"),
            description="dollar-cost averaging",
            strategy_name="dca-us",
        )
        assert account.cash_usd == Decimal("5000")
        assert account.description == "dollar-cost averaging"
        assert account.strategy_name == "dca-us"

    @pytest.mark.asyncio
    async def test_reset_account_restores_cash_and_deletes_positions(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(
            id=1,
            name="Test",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("3000000"),
            cash_usd=Decimal("100"),
            is_active=True,
        )

        async def fake_get(account_id):
            assert account_id == 1
            return account

        monkeypatch.setattr(service, "get_account", fake_get)

        mock_db.execute = AsyncMock()
        result = await service.reset_account(1)

        assert result.cash_krw == Decimal("10000000")
        assert result.cash_usd == Decimal("0")
        # DELETE FROM paper_positions WHERE account_id = 1
        mock_db.execute.assert_awaited_once()
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_account_missing_raises(
        self, service, mock_db, monkeypatch
    ):
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=None))
        with pytest.raises(ValueError, match="Account 99 not found"):
            await service.reset_account(99)

    @pytest.mark.asyncio
    async def test_delete_account_returns_true_when_found(
        self, service, mock_db, monkeypatch
    ):
        account = PaperAccount(id=1, name="x", initial_capital=Decimal("0"),
                               cash_krw=Decimal("0"), cash_usd=Decimal("0"),
                               is_active=True)
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=account))
        mock_db.delete = AsyncMock()

        ok = await service.delete_account(1)

        assert ok is True
        mock_db.delete.assert_awaited_once_with(account)
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_account_returns_false_when_missing(
        self, service, monkeypatch
    ):
        monkeypatch.setattr(service, "get_account", AsyncMock(return_value=None))
        ok = await service.delete_account(42)
        assert ok is False


class TestFetchCurrentPrice:
    @pytest.fixture
    def service(self, mock_db):
        return PaperTradingService(mock_db)

    @pytest.mark.asyncio
    async def test_fetch_equity_kr_uses_kis_quote(self, service, monkeypatch):
        monkeypatch.setattr(
            "app.services.paper_trading_service._fetch_quote_equity_kr",
            AsyncMock(return_value={"price": 70000.0}),
        )
        price = await service._fetch_current_price("005930", "equity_kr")
        assert price == Decimal("70000.0")

    @pytest.mark.asyncio
    async def test_fetch_equity_us_uses_yahoo_quote(self, service, monkeypatch):
        monkeypatch.setattr(
            "app.services.paper_trading_service._fetch_quote_equity_us",
            AsyncMock(return_value={"price": 190.5}),
        )
        price = await service._fetch_current_price("AAPL", "equity_us")
        assert price == Decimal("190.5")

    @pytest.mark.asyncio
    async def test_fetch_crypto_uses_upbit_batch(self, service, monkeypatch):
        monkeypatch.setattr(
            "app.services.paper_trading_service.fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95000000.0}),
        )
        price = await service._fetch_current_price("KRW-BTC", "crypto")
        assert price == Decimal("95000000.0")

    @pytest.mark.asyncio
    async def test_fetch_crypto_missing_raises(self, service, monkeypatch):
        monkeypatch.setattr(
            "app.services.paper_trading_service.fetch_multiple_current_prices",
            AsyncMock(return_value={}),
        )
        with pytest.raises(ValueError, match="No price for KRW-BTC"):
            await service._fetch_current_price("KRW-BTC", "crypto")
