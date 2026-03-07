"""Unit tests for KIS trading step functions."""

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.kis_trading_types import (
    FailurePolicy,
    StepOutcome,
    StepResult,
    TradingContext,
)
from app.jobs.kis_trading_steps import (
    AnalyzeStep,
    BuyStep,
    CancelBuyOrdersStep,
    CancelSellOrdersStep,
    RefreshStep,
    SellStep,
    TradingStep,
)


# Helper to create a mock TradingContext
def create_mock_context(
    symbol: str = "005935",
    name: str = "삼성전자우",
    avg_price: float = 73800.0,
    current_price: float = 75850.0,
    quantity: int = 5,
    is_manual: bool = False,
    exchange_code: str = "",
    open_orders: list[dict] | None = None,
    analysis_result: dict | None = None,
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

    if analysis_result:
        context.analysis_result = analysis_result

    return context


# ==================== TradingStep Base Class Tests ====================


class TestTradingStepBase:
    """Tests for the TradingStep abstract base class."""

    def test_failure_policy_default_is_continue(self):
        """Default failure policy should be CONTINUE."""

        class ConcreteStep(TradingStep):
            @property
            def name(self) -> str:
                return "test"

            async def execute(self, context: TradingContext) -> StepOutcome:
                return self._success("test")

        step = ConcreteStep()
        assert step.failure_policy == FailurePolicy.CONTINUE

    def test_skip_conditions_default_empty(self):
        """Default skip conditions should be empty list."""

        class ConcreteStep(TradingStep):
            @property
            def name(self) -> str:
                return "test"

            async def execute(self, context: TradingContext) -> StepOutcome:
                return self._success("test")

        step = ConcreteStep()
        assert step.skip_conditions == []

    def test_should_skip_returns_false_when_no_conditions(self):
        """should_skip should return False when no skip conditions."""

        class ConcreteStep(TradingStep):
            @property
            def name(self) -> str:
                return "test"

            async def execute(self, context: TradingContext) -> StepOutcome:
                return self._success("test")

        step = ConcreteStep()
        context = create_mock_context()
        assert step.should_skip(context) is False

    def test_should_skip_returns_true_when_condition_met(self):
        """should_skip should return True when any condition is met."""

        class ConcreteStep(TradingStep):
            @property
            def name(self) -> str:
                return "test"

            @property
            def skip_conditions(self):
                return [lambda ctx: ctx.is_manual]

            async def execute(self, context: TradingContext) -> StepOutcome:
                return self._success("test")

        step = ConcreteStep()
        context = create_mock_context(is_manual=True)
        assert step.should_skip(context) is True

    def test_success_helper_creates_success_outcome(self):
        """_success helper should create correct StepOutcome."""

        class ConcreteStep(TradingStep):
            @property
            def name(self) -> str:
                return "test"

            async def execute(self, context: TradingContext) -> StepOutcome:
                return self._success("test")

        step = ConcreteStep()
        outcome = step._success("test message", data={"key": "value"})

        assert outcome.result == StepResult.SUCCESS
        assert outcome.message == "test message"
        assert outcome.data == {"key": "value"}
        assert outcome.should_continue is True

    def test_failure_helper_creates_failure_outcome(self):
        """_failure helper should create correct StepOutcome."""

        class ConcreteStep(TradingStep):
            @property
            def name(self) -> str:
                return "test"

            async def execute(self, context: TradingContext) -> StepOutcome:
                return self._success("test")

        step = ConcreteStep()
        outcome = step._failure("error message", should_continue=False)

        assert outcome.result == StepResult.FAILURE
        assert outcome.message == "error message"
        assert outcome.should_continue is False

    def test_skip_helper_creates_skip_outcome(self):
        """_skip helper should create correct StepOutcome."""

        class ConcreteStep(TradingStep):
            @property
            def name(self) -> str:
                return "test"

            async def execute(self, context: TradingContext) -> StepOutcome:
                return self._success("test")

        step = ConcreteStep()
        outcome = step._skip("skip reason")

        assert outcome.result == StepResult.SKIP
        assert outcome.message == "skip reason"
        assert outcome.should_continue is True


# ==================== AnalyzeStep Tests ====================


class TestAnalyzeStep:
    """Tests for AnalyzeStep."""

    def test_name_is_analyze(self):
        """Step name should be 'analyze'."""
        step = AnalyzeStep()
        assert step.name == "analyze"

    def test_failure_policy_is_stop_stock(self):
        """Analysis failure should stop stock processing."""
        step = AnalyzeStep()
        assert step.failure_policy == FailurePolicy.STOP_STOCK

    @pytest.mark.asyncio
    async def test_execute_skips_manual_holdings(self):
        """Manual holdings should be skipped."""
        step = AnalyzeStep()
        context = create_mock_context(is_manual=True)

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "수동 잔고" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_skips_missing_name(self):
        """Stocks without name should be skipped."""
        step = AnalyzeStep()
        context = create_mock_context(name="")

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "종목명" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_success_with_structured_result(self):
        """Successful analysis should return structured result."""
        # Mock analyzer result
        mock_result = MagicMock()
        mock_result.decision = "buy"
        mock_result.confidence = 75
        mock_result.reasons = ["이유1", "이유2"]
        mock_result.appropriate_buy_min = 70000
        mock_result.appropriate_buy_max = 72000
        mock_result.appropriate_sell_min = 80000
        mock_result.appropriate_sell_max = 82000
        mock_result.buy_hope_min = 68000
        mock_result.buy_hope_max = 70000
        mock_result.sell_target_min = 85000
        mock_result.sell_target_max = 87000

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_stock_json = AsyncMock(
            return_value=(mock_result, "gemini-2.5-pro")
        )

        step = AnalyzeStep(analyzer=mock_analyzer)
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        assert outcome.data is not None
        assert outcome.data["decision"] == "buy"
        assert outcome.data["confidence"] == 75
        assert context.analysis_result is not None

    @pytest.mark.asyncio
    async def test_execute_failure_on_analyzer_exception(self):
        """Analyzer exception should return failure with STOP_STOCK."""
        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_stock_json = AsyncMock(
            side_effect=Exception("API 오류")
        )

        step = AnalyzeStep(analyzer=mock_analyzer)
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.FAILURE
        assert outcome.should_continue is False  # STOP_STOCK
        assert "분석 실패" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_failure_on_none_result(self):
        """None analysis result should return failure."""
        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_stock_json = AsyncMock(return_value=(None, None))

        step = AnalyzeStep(analyzer=mock_analyzer)
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.FAILURE
        assert outcome.should_continue is False


# ==================== CancelBuyOrdersStep Tests ====================


class TestCancelBuyOrdersStep:
    """Tests for CancelBuyOrdersStep."""

    def test_name_is_cancel_buy_orders(self):
        """Step name should be 'cancel_buy_orders'."""
        step = CancelBuyOrdersStep()
        assert step.name == "cancel_buy_orders"

    def test_failure_policy_is_continue(self):
        """Cancellation failure should continue processing."""
        step = CancelBuyOrdersStep()
        assert step.failure_policy == FailurePolicy.CONTINUE

    @pytest.mark.asyncio
    async def test_execute_skips_when_no_open_orders(self):
        """Should skip when there are no open orders."""
        step = CancelBuyOrdersStep()
        context = create_mock_context(open_orders=[])

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "미체결 주문 없음" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_skips_when_no_buy_orders(self):
        """Should skip when there are no buy orders for this stock."""
        step = CancelBuyOrdersStep()
        # Only sell orders (sll_buy_dvsn_cd: 01=sell, 02=buy)
        open_orders = [
            {"pdno": "005935", "sll_buy_dvsn_cd": "01", "odno": "123"}
        ]
        context = create_mock_context(open_orders=open_orders)

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "매수 주문 없음" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_cancels_domestic_buy_orders(self):
        """Should cancel domestic buy orders successfully."""
        step = CancelBuyOrdersStep()
        open_orders = [
            {
                "pdno": "005935",
                "sll_buy_dvsn_cd": "02",  # buy
                "odno": "12345",
                "ft_ord_qty": "1",
                "ord_unpr": "75000",
                "ord_gno_brno": "123",
            }
        ]
        context = create_mock_context(open_orders=open_orders)
        context.kis.cancel_korea_order = AsyncMock(return_value={"odno": "12345"})

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        assert outcome.data["cancelled"] == 1
        assert outcome.data["failed"] == 0
        context.kis.cancel_korea_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_handles_partial_failure(self):
        """Should handle partial cancellation failures."""
        step = CancelBuyOrdersStep()
        open_orders = [
            {
                "pdno": "005935",
                "sll_buy_dvsn_cd": "02",
                "odno": "12345",
                "ft_ord_qty": "1",
                "ord_unpr": "75000",
            },
            {
                "pdno": "005935",
                "sll_buy_dvsn_cd": "02",
                "odno": "12346",
                "ft_ord_qty": "1",
                "ord_unpr": "74000",
            },
        ]
        context = create_mock_context(open_orders=open_orders)

        call_count = 0

        async def mock_cancel(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"odno": "12345"}
            raise Exception("취소 실패")

        context.kis.cancel_korea_order = mock_cancel

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        assert outcome.data["cancelled"] == 1
        assert outcome.data["failed"] == 1


# ==================== CancelSellOrdersStep Tests ====================


class TestCancelSellOrdersStep:
    """Tests for CancelSellOrdersStep."""

    def test_name_is_cancel_sell_orders(self):
        """Step name should be 'cancel_sell_orders'."""
        step = CancelSellOrdersStep()
        assert step.name == "cancel_sell_orders"

    def test_failure_policy_is_continue(self):
        """Cancellation failure should continue processing."""
        step = CancelSellOrdersStep()
        assert step.failure_policy == FailurePolicy.CONTINUE

    @pytest.mark.asyncio
    async def test_execute_skips_when_no_sell_orders(self):
        """Should skip when there are no sell orders for this stock."""
        step = CancelSellOrdersStep()
        # Only buy orders (sll_buy_dvsn_cd: 02=buy)
        open_orders = [
            {"pdno": "005935", "sll_buy_dvsn_cd": "02", "odno": "123"}
        ]
        context = create_mock_context(open_orders=open_orders)

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "매도 주문 없음" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_cancels_domestic_sell_orders(self):
        """Should cancel domestic sell orders successfully."""
        step = CancelSellOrdersStep()
        open_orders = [
            {
                "pdno": "005935",
                "sll_buy_dvsn_cd": "01",  # sell
                "odno": "12345",
                "ft_ord_qty": "1",
                "ord_unpr": "80000",
                "ord_gno_brno": "123",
            }
        ]
        context = create_mock_context(open_orders=open_orders)
        context.kis.cancel_korea_order = AsyncMock(return_value={"odno": "12345"})

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        assert outcome.data["cancelled"] == 1
        assert outcome.data["failed"] == 0


# ==================== BuyStep Tests ====================


class TestBuyStep:
    """Tests for BuyStep."""

    def test_name_is_buy(self):
        """Step name should be 'buy'."""
        step = BuyStep()
        assert step.name == "buy"

    def test_failure_policy_is_continue(self):
        """Buy failure should continue processing."""
        step = BuyStep()
        assert step.failure_policy == FailurePolicy.CONTINUE

    @pytest.mark.asyncio
    async def test_execute_skips_manual_holdings(self):
        """Manual holdings should be skipped."""
        step = BuyStep()
        context = create_mock_context(is_manual=True)

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "수동 잔고" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_domestic_buy_success(self):
        """Should execute domestic buy orders successfully."""
        mock_buy_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 1,
                "total_amount": 75000.0,
                "prices": [75000],
            }
        )

        mock_notifier = MagicMock()
        mock_notifier.notify_buy_order = AsyncMock()

        step = BuyStep(
            domestic_buy_func=mock_buy_func,
            notifier_factory=lambda: mock_notifier,
        )
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        assert "1건" in outcome.message
        mock_buy_func.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_domestic_buy_skip_when_no_orders(self):
        """Should skip when buy conditions not met."""
        mock_buy_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 0,
                "message": "매수 조건 미충족",
            }
        )

        step = BuyStep(domestic_buy_func=mock_buy_func)
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "매수 조건 미충족" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_domestic_buy_failure(self):
        """Should handle buy failure gracefully."""
        mock_buy_func = AsyncMock(
            return_value={
                "success": False,
                "message": "잔고 부족",
            }
        )

        step = BuyStep(domestic_buy_func=mock_buy_func)
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.FAILURE
        assert "잔고 부족" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_overseas_buy_success(self):
        """Should execute overseas buy orders successfully."""
        mock_buy_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 1,
                "total_amount": 100.0,
                "prices": [100],
            }
        )

        mock_notifier = MagicMock()
        mock_notifier.notify_buy_order = AsyncMock()

        step = BuyStep(
            overseas_buy_func=mock_buy_func,
            notifier_factory=lambda: mock_notifier,
        )
        context = create_mock_context(exchange_code="NASD")

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        mock_buy_func.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_sends_notification_on_success(self):
        """Should send notification on successful buy."""
        mock_buy_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 2,
                "total_amount": 150000.0,
                "prices": [74000, 76000],
            }
        )

        mock_notifier = MagicMock()
        mock_notifier.notify_buy_order = AsyncMock()

        step = BuyStep(
            domestic_buy_func=mock_buy_func,
            notifier_factory=lambda: mock_notifier,
        )
        context = create_mock_context()

        await step.execute(context)

        mock_notifier.notify_buy_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_handles_exception(self):
        """Should handle exceptions gracefully."""
        mock_buy_func = AsyncMock(side_effect=Exception("API 오류"))

        mock_notifier = MagicMock()
        mock_notifier.notify_trade_failure = AsyncMock()

        step = BuyStep(
            domestic_buy_func=mock_buy_func,
            notifier_factory=lambda: mock_notifier,
        )
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.FAILURE
        assert "API 오류" in outcome.message


# ==================== RefreshStep Tests ====================


class TestRefreshStep:
    """Tests for RefreshStep."""

    def test_name_is_refresh(self):
        """Step name should be 'refresh'."""
        step = RefreshStep()
        assert step.name == "refresh"

    def test_failure_policy_is_continue(self):
        """Refresh failure should continue processing."""
        step = RefreshStep()
        assert step.failure_policy == FailurePolicy.CONTINUE

    @pytest.mark.asyncio
    async def test_execute_skips_manual_holdings(self):
        """Manual holdings should be skipped."""
        step = RefreshStep()
        context = create_mock_context(is_manual=True)

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "수동 잔고" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_refreshes_domestic_holdings(self):
        """Should refresh domestic holdings from KIS API."""
        step = RefreshStep()
        context = create_mock_context(quantity=5, avg_price=73800.0)

        # Mock refreshed data with updated values
        context.kis.fetch_my_stocks = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "ord_psbl_qty": "10",  # Updated quantity
                    "pchs_avg_pric": "74000",  # Updated avg price
                    "prpr": "76000",  # Updated current price
                }
            ]
        )

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        assert context.quantity == 10
        assert context.avg_price == 74000.0
        assert context.current_price == 76000.0

    @pytest.mark.asyncio
    async def test_execute_refreshes_overseas_holdings(self):
        """Should refresh overseas holdings from KIS API."""
        step = RefreshStep()
        # Use matching symbol for overseas stock
        context = create_mock_context(
            symbol="AAPL",
            name="Apple Inc",
            exchange_code="NASD",
            quantity=5,
            avg_price=100.0,
            current_price=105.0,
        )

        context.kis.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "AAPL",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "102",
                    "now_pric2": "110",
                }
            ]
        )

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        assert context.quantity == 10
        assert context.avg_price == 102.0
        assert context.current_price == 110.0

    @pytest.mark.asyncio
    async def test_execute_skips_when_no_refreshed_data(self):
        """Should skip when stock not found in refreshed data."""
        step = RefreshStep()
        context = create_mock_context()

        context.kis.fetch_my_stocks = AsyncMock(return_value=[])

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "갱신된 잔고 정보 없음" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_keeps_existing_values_on_failure(self):
        """Should keep existing values on refresh failure."""
        step = RefreshStep()
        context = create_mock_context(quantity=5, avg_price=73800.0, current_price=75850.0)

        context.kis.fetch_my_stocks = AsyncMock(side_effect=Exception("API 오류"))

        outcome = await step.execute(context)

        # Returns success (not failure) with existing values
        assert outcome.result == StepResult.SUCCESS
        assert "기존 값 사용" in outcome.message
        # Values should remain unchanged
        assert context.quantity == 5
        assert context.avg_price == 73800.0
        assert context.current_price == 75850.0


# ==================== SellStep Tests ====================


class TestSellStep:
    """Tests for SellStep."""

    def test_name_is_sell(self):
        """Step name should be 'sell'."""
        step = SellStep()
        assert step.name == "sell"

    def test_failure_policy_is_continue(self):
        """Sell failure should continue processing."""
        step = SellStep()
        assert step.failure_policy == FailurePolicy.CONTINUE

    def test_skip_conditions_includes_manual_holdings(self):
        """Skip conditions should include manual holdings check."""
        step = SellStep()
        assert len(step.skip_conditions) == 1

        # Test the skip condition
        manual_context = create_mock_context(is_manual=True)
        normal_context = create_mock_context(is_manual=False)

        assert step.skip_conditions[0](manual_context) is True
        assert step.skip_conditions[0](normal_context) is False

    @pytest.mark.asyncio
    async def test_execute_handles_manual_holdings(self):
        """Manual holdings should trigger recommendation notification."""
        step = SellStep()
        context = create_mock_context(is_manual=True)

        with patch(
            "app.jobs.kis_trading_steps.SellStep._send_toss_recommendation",
            new_callable=AsyncMock,
        ):
            outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert outcome.data is not None
        assert outcome.data.get("is_manual") is True

    @pytest.mark.asyncio
    async def test_execute_domestic_sell_success(self):
        """Should execute domestic sell orders successfully."""
        mock_sell_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 1,
                "total_volume": 5,
                "prices": [80000],
                "quantities": [5],
                "expected_amount": 400000.0,
            }
        )

        mock_notifier = MagicMock()
        mock_notifier.notify_sell_order = AsyncMock()

        step = SellStep(
            domestic_sell_func=mock_sell_func,
            notifier_factory=lambda: mock_notifier,
        )
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        assert "1건" in outcome.message
        mock_sell_func.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_domestic_sell_skip_when_no_orders(self):
        """Should skip when sell conditions not met."""
        mock_sell_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 0,
                "message": "매도 조건 미충족",
            }
        )

        step = SellStep(domestic_sell_func=mock_sell_func)
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SKIP
        assert "매도 조건 미충족" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_domestic_sell_failure(self):
        """Should handle sell failure gracefully."""
        mock_sell_func = AsyncMock(
            return_value={
                "success": False,
                "message": "매도 실패",
            }
        )

        step = SellStep(domestic_sell_func=mock_sell_func)
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.FAILURE
        assert "매도 실패" in outcome.message

    @pytest.mark.asyncio
    async def test_execute_overseas_sell_success(self):
        """Should execute overseas sell orders successfully."""
        mock_sell_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 1,
                "total_volume": 10,
                "prices": [105],
                "quantities": [10],
                "expected_amount": 1050.0,
            }
        )

        mock_notifier = MagicMock()
        mock_notifier.notify_sell_order = AsyncMock()

        step = SellStep(
            overseas_sell_func=mock_sell_func,
            notifier_factory=lambda: mock_notifier,
        )
        context = create_mock_context(exchange_code="NASD")

        outcome = await step.execute(context)

        assert outcome.result == StepResult.SUCCESS
        mock_sell_func.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_sends_notification_on_success(self):
        """Should send notification on successful sell."""
        mock_sell_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 1,
                "total_volume": 5,
                "prices": [80000],
                "quantities": [5],
                "expected_amount": 400000.0,
            }
        )

        mock_notifier = MagicMock()
        mock_notifier.notify_sell_order = AsyncMock()

        step = SellStep(
            domestic_sell_func=mock_sell_func,
            notifier_factory=lambda: mock_notifier,
        )
        context = create_mock_context()

        await step.execute(context)

        mock_notifier.notify_sell_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_handles_exception(self):
        """Should handle exceptions gracefully."""
        mock_sell_func = AsyncMock(side_effect=Exception("API 오류"))

        mock_notifier = MagicMock()
        mock_notifier.notify_trade_failure = AsyncMock()

        step = SellStep(
            domestic_sell_func=mock_sell_func,
            notifier_factory=lambda: mock_notifier,
        )
        context = create_mock_context()

        outcome = await step.execute(context)

        assert outcome.result == StepResult.FAILURE
        assert "API 오류" in outcome.message


# ==================== Integration Tests ====================


class TestStepIntegration:
    """Integration tests for step interactions."""

    @pytest.mark.asyncio
    async def test_analyze_to_buy_flow(self):
        """Test flow from analyze to buy step."""
        # Setup analyze step with mock
        mock_result = MagicMock()
        mock_result.decision = "buy"
        mock_result.confidence = 80
        mock_result.reasons = ["상승 추세"]
        mock_result.appropriate_buy_min = 70000
        mock_result.appropriate_buy_max = 72000
        mock_result.appropriate_sell_min = 80000
        mock_result.appropriate_sell_max = 82000

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_stock_json = AsyncMock(
            return_value=(mock_result, "gemini-2.5-pro")
        )

        analyze_step = AnalyzeStep(analyzer=mock_analyzer)

        # Setup buy step with mock
        mock_buy_func = AsyncMock(
            return_value={
                "success": True,
                "orders_placed": 1,
                "total_amount": 71000.0,
                "prices": [71000],
            }
        )

        mock_notifier = MagicMock()
        mock_notifier.notify_buy_order = AsyncMock()

        buy_step = BuyStep(
            domestic_buy_func=mock_buy_func,
            notifier_factory=lambda: mock_notifier,
        )

        context = create_mock_context()

        # Execute analyze
        analyze_outcome = await analyze_step.execute(context)
        assert analyze_outcome.result == StepResult.SUCCESS
        assert context.analysis_result is not None

        # Execute buy
        buy_outcome = await buy_step.execute(context)
        assert buy_outcome.result == StepResult.SUCCESS

    @pytest.mark.asyncio
    async def test_full_domestic_flow(self):
        """Test full flow for domestic stock."""
        context = create_mock_context()

        # Mock all KIS methods
        context.kis.cancel_korea_order = AsyncMock(return_value={"odno": "12345"})
        context.kis.fetch_my_stocks = AsyncMock(
            return_value=[
                {
                    "pdno": "005935",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "74000",
                    "prpr": "76000",
                }
            ]
        )

        # Analyze step
        mock_result = MagicMock()
        mock_result.decision = "hold"
        mock_result.confidence = 65
        mock_result.reasons = ["관망"]
        mock_result.appropriate_buy_min = 70000
        mock_result.appropriate_buy_max = 72000
        mock_result.appropriate_sell_min = 80000
        mock_result.appropriate_sell_max = 82000

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze_stock_json = AsyncMock(
            return_value=(mock_result, "gemini-2.5-pro")
        )

        analyze_step = AnalyzeStep(analyzer=mock_analyzer)
        outcome = await analyze_step.execute(context)
        assert outcome.result == StepResult.SUCCESS

        # Cancel buy orders step
        cancel_buy_step = CancelBuyOrdersStep()
        outcome = await cancel_buy_step.execute(context)
        assert outcome.result == StepResult.SKIP  # No open orders

        # Buy step
        mock_buy_func = AsyncMock(
            return_value={"success": True, "orders_placed": 0, "message": "조건 미충족"}
        )
        mock_notifier = MagicMock()
        mock_notifier.notify_buy_order = AsyncMock()
        buy_step = BuyStep(
            domestic_buy_func=mock_buy_func,
            notifier_factory=lambda: mock_notifier,
        )
        outcome = await buy_step.execute(context)
        assert outcome.result == StepResult.SKIP

        # Refresh step
        refresh_step = RefreshStep()
        outcome = await refresh_step.execute(context)
        assert outcome.result == StepResult.SUCCESS

        # Cancel sell orders step
        cancel_sell_step = CancelSellOrdersStep()
        outcome = await cancel_sell_step.execute(context)
        assert outcome.result == StepResult.SKIP  # No open orders

        # Sell step
        mock_sell_func = AsyncMock(
            return_value={"success": True, "orders_placed": 0, "message": "조건 미충족"}
        )
        sell_step = SellStep(
            domestic_sell_func=mock_sell_func,
            notifier_factory=lambda: mock_notifier,
        )
        outcome = await sell_step.execute(context)
        assert outcome.result == StepResult.SKIP
