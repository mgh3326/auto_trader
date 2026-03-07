"""Unit tests for KIS trading orchestrator and market strategies."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.kis_trading_orchestrator import (
    DomesticStrategy,
    MarketStrategy,
    OverseasStrategy,
    TradingOrchestrator,
    _extract_overseas_order_id,
)
from app.jobs.kis_trading_types import (
    FailurePolicy,
    StepOutcome,
    StepResult,
    TradingContext,
)


# ==================== Helper Functions ====================


def create_mock_context(
    symbol: str = "005935",
    name: str = "삼성전자우",
    avg_price: float = 73800.0,
    current_price: float = 75850.0,
    quantity: int = 5,
    is_manual: bool = False,
    exchange_code: str = "",
    open_orders: list[dict] | None = None,
) -> TradingContext:
    """Create a mock TradingContext for testing."""
    stock = {
        "pdno": symbol,
        "prdt_name": name,
        "pchs_avg_pric": str(avg_price),
        "prpr": str(current_price),
        "hldg_qty": str(quantity),
        "ord_psbl_qty": str(quantity),
        "_is_manual": is_manual,
    }

    if exchange_code:
        stock["ovrs_pdno"] = symbol
        stock["ovrs_item_name"] = name
        stock["ovrs_excg_cd"] = exchange_code
        stock["last_price"] = current_price

    mock_kis = MagicMock()
    mock_strategy = MagicMock()

    context = TradingContext(
        stock=stock,
        open_orders=open_orders or [],
        kis=mock_kis,
        strategy=mock_strategy,
    )

    return context


def create_mock_step(
    name: str = "test_step",
    outcome: StepOutcome = None,
    failure_policy: FailurePolicy = FailurePolicy.CONTINUE,
    should_skip: bool = False,
):
    """Create a mock TradingStep for testing."""
    step = MagicMock()
    step.name = name
    step.failure_policy = failure_policy
    step.should_skip = MagicMock(return_value=should_skip)
    step.execute = AsyncMock(return_value=outcome or StepOutcome(
        result=StepResult.SUCCESS,
        message="Success",
    ))
    step._log_skip = MagicMock()
    return step


# ==================== _extract_overseas_order_id Tests ====================


class TestExtractOverseasOrderId:
    """Tests for _extract_overseas_order_id helper function."""

    def test_extracts_odno_lowercase(self):
        """Should extract order ID from 'odno' key."""
        order = {"odno": "12345", "symbol": "AAPL"}
        assert _extract_overseas_order_id(order) == "12345"

    def test_extracts_odno_uppercase(self):
        """Should extract order ID from 'ODNO' key."""
        order = {"ODNO": "67890", "symbol": "MSFT"}
        assert _extract_overseas_order_id(order) == "67890"

    def test_extracts_ord_no_lowercase(self):
        """Should extract order ID from 'ord_no' key."""
        order = {"ord_no": "11111", "symbol": "GOOG"}
        assert _extract_overseas_order_id(order) == "11111"

    def test_extracts_ord_no_uppercase(self):
        """Should extract order ID from 'ORD_NO' key."""
        order = {"ORD_NO": "22222", "symbol": "AMZN"}
        assert _extract_overseas_order_id(order) == "22222"

    def test_prioritizes_first_found_key(self):
        """Should return first found key value."""
        order = {"odno": "first", "ODNO": "second"}
        # odno comes first in the check order
        assert _extract_overseas_order_id(order) == "first"

    def test_returns_empty_string_when_not_found(self):
        """Should return empty string when no order ID key found."""
        order = {"symbol": "AAPL", "quantity": 10}
        assert _extract_overseas_order_id(order) == ""

    def test_strips_whitespace(self):
        """Should strip whitespace from order ID."""
        order = {"odno": "  12345  "}
        assert _extract_overseas_order_id(order) == "12345"

    def test_handles_empty_value(self):
        """Should return empty string for empty order ID value."""
        order = {"odno": "", "ORD_NO": "   "}
        assert _extract_overseas_order_id(order) == ""

    def test_converts_to_string(self):
        """Should convert numeric order ID to string."""
        order = {"odno": 12345}
        assert _extract_overseas_order_id(order) == "12345"


# ==================== DomesticStrategy Tests ====================


class TestDomesticStrategy:
    """Tests for DomesticStrategy market strategy."""

    def test_market_name_returns_korean(self):
        """market_name should return '국내주식'."""
        strategy = DomesticStrategy()
        assert strategy.market_name == "국내주식"

    def test_market_type_returns_korean(self):
        """market_type should return '국내주식'."""
        strategy = DomesticStrategy()
        assert strategy.market_type == "국내주식"

    def test_get_exchange_code_returns_empty(self):
        """get_exchange_code should return empty string for domestic stocks."""
        strategy = DomesticStrategy()
        stock = {"pdno": "005935", "prdt_name": "삼성전자우"}
        assert strategy.get_exchange_code(stock) == ""

    @pytest.mark.asyncio
    async def test_resolve_exchange_code_returns_empty(self):
        """resolve_exchange_code should return empty string for domestic stocks."""
        strategy = DomesticStrategy()
        result = await strategy.resolve_exchange_code("005935", {})
        assert result == ""

    @pytest.mark.asyncio
    async def test_fetch_holdings_returns_kis_holdings(self):
        """fetch_holdings should return KIS holdings."""
        strategy = DomesticStrategy()
        mock_kis = MagicMock()
        mock_kis.fetch_my_stocks = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session,
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService"
            ) as mock_service_cls,
        ):
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_service = MagicMock()
            mock_service.get_holdings_by_user = AsyncMock(return_value=[])
            mock_service_cls.return_value = mock_service

            holdings = await strategy.fetch_holdings(mock_kis)

        assert len(holdings) == 1
        assert holdings[0]["pdno"] == "005935"
        assert holdings[0]["prdt_name"] == "삼성전자우"

    @pytest.mark.asyncio
    async def test_fetch_holdings_merges_manual_holdings(self):
        """fetch_holdings should merge manual holdings from database."""
        strategy = DomesticStrategy()
        mock_kis = MagicMock()
        mock_kis.fetch_my_stocks = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )

        # Mock manual holding
        mock_manual = MagicMock()
        mock_manual.ticker = "035420"  # NAVER
        mock_manual.display_name = "NAVER"
        mock_manual.quantity = 10
        mock_manual.avg_price = 250000

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session,
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService"
            ) as mock_service_cls,
        ):
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_service = MagicMock()
            mock_service.get_holdings_by_user = AsyncMock(return_value=[mock_manual])
            mock_service_cls.return_value = mock_service

            holdings = await strategy.fetch_holdings(mock_kis)

        assert len(holdings) == 2
        # Check manual holding was added
        manual_holding = next((h for h in holdings if h.get("_is_manual")), None)
        assert manual_holding is not None
        assert manual_holding["pdno"] == "035420"
        assert manual_holding["_is_manual"] is True

    @pytest.mark.asyncio
    async def test_fetch_holdings_skips_duplicate_manual_holdings(self):
        """fetch_holdings should not add manual holding if already in KIS."""
        strategy = DomesticStrategy()
        mock_kis = MagicMock()
        mock_kis.fetch_my_stocks = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )

        # Mock manual holding with same ticker
        mock_manual = MagicMock()
        mock_manual.ticker = "005935"  # Same as KIS holding
        mock_manual.display_name = "삼성전자우"
        mock_manual.quantity = 10
        mock_manual.avg_price = 74000

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session,
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService"
            ) as mock_service_cls,
        ):
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=None)

            mock_service = MagicMock()
            mock_service.get_holdings_by_user = AsyncMock(return_value=[mock_manual])
            mock_service_cls.return_value = mock_service

            holdings = await strategy.fetch_holdings(mock_kis)

        # Should still be 1 (duplicate not added)
        assert len(holdings) == 1
        assert not holdings[0].get("_is_manual", False)

    @pytest.mark.asyncio
    async def test_fetch_open_orders_calls_kis(self):
        """fetch_open_orders should call inquire_korea_orders."""
        strategy = DomesticStrategy()
        mock_kis = MagicMock()
        mock_kis.inquire_korea_orders = AsyncMock(
            return_value=[
                {"pdno": "005935", "sll_buy_dvsn_cd": "02", "odno": "123"}
            ]
        )

        orders = await strategy.fetch_open_orders(mock_kis)

        mock_kis.inquire_korea_orders.assert_called_once_with(is_mock=False)
        assert len(orders) == 1

    def test_get_analyzer_returns_kis_analyzer(self):
        """get_analyzer should return KISAnalyzer instance."""
        strategy = DomesticStrategy()

        with patch("app.analysis.service_analyzers.KISAnalyzer") as mock_analyzer_cls:
            mock_analyzer_cls.return_value = MagicMock()
            analyzer = strategy.get_analyzer()
            mock_analyzer_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_current_price_for_manual_success(self):
        """fetch_current_price_for_manual should return price from API."""
        strategy = DomesticStrategy()
        mock_kis = MagicMock()
        mock_kis.fetch_fundamental_info = AsyncMock(
            return_value={"현재가": 76000}
        )

        price = await strategy.fetch_current_price_for_manual(
            mock_kis, "005935", 75000
        )

        assert price == 76000

    @pytest.mark.asyncio
    async def test_fetch_current_price_for_manual_fallback_on_error(self):
        """fetch_current_price_for_manual should return default on error."""
        strategy = DomesticStrategy()
        mock_kis = MagicMock()
        mock_kis.fetch_fundamental_info = AsyncMock(
            side_effect=Exception("API 오류")
        )

        price = await strategy.fetch_current_price_for_manual(
            mock_kis, "005935", 75000
        )

        assert price == 75000


# ==================== OverseasStrategy Tests ====================


class TestOverseasStrategy:
    """Tests for OverseasStrategy market strategy."""

    def test_market_name_returns_overseas(self):
        """market_name should return '해외주식'."""
        strategy = OverseasStrategy()
        assert strategy.market_name == "해외주식"

    def test_market_type_returns_overseas(self):
        """market_type should return '해외주식'."""
        strategy = OverseasStrategy()
        assert strategy.market_type == "해외주식"

    def test_get_exchange_code_returns_from_stock(self):
        """get_exchange_code should return exchange code from stock dict."""
        strategy = OverseasStrategy()
        stock = {"ovrs_pdno": "AAPL", "ovrs_excg_cd": "NASD"}
        assert strategy.get_exchange_code(stock) == "NASD"

    def test_get_exchange_code_normalizes_case(self):
        """get_exchange_code should return uppercase exchange code."""
        strategy = OverseasStrategy()
        stock = {"ovrs_pdno": "AAPL", "ovrs_excg_cd": "nasd"}
        assert strategy.get_exchange_code(stock) == "NASD"

    def test_get_exchange_code_returns_empty_when_missing(self):
        """get_exchange_code should return empty string when not in stock."""
        strategy = OverseasStrategy()
        stock = {"ovrs_pdno": "AAPL"}
        assert strategy.get_exchange_code(stock) == ""

    @pytest.mark.asyncio
    async def test_fetch_holdings_returns_kis_holdings(self):
        """fetch_holdings should return KIS overseas holdings."""
        strategy = OverseasStrategy()
        mock_kis = MagicMock()
        mock_kis.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc",
                    "ovrs_cblc_qty": "10",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "150",
                    "now_pric2": "160",
                    "ovrs_excg_cd": "NASD",
                }
            ]
        )

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session,
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService"
            ) as mock_service_cls,
            patch(
                "app.core.symbol.to_db_symbol",
                side_effect=lambda x: x,
            ),
        ):
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_service = MagicMock()
            mock_service.get_holdings_by_user = AsyncMock(return_value=[])
            mock_service_cls.return_value = mock_service

            holdings = await strategy.fetch_holdings(mock_kis)

        assert len(holdings) == 1
        assert holdings[0]["ovrs_pdno"] == "AAPL"

    @pytest.mark.asyncio
    async def test_fetch_open_orders_queries_all_exchanges(self):
        """fetch_open_orders should query NASD, NYSE, and AMEX."""
        strategy = OverseasStrategy()
        mock_kis = MagicMock()

        # Mock responses for different exchanges
        mock_kis.inquire_overseas_orders = AsyncMock(
            return_value=[
                {"odno": "123", "ovrs_pdno": "AAPL"},
            ]
        )

        orders = await strategy.fetch_open_orders(mock_kis)

        # Should be called 3 times (NASD, NYSE, AMEX)
        assert mock_kis.inquire_overseas_orders.call_count == 3

    @pytest.mark.asyncio
    async def test_fetch_open_orders_deduplicates_by_order_id(self):
        """fetch_open_orders should deduplicate orders by order ID."""
        strategy = OverseasStrategy()
        mock_kis = MagicMock()

        call_count = 0

        async def mock_inquire(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Same order ID from different exchanges
            if call_count == 1:
                return [{"odno": "123", "ovrs_pdno": "AAPL"}]
            elif call_count == 2:
                return [{"odno": "123", "ovrs_pdno": "AAPL"}]  # Duplicate
            else:
                return []

        mock_kis.inquire_overseas_orders = mock_inquire

        orders = await strategy.fetch_open_orders(mock_kis)

        # Should deduplicate to 1 order
        assert len(orders) == 1

    @pytest.mark.asyncio
    async def test_fetch_open_orders_handles_exchange_failure(self):
        """fetch_open_orders should continue if one exchange fails."""
        strategy = OverseasStrategy()
        mock_kis = MagicMock()

        call_count = 0

        async def mock_inquire(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"odno": "123", "ovrs_pdno": "AAPL"}]
            elif call_count == 2:
                raise Exception("Exchange not available")
            else:
                return [{"odno": "456", "ovrs_pdno": "MSFT"}]

        mock_kis.inquire_overseas_orders = mock_inquire

        orders = await strategy.fetch_open_orders(mock_kis)

        # Should have orders from successful exchanges
        assert len(orders) == 2

    def test_get_analyzer_returns_yahoo_analyzer(self):
        """get_analyzer should return YahooAnalyzer instance."""
        strategy = OverseasStrategy()

        with patch("app.analysis.service_analyzers.YahooAnalyzer") as mock_analyzer_cls:
            mock_analyzer_cls.return_value = MagicMock()
            analyzer = strategy.get_analyzer()
            mock_analyzer_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_exchange_code_from_stock(self):
        """resolve_exchange_code should return exchange from stock dict."""
        strategy = OverseasStrategy()
        stock = {"ovrs_excg_cd": "NASD"}

        result = await strategy.resolve_exchange_code("AAPL", stock)

        assert result == "NASD"

    @pytest.mark.asyncio
    async def test_resolve_exchange_code_from_database(self):
        """resolve_exchange_code should look up from database if not in stock."""
        strategy = OverseasStrategy()
        stock = {}

        with patch(
            "app.services.us_symbol_universe_service.get_us_exchange_by_symbol"
        ) as mock_get:
            mock_get.return_value = "NYSE"
            result = await strategy.resolve_exchange_code("MSFT", stock)
            mock_get.assert_called_once_with("MSFT")

        assert result == "NYSE"

    @pytest.mark.asyncio
    async def test_fetch_current_price_for_manual_success(self):
        """fetch_current_price_for_manual should return price from API."""
        strategy = OverseasStrategy()
        mock_kis = MagicMock()

        # Mock DataFrame response
        import pandas as pd
        mock_df = pd.DataFrame({"close": [150.0]})
        mock_kis.inquire_overseas_price = AsyncMock(return_value=mock_df)

        price = await strategy.fetch_current_price_for_manual(
            mock_kis, "AAPL", 100.0
        )

        assert price == 150.0

    @pytest.mark.asyncio
    async def test_fetch_current_price_for_manual_fallback_on_empty(self):
        """fetch_current_price_for_manual should return default on empty response."""
        strategy = OverseasStrategy()
        mock_kis = MagicMock()

        import pandas as pd
        mock_df = pd.DataFrame()  # Empty DataFrame
        mock_kis.inquire_overseas_price = AsyncMock(return_value=mock_df)

        price = await strategy.fetch_current_price_for_manual(
            mock_kis, "AAPL", 100.0
        )

        assert price == 100.0

    @pytest.mark.asyncio
    async def test_fetch_current_price_for_manual_fallback_on_error(self):
        """fetch_current_price_for_manual should return default on error."""
        strategy = OverseasStrategy()
        mock_kis = MagicMock()
        mock_kis.inquire_overseas_price = AsyncMock(
            side_effect=Exception("API 오류")
        )

        price = await strategy.fetch_current_price_for_manual(
            mock_kis, "AAPL", 100.0
        )

        assert price == 100.0


# ==================== TradingOrchestrator Tests ====================


class TestTradingOrchestrator:
    """Tests for TradingOrchestrator."""

    def test_initialization(self):
        """Orchestrator should store strategy and steps."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_steps = [MagicMock(), MagicMock()]

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=mock_steps,
        )

        assert orchestrator.strategy is mock_strategy
        assert orchestrator.steps == mock_steps

    @pytest.mark.asyncio
    async def test_run_returns_completed_when_no_holdings(self):
        """run should return completed status when no holdings."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(return_value=[])
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[],
        )

        result = await orchestrator.run(MagicMock())

        assert result["status"] == "completed"
        assert "없습니다" in result["message"]
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_run_processes_single_stock(self):
        """run should process a single stock and return results."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        # Create a mock step that succeeds
        mock_step = create_mock_step(
            name="test_step",
            outcome=StepOutcome(
                result=StepResult.SUCCESS,
                message="Success",
                data={"key": "value"},
            ),
        )

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[mock_step],
        )

        result = await orchestrator.run(MagicMock())

        assert result["status"] == "completed"
        assert len(result["results"]) == 1
        assert result["results"][0]["symbol"] == "005935"
        assert result["results"][0]["name"] == "삼성전자우"

    @pytest.mark.asyncio
    async def test_run_processes_multiple_stocks(self):
        """run should process multiple stocks sequentially."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                },
                {
                    "pdno": "035420",
                    "prdt_name": "NAVER",
                    "hldg_qty": "10",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "250000",
                    "prpr": "260000",
                },
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        mock_step = create_mock_step(name="test_step")

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[mock_step],
        )

        result = await orchestrator.run(MagicMock())

        assert result["status"] == "completed"
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_run_handles_step_skip(self):
        """run should handle skipped steps correctly."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        mock_step = create_mock_step(
            name="test_step",
            outcome=StepOutcome(
                result=StepResult.SKIP,
                message="Skip condition met",
            ),
        )

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[mock_step],
        )

        result = await orchestrator.run(MagicMock())

        assert result["status"] == "completed"
        assert result["results"][0]["steps"][0]["result"]["skipped"] is True

    @pytest.mark.asyncio
    async def test_run_handles_step_failure_with_continue_policy(self):
        """run should continue when step fails with CONTINUE policy."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        # Step that fails with CONTINUE policy
        failing_step = create_mock_step(
            name="failing_step",
            outcome=StepOutcome(
                result=StepResult.FAILURE,
                message="Step failed",
            ),
            failure_policy=FailurePolicy.CONTINUE,
        )

        # Next step that should still execute
        success_step = create_mock_step(
            name="success_step",
            outcome=StepOutcome(
                result=StepResult.SUCCESS,
                message="Success",
            ),
        )

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[failing_step, success_step],
        )

        result = await orchestrator.run(MagicMock())

        # Both steps should have been executed
        assert len(result["results"][0]["steps"]) == 2
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_handles_stop_stock_policy(self):
        """run should stop processing current stock with STOP_STOCK policy."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        # Step that fails with STOP_STOCK policy
        failing_step = create_mock_step(
            name="failing_step",
            outcome=StepOutcome(
                result=StepResult.FAILURE,
                message="Critical failure",
            ),
            failure_policy=FailurePolicy.STOP_STOCK,
        )

        # This step should NOT be executed
        next_step = create_mock_step(name="next_step")

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[failing_step, next_step],
        )

        result = await orchestrator.run(MagicMock())

        # Only one step should have been executed
        assert len(result["results"][0]["steps"]) == 1
        assert result["results"][0]["steps"][0]["step"] == "failing_step"
        # Status should still be completed (not stopped) since STOP_STOCK only affects one stock
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_handles_stop_all_policy(self):
        """run should stop all processing with STOP_ALL policy."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                },
                {
                    "pdno": "035420",
                    "prdt_name": "NAVER",
                    "hldg_qty": "10",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "250000",
                    "prpr": "260000",
                },
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        # Step that fails with STOP_ALL policy
        failing_step = create_mock_step(
            name="failing_step",
            outcome=StepOutcome(
                result=StepResult.FAILURE,
                message="Critical system failure",
            ),
            failure_policy=FailurePolicy.STOP_ALL,
        )

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[failing_step],
        )

        result = await orchestrator.run(MagicMock())

        # Should have stopped status
        assert result["status"] == "stopped"
        assert "STOP_ALL" in result["message"]
        # Only first stock should have been processed
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_run_handles_step_exception(self):
        """run should handle exceptions during step execution."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        # Step that raises an exception
        failing_step = MagicMock()
        failing_step.name = "failing_step"
        failing_step.failure_policy = FailurePolicy.CONTINUE
        failing_step.should_skip = MagicMock(return_value=False)
        failing_step.execute = AsyncMock(side_effect=Exception("Unexpected error"))
        failing_step._log_skip = MagicMock()

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[failing_step],
        )

        result = await orchestrator.run(MagicMock())

        # Should have recorded the error
        assert result["status"] == "completed"
        assert "error" in result["results"][0]["steps"][0]["result"]

    @pytest.mark.asyncio
    async def test_run_handles_exception_with_stop_all_policy(self):
        """run should stop all on exception with STOP_ALL policy."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                },
                {
                    "pdno": "035420",
                    "prdt_name": "NAVER",
                    "hldg_qty": "10",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "250000",
                    "prpr": "260000",
                },
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        # Step that raises exception with STOP_ALL policy
        failing_step = MagicMock()
        failing_step.name = "failing_step"
        failing_step.failure_policy = FailurePolicy.STOP_ALL
        failing_step.should_skip = MagicMock(return_value=False)
        failing_step.execute = AsyncMock(side_effect=Exception("Critical error"))
        failing_step._log_skip = MagicMock()

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[failing_step],
        )

        result = await orchestrator.run(MagicMock())

        assert result["status"] == "stopped"
        assert len(result["results"]) == 1  # Only first stock processed

    @pytest.mark.asyncio
    async def test_run_skips_step_when_should_skip_true(self):
        """run should skip step when should_skip returns True."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        # Step that should be skipped
        skip_step = create_mock_step(
            name="skip_step",
            should_skip=True,
        )

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[skip_step],
        )

        result = await orchestrator.run(MagicMock())

        # Step should have been skipped, not executed
        assert result["results"][0]["steps"][0]["result"]["skipped"] is True
        skip_step.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_stores_analysis_result_in_context(self):
        """run should store analysis result in context when step returns data."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        analysis_data = {
            "decision": "buy",
            "confidence": 75,
            "appropriate_buy_min": 70000,
            "appropriate_buy_max": 72000,
        }

        mock_step = create_mock_step(
            name="analyze",
            outcome=StepOutcome(
                result=StepResult.SUCCESS,
                message="Analysis complete",
                data=analysis_data,
            ),
        )

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[mock_step],
        )

        result = await orchestrator.run(MagicMock())

        assert result["status"] == "completed"
        assert result["results"][0]["steps"][0]["result"]["data"] == analysis_data


# ==================== Integration Tests ====================


class TestOrchestratorIntegration:
    """Integration tests for orchestrator with real strategy instances."""

    @pytest.mark.asyncio
    async def test_domestic_strategy_full_flow(self):
        """Test full flow with DomesticStrategy."""
        # Create real DomesticStrategy
        strategy = DomesticStrategy()

        # Mock KIS client
        mock_kis = MagicMock()
        mock_kis.fetch_my_stocks = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                }
            ]
        )
        mock_kis.inquire_korea_orders = AsyncMock(return_value=[])

        # Create mock steps
        steps = [
            create_mock_step(
                name="analyze",
                outcome=StepOutcome(
                    result=StepResult.SUCCESS,
                    message="Analysis complete",
                    data={"decision": "hold"},
                ),
            ),
            create_mock_step(
                name="buy",
                outcome=StepOutcome(
                    result=StepResult.SKIP,
                    message="매수 조건 미충족",
                ),
            ),
            create_mock_step(
                name="sell",
                outcome=StepOutcome(
                    result=StepResult.SKIP,
                    message="매도 조건 미충족",
                ),
            ),
        ]

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session,
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService"
            ) as mock_service_cls,
        ):
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_service = MagicMock()
            mock_service.get_holdings_by_user = AsyncMock(return_value=[])
            mock_service_cls.return_value = mock_service

            orchestrator = TradingOrchestrator(
                strategy=strategy,
                steps=steps,
            )

            result = await orchestrator.run(mock_kis)

        assert result["status"] == "completed"
        assert len(result["results"]) == 1
        assert len(result["results"][0]["steps"]) == 3

    @pytest.mark.asyncio
    async def test_overseas_strategy_full_flow(self):
        """Test full flow with OverseasStrategy."""
        # Create real OverseasStrategy
        strategy = OverseasStrategy()

        # Mock KIS client
        mock_kis = MagicMock()
        mock_kis.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc",
                    "ovrs_cblc_qty": "10",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "150",
                    "now_pric2": "160",
                    "ovrs_excg_cd": "NASD",
                }
            ]
        )
        mock_kis.inquire_overseas_orders = AsyncMock(return_value=[])

        # Create mock steps
        steps = [
            create_mock_step(
                name="analyze",
                outcome=StepOutcome(
                    result=StepResult.SUCCESS,
                    message="Analysis complete",
                    data={"decision": "hold"},
                ),
            ),
        ]

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session,
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService"
            ) as mock_service_cls,
            patch(
                "app.core.symbol.to_db_symbol",
                side_effect=lambda x: x,
            ),
        ):
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_service = MagicMock()
            mock_service.get_holdings_by_user = AsyncMock(return_value=[])
            mock_service_cls.return_value = mock_service

            orchestrator = TradingOrchestrator(
                strategy=strategy,
                steps=steps,
            )

            result = await orchestrator.run(mock_kis)

        assert result["status"] == "completed"
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_multiple_stocks_with_mixed_results(self):
        """Test multiple stocks with different step outcomes."""
        mock_strategy = MagicMock(spec=MarketStrategy)
        mock_strategy.market_name = "국내주식"
        mock_strategy.fetch_holdings = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "hldg_qty": "5",
                    "ord_psbl_qty": "5",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                },
                {
                    "pdno": "035420",
                    "prdt_name": "NAVER",
                    "hldg_qty": "10",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "250000",
                    "prpr": "260000",
                },
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "20",
                    "ord_psbl_qty": "20",
                    "pchs_avg_pric": "70000",
                    "prpr": "71000",
                },
            ]
        )
        mock_strategy.fetch_open_orders = AsyncMock(return_value=[])

        # Step that succeeds for all stocks
        success_step = create_mock_step(
            name="analyze",
            outcome=StepOutcome(
                result=StepResult.SUCCESS,
                message="Success",
            ),
        )

        orchestrator = TradingOrchestrator(
            strategy=mock_strategy,
            steps=[success_step],
        )

        result = await orchestrator.run(MagicMock())

        assert result["status"] == "completed"
        assert len(result["results"]) == 3

        # Check each stock has results
        symbols = [r["symbol"] for r in result["results"]]
        assert "005935" in symbols
        assert "035420" in symbols
        assert "005930" in symbols
