"""Tests for AI Markdown schemas"""

import pytest

from app.schemas.ai_markdown import (
    InvestmentProfile,
    PortfolioMarkdownRequest,
    PresetType,
    StockMarkdownRequest,
)


class TestPresetType:
    def test_preset_values(self):
        assert PresetType.PORTFOLIO_STANCE == "portfolio_stance"
        assert PresetType.STOCK_STANCE == "stock_stance"
        assert PresetType.STOCK_ADD_OR_HOLD == "stock_add_or_hold"


class TestPortfolioMarkdownRequest:
    def test_default_values(self):
        req = PortfolioMarkdownRequest()
        assert req.preset == PresetType.PORTFOLIO_STANCE
        assert req.include_market == "ALL"

    def test_custom_values(self):
        req = PortfolioMarkdownRequest(
            preset=PresetType.PORTFOLIO_STANCE, include_market="US"
        )
        assert req.include_market == "US"


class TestStockMarkdownRequest:
    def test_required_fields(self):
        with pytest.raises(ValueError):
            StockMarkdownRequest()

    def test_valid_request(self):
        req = StockMarkdownRequest(
            preset=PresetType.STOCK_STANCE, symbol="AAPL", market_type="US"
        )
        assert req.symbol == "AAPL"
        assert req.market_type == "US"


class TestInvestmentProfile:
    def test_default_profile(self):
        profile = InvestmentProfile()
        assert "분할매수" in profile.style
        assert "손절" in profile.stop_loss_philosophy

    def test_to_markdown(self):
        profile = InvestmentProfile()
        md = profile.to_markdown()
        assert "- " in md
        assert "분할매수" in md
