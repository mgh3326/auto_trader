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
