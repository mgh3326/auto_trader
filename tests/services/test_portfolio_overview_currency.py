import pytest

from app.services.merged_portfolio_service import MergedPortfolioService
from app.services.portfolio_overview_service import _MARKET_US, PortfolioOverviewService


class TestUSPortfolioCurrencyConversion:
    """Test US portfolio PnL calculation with KRW/USD currency mixing."""

    @pytest.fixture
    def mock_components_mixed_currency(self):
        """Mock components with KIS in USD and manual holdings in KRW."""
        return [
            {
                "market_type": _MARKET_US,
                "symbol": "AAPL",
                "name": "Apple Inc",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS 실계좌",
                "source": "live",
                "quantity": 10.0,
                "avg_price": 150.0,  # USD
                "current_price": 200.0,  # USD
                "evaluation": 2000.0,
                "profit_loss": 500.0,
                "profit_rate": 0.333,
            },
            {
                "market_type": _MARKET_US,
                "symbol": "AAPL",
                "name": "Apple Inc",
                "account_key": "manual:1",
                "broker": "toss",
                "account_name": "Toss",
                "source": "manual",
                "quantity": 5.0,
                "avg_price": 195000.0,  # KRW (approx $150 at 1300 rate)
                "current_price": None,
                "evaluation": None,
                "profit_loss": None,
                "profit_rate": None,
            },
        ]

    def test_aggregate_positions_converts_krw_to_usd(
        self, mock_components_mixed_currency
    ):
        """Test that KRW avg_prices are converted to USD when aggregating."""
        service = PortfolioOverviewService(db=None)
        usd_krw = 1300.0

        positions = service._aggregate_positions(
            mock_components_mixed_currency, usd_krw=usd_krw
        )

        assert len(positions) == 1
        position = positions[0]

        # Total quantity should be 15 (10 + 5)
        assert position["quantity"] == 15.0

        # avg_price should be weighted average in USD
        # (10 * 150 + 5 * 150) / 15 = 150 (after KRW conversion: 195000/1300 = 150)
        assert abs(position["avg_price"] - 150.0) < 0.01

        # Cost basis should be reasonable (2250 USD, not ~977K)
        expected_cost_basis = 15.0 * 150.0
        actual_cost_basis = position["quantity"] * position["avg_price"]
        assert abs(actual_cost_basis - expected_cost_basis) < 0.01

        # PnL should be positive (bought at 150, now at 200)
        assert position["profit_rate"] > 0
        assert abs(position["profit_rate"] - 0.333) < 0.01  # ~33.3% gain

    def test_aggregate_positions_without_conversion_gives_wrong_result(
        self, mock_components_mixed_currency
    ):
        """Verify that without conversion, PnL calculation is broken."""
        service = PortfolioOverviewService(db=None)

        # Without usd_krw rate, no conversion happens
        positions = service._aggregate_positions(
            mock_components_mixed_currency, usd_krw=None
        )

        position = positions[0]
        # avg_price would be way too high due to KRW values
        # This would cause negative PnL like the reported bug
        assert position["avg_price"] > 1000  # Unconverted KRW price

    def test_aggregate_positions_sets_evaluation_krw_for_us_positions(self):
        """Test that US positions have KRW-normalized valuation fields."""
        service = PortfolioOverviewService(db=None)
        usd_krw = 1300.0
        components = [
            {
                "market_type": _MARKET_US,
                "symbol": "AAPL",
                "name": "Apple Inc",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS",
                "source": "live",
                "quantity": 10.0,
                "avg_price": 150.0,
                "current_price": 200.0,
                "evaluation": 2000.0,
                "profit_loss": 500.0,
                "profit_rate": 0.333,
            }
        ]

        positions = service._aggregate_positions(components, usd_krw=usd_krw)

        assert len(positions) == 1
        pos = positions[0]
        assert pos["evaluation"] == 2000.0
        assert pos["evaluation_krw"] == 2600000.0
        assert pos["profit_loss"] == 500.0
        assert pos["profit_loss_krw"] == 650000.0

    def test_aggregate_positions_copies_evaluation_krw_for_kr_and_crypto_positions(
        self,
    ):
        """Test that KR/CRYPTO positions have evaluation_krw mirroring evaluation."""
        service = PortfolioOverviewService(db=None)
        components = [
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS",
                "source": "live",
                "quantity": 10.0,
                "avg_price": 70000.0,
                "current_price": 75000.0,
                "evaluation": 750000.0,
                "profit_loss": 50000.0,
                "profit_rate": 0.071,
            }
        ]

        positions = service._aggregate_positions(components, usd_krw=1300.0)

        assert len(positions) == 1
        pos = positions[0]
        assert pos["evaluation"] == 750000.0
        assert pos["evaluation_krw"] == 750000.0

    def test_aggregate_positions_handles_missing_usd_krw_for_us_positions(self):
        """Test that US positions have None for evaluation_krw when usd_krw is missing."""
        service = PortfolioOverviewService(db=None)
        components = [
            {
                "market_type": _MARKET_US,
                "symbol": "AAPL",
                "evaluation": 2000.0,
                "profit_loss": 500.0,
                "name": "Apple",
                "account_key": "k",
                "broker": "b",
                "account_name": "an",
                "source": "s",
                "quantity": 10,
                "avg_price": 150,
                "current_price": 200,
                "profit_rate": 0.33,
            }
        ]

        positions = service._aggregate_positions(components, usd_krw=None)

        assert len(positions) == 1
        pos = positions[0]
        assert pos["evaluation"] == 2000.0
        assert pos["evaluation_krw"] is None


class TestMergedPortfolioCurrencyConversion:
    """Test MergedPortfolioService currency conversion."""

    def test_finalize_holdings_converts_krw_avg_price(self):
        """Test _finalize_holdings converts KRW avg_price to USD."""
        from app.services.merged_portfolio_service import (
            HoldingInfo,
            MarketType,
            MergedHolding,
        )

        service = MergedPortfolioService(db=None)

        holding = MergedHolding(
            ticker="AAPL",
            name="Apple Inc",
            market_type=MarketType.US.value,
            current_price=200.0,  # USD
        )
        holding.holdings = [
            HoldingInfo(broker="kis", quantity=10, avg_price=150.0),  # USD
            HoldingInfo(broker="toss", quantity=5, avg_price=195000.0),  # KRW
        ]

        merged = {"AAPL": holding}
        usd_krw = 1300.0

        service._finalize_holdings(merged, usd_krw=usd_krw)

        # Toss avg_price should be converted from KRW to USD
        assert abs(holding.toss_avg_price - 150.0) < 0.01  # 195000/1300

        # Combined avg should be weighted average in USD
        assert abs(holding.combined_avg_price - 150.0) < 0.01

        # PnL should be calculated correctly
        expected_profit_rate = (200.0 - 150.0) / 150.0  # ~33.3%
        assert abs(holding.profit_rate - expected_profit_rate) < 0.01
