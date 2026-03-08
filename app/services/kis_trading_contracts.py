"""KIS Trading Service contracts and error handling.

This module defines the internal result types and error handling utilities
for the KIS trading service layer. It enables exception absorption while
maintaining backward-compatible dict payloads for callers.

Stage 1 of refactoring: Add exception handling without changing public contracts.
"""

from dataclasses import dataclass, field
from typing import Any

from app.core.async_rate_limiter import RateLimitExceededError


@dataclass
class OrderStepResult:
    """Internal result type for order execution steps.

    Provides structured result data with to_payload() for backward-compatible
    dict conversion. This allows callers to continue using res["success"],
    res.get("orders_placed", 0), etc.
    """

    # Required fields (always present)
    success: bool
    message: str
    orders_placed: int = 0

    # Conditional fields (buy success)
    prices: list[float] = field(default_factory=list)
    quantities: list[int] = field(default_factory=list)
    total_amount: float = 0.0

    # Conditional fields (sell success)
    total_volume: int = 0
    expected_amount: float = 0.0

    # Error information (when exception is absorbed)
    error: str | None = None
    error_type: str | None = None  # "db", "api", "validation", "rate_limit"

    def to_payload(self) -> dict[str, Any]:
        """Convert to backward-compatible dict payload.

        Returns a dict that matches the historical return shape from
        process_kis_*_orders_with_analysis functions. Callers access via:
        - res["success"], res["message"] (direct access)
        - res.get("orders_placed", 0) (safe access)
        - res.get("error") (error check, was always None before)
        """
        payload: dict[str, Any] = {
            "success": self.success,
            "message": self.message,
            "orders_placed": self.orders_placed,
        }

        # Buy success fields
        if self.prices:
            payload["prices"] = self.prices
        if self.quantities:
            payload["quantities"] = self.quantities
        if self.total_amount > 0:
            payload["total_amount"] = self.total_amount

        # Sell success fields
        if self.total_volume > 0:
            payload["total_volume"] = self.total_volume
        if self.expected_amount > 0:
            payload["expected_amount"] = self.expected_amount

        # Error information (for callers checking res.get("error"))
        if self.error:
            payload["error"] = self.error
        if self.error_type:
            payload["error_type"] = self.error_type

        return payload


def _map_exception_to_result(exc: Exception, context: str) -> OrderStepResult:
    """Map an exception to an OrderStepResult.

    This function provides consistent error handling across all KIS trading
    service functions. It categorizes exceptions and returns structured results
    that can be converted to backward-compatible payloads.

    Args:
        exc: The caught exception
        context: Description of the operation (e.g., "domestic buy", "overseas sell")

    Returns:
        OrderStepResult with error information populated
    """
    if isinstance(exc, RateLimitExceededError):
        return OrderStepResult(
            success=False,
            message=f"Rate limit exceeded during {context}",
            error=str(exc),
            error_type="rate_limit",
        )

    if isinstance(exc, ValueError):
        return OrderStepResult(
            success=False,
            message=f"Validation error during {context}: {exc}",
            error=str(exc),
            error_type="validation",
        )

    # DB errors: OperationalError, InterfaceError, etc.
    exc_name = type(exc).__name__
    if exc_name in ("OperationalError", "InterfaceError", "DBAPIError"):
        return OrderStepResult(
            success=False,
            message=f"Database error during {context}",
            error=str(exc),
            error_type="db",
        )

    # KIS API errors: RuntimeError from KISClient
    if isinstance(exc, RuntimeError):
        return OrderStepResult(
            success=False,
            message=f"API error during {context}: {exc}",
            error=str(exc),
            error_type="api",
        )

    # Network errors
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return OrderStepResult(
            success=False,
            message=f"Network error during {context}: {exc}",
            error=str(exc),
            error_type="network",
        )

    # Unknown errors - catch all
    return OrderStepResult(
        success=False,
        message=f"Unexpected error during {context}: {exc}",
        error=str(exc),
        error_type="unknown",
    )
