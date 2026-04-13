"""Unit tests for PaperTradingService."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

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
