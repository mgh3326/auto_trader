"""
Trading step base class and concrete step implementations for KIS automation.

This module defines the TradingStep abstract base class that all trading
automation steps must implement. Each step represents a discrete action
in the trading automation workflow (analyze, buy, sell, etc.).

Steps are executed sequentially by the TradingOrchestrator, with explicit
failure policies and skip conditions.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.jobs.kis_trading_types import (
    FailurePolicy,
    SkipCondition,
    StepOutcome,
    StepResult,
    TradingContext,
)

logger = logging.getLogger(__name__)


class TradingStep(ABC):
    """
    Abstract base class for trading automation steps.

    Each step represents a discrete action in the trading automation workflow.
    Steps are executed sequentially by the TradingOrchestrator.

    Subclasses must implement:
    - name: Property returning the step name
    - execute: Async method performing the step action

    Subclasses can optionally override:
    - failure_policy: What to do when this step fails (default: CONTINUE)
    - skip_conditions: List of conditions under which to skip this step
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Return the name of this step.

        Used for logging and result aggregation.
        Example: "analyze", "buy", "sell"
        """
        ...

    @property
    def failure_policy(self) -> FailurePolicy:
        """
        What to do when this step fails.

        Default is CONTINUE (log error, continue to next step).
        Override this property to change the behavior.

        Returns:
            FailurePolicy enum value
        """
        return FailurePolicy.CONTINUE

    @property
    def skip_conditions(self) -> list[SkipCondition]:
        """
        Conditions under which this step should be skipped.

        Each condition is a callable that takes TradingContext and returns
        True if the step should be skipped.

        Returns:
            List of skip condition callables
        """
        return []

    @abstractmethod
    async def execute(self, context: TradingContext) -> StepOutcome:
        """
        Execute this step for the given stock context.

        This method must be implemented by subclasses to perform the
        actual step action.

        Args:
            context: TradingContext containing stock info, orders, and services

        Returns:
            StepOutcome indicating success/failure/skip and any data
        """
        ...

    def should_skip(self, context: TradingContext) -> bool:
        """
        Check if this step should be skipped for the given context.

        Args:
            context: TradingContext for the current stock

        Returns:
            True if any skip condition is met, False otherwise
        """
        return any(condition(context) for condition in self.skip_conditions)

    def _log_start(self, context: TradingContext) -> None:
        """Log the start of step execution."""
        logger.info(
            "[Step:%s] Starting for %s (%s)",
            self.name,
            context.name,
            context.symbol,
        )

    def _log_success(self, context: TradingContext, message: str = "완료") -> None:
        """Log successful step completion."""
        logger.info(
            "[Step:%s] Success for %s (%s): %s",
            self.name,
            context.name,
            context.symbol,
            message,
        )

    def _log_failure(
        self, context: TradingContext, error: Exception, message: str = ""
    ) -> None:
        """Log step failure."""
        logger.error(
            "[Step:%s] Failed for %s (%s): %s%s",
            self.name,
            context.name,
            context.symbol,
            f"{message}: " if message else "",
            error,
            exc_info=error,
        )

    def _log_skip(self, context: TradingContext, reason: str) -> None:
        """Log step skip."""
        logger.info(
            "[Step:%s] Skipped for %s (%s): %s",
            self.name,
            context.name,
            context.symbol,
            reason,
        )

    @staticmethod
    def _success(message: str, data: dict[str, Any] | None = None) -> StepOutcome:
        """Helper to create a success outcome."""
        return StepOutcome(
            result=StepResult.SUCCESS,
            message=message,
            data=data,
            should_continue=True,
        )

    @staticmethod
    def _failure(
        message: str,
        data: dict[str, Any] | None = None,
        should_continue: bool = True,
    ) -> StepOutcome:
        """Helper to create a failure outcome."""
        return StepOutcome(
            result=StepResult.FAILURE,
            message=message,
            data=data,
            should_continue=should_continue,
        )

    @staticmethod
    def _skip(message: str, data: dict[str, Any] | None = None) -> StepOutcome:
        """Helper to create a skip outcome."""
        return StepOutcome(
            result=StepResult.SKIP,
            message=message,
            data=data,
            should_continue=True,
        )


class AnalyzeStep(TradingStep):
    """
    Step that performs AI analysis on a stock.

    Uses KISAnalyzer to analyze the stock and stores the result in context.
    If analysis fails, processing for this stock is stopped (STOP_STOCK policy).
    """

    @property
    def name(self) -> str:
        return "analyze"

    @property
    def failure_policy(self) -> FailurePolicy:
        """Analysis failure stops processing for this stock."""
        return FailurePolicy.STOP_STOCK

    async def execute(self, context: TradingContext) -> StepOutcome:
        """Execute AI analysis for the stock."""
        self._log_start(context)

        # Skip manual holdings (토스 등) - they don't need AI analysis
        if context.is_manual:
            self._log_skip(context, "수동 잔고는 분석 스킵")
            return self._skip("수동 잔고 종목")

        if not context.name:
            self._log_skip(context, "종목명 없음")
            return self._skip("종목명을 찾을 수 없음")

        try:
            from app.analysis.service_analyzers import KISAnalyzer

            analyzer = KISAnalyzer()
            result, _ = await analyzer.analyze_stock_json(context.name)

            if result is None:
                self._log_failure(context, Exception("분석 결과 없음"))
                return self._failure(
                    "분석 결과를 가져올 수 없습니다.",
                    should_continue=False,  # STOP_STOCK
                )

            # Store analysis result in context for later steps
            analysis_data: dict[str, Any] = {}

            if hasattr(result, "decision"):
                analysis_data["decision"] = result.decision
                analysis_data["confidence"] = (
                    float(result.confidence) if result.confidence else 0.0
                )
                analysis_data["reasons"] = (
                    list(result.reasons)
                    if hasattr(result, "reasons") and result.reasons
                    else []
                )

                # Price ranges
                for attr in (
                    "appropriate_buy_min",
                    "appropriate_buy_max",
                    "appropriate_sell_min",
                    "appropriate_sell_max",
                    "buy_hope_min",
                    "buy_hope_max",
                    "sell_target_min",
                    "sell_target_max",
                ):
                    if hasattr(result, attr):
                        analysis_data[attr] = getattr(result, attr)

                # Store in context
                context.analysis_result = analysis_data

                decision = result.decision
                confidence = analysis_data.get("confidence", 0)
                self._log_success(
                    context, f"결정: {decision}, 신뢰도: {confidence}%"
                )
                return self._success(
                    f"분석 완료: {decision} ({confidence}%)",
                    data=analysis_data,
                )
            else:
                # Fallback: text result (no structured decision)
                analysis_data["raw_result"] = str(result)
                context.analysis_result = analysis_data
                self._log_success(context, "분석 완료 (텍스트 응답)")
                return self._success("분석 완료 (텍스트 응답)", data=analysis_data)

        except Exception as e:
            self._log_failure(context, e)
            return self._failure(
                f"분석 실패: {e}",
                should_continue=False,  # STOP_STOCK
            )
