"""
Data structures and types for KIS Trading Automation orchestration.

This module defines the core types used by the step orchestrator pattern:
- StepResult: Enum for step execution outcomes
- FailurePolicy: Enum for handling step failures
- TradingContext: Dataclass holding context for a single stock's automation
- StepOutcome: Dataclass representing the result of a step execution
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.brokers.kis.client import KISClient


class StepResult(Enum):
    """Result of a step execution."""

    SUCCESS = "success"
    FAILURE = "failure"
    SKIP = "skip"


class FailurePolicy(Enum):
    """What to do when a step fails."""

    CONTINUE = "continue"  # Log error, continue to next step
    STOP_STOCK = "stop_stock"  # Stop processing this stock, continue others
    STOP_ALL = "stop_all"  # Stop entire automation run


@dataclass
class TradingContext:
    """
    Context for trading automation of a single stock.

    Holds all the information needed by step functions to execute,
    including the stock data, open orders, KIS client reference,
    and market strategy.
    """

    # Stock information from holdings
    stock: dict[str, Any]

    # Open orders for this stock (from inquire orders API)
    open_orders: list[dict[str, Any]]

    # KIS client for API calls
    kis: "KISClient"

    # Market strategy (domestic or overseas)
    strategy: Any  # MarketStrategy type hint avoided to prevent circular import

    # Parsed stock details (populated from stock dict)
    symbol: str = ""
    name: str = ""
    avg_price: float = 0.0
    current_price: float = 0.0
    quantity: int = 0
    is_manual: bool = False

    # Exchange code (for overseas stocks)
    exchange_code: str = ""

    # Analysis result from the analyzer
    analysis_result: dict[str, Any] | None = None

    # Steps executed for this stock (for result aggregation)
    steps_result: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Parse stock dict into convenient attributes."""
        # Domestic stock format (KIS API)
        self.symbol = self.stock.get("pdno", "")
        self.name = self.stock.get("prdt_name", "")
        self.avg_price = float(self.stock.get("pchs_avg_pric", 0) or 0)
        self.current_price = float(self.stock.get("prpr", 0) or 0)
        # Use ord_psbl_qty (orderable quantity) for sell, fallback to hldg_qty
        qty_str = self.stock.get("ord_psbl_qty", self.stock.get("hldg_qty", "0"))
        self.quantity = int(float(qty_str or 0))
        self.is_manual = self.stock.get("_is_manual", False)

        # Overseas stock format (different key names)
        if not self.symbol:
            self.symbol = self.stock.get("ovrs_pdno", "")
        if not self.name:
            self.name = self.stock.get("ovrs_item_name", "")
        if not self.avg_price:
            self.avg_price = float(self.stock.get("pchs_avg_pric", 0) or 0)
        if not self.current_price:
            overseas_price = (
                self.stock.get("now_pric2")
                or self.stock.get("ovrs_now_pric1")
                or self.stock.get("last_price")
                or 0
            )
            self.current_price = float(overseas_price or 0)

        # Exchange code for overseas stocks
        self.exchange_code = str(self.stock.get("ovrs_excg_cd", "")).strip().upper()


@dataclass
class StepOutcome:
    """
    Result of a step function execution.

    Returned by each step function to indicate success/failure/skip
    and provide data for the next steps or for result aggregation.
    """

    # The result status
    result: StepResult

    # Human-readable message describing the outcome
    message: str

    # Optional data returned by the step (e.g., analysis results, order IDs)
    data: dict[str, Any] | None = None

    # Whether to continue processing this stock
    # False means stop processing this stock (respects failure_policy)
    should_continue: bool = True


# Type alias for skip condition check functions
SkipCondition = Callable[[TradingContext], bool]
