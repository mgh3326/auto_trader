"""ROB-469 PR2: per-tool execution timeout middleware.

A single slow or awaiting tool can stall the FastMCP streamable-http event loop and
take ALL tools down at once (the ROB-469 SPOF). This middleware bounds each
``tools/call`` with ``asyncio.wait_for`` and converts a timeout into a clean
``ToolError`` so one slow tool fails by itself instead of wedging the whole server.

LIMITATION: ``asyncio.wait_for`` can only cancel a coroutine blocked on ``await``. A
tool that blocks the loop SYNCHRONOUSLY (heavy pandas with no await, a blocking C
call) cannot be cancelled this way — that case is covered by ROB-469 PR3's watchdog.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_S = 45.0

# Heavy tools that legitimately run long get an elevated budget so the default does
# not kill them (ROB-469: "global default + exempt heavy tools"). Generous on
# purpose. Names verified against the registered tool surface. A budget of 0 means
# "exempt" (no timeout).
ELEVATED_TOOL_TIMEOUTS_S: dict[str, float] = {
    # Report generation (snapshot collectors + Hermes composition) — heaviest.
    "investment_report_generate_from_bundle": 240.0,
    "investment_report_prepare_bundle": 240.0,
    "investment_report_create_from_hermes_composition": 240.0,
    "investment_report_prepare_intraday_context": 180.0,
    "investment_report_get_hermes_context": 180.0,
    # Batch analysis / screeners (multi-symbol fan-out).
    "analyze_stock_batch": 120.0,
    "analyze_portfolio": 120.0,
    "screen_stocks": 120.0,
    "screen_stocks_snapshot": 120.0,
    # Single heavy fan-out.
    "analyze_stock": 90.0,
    "get_holdings": 120.0,
    # Multi-API fundamentals.
    "get_financials": 90.0,
    "get_company_profile": 90.0,
    "get_valuation": 90.0,
    "get_sector_peers": 90.0,
    # Crypto multi-source (network).
    "get_crypto_catalysts": 75.0,
    "get_crypto_social": 75.0,
    "get_crypto_order_flow": 75.0,
    # OHLCV + indicator compute; news fetch.
    "get_indicators": 75.0,
    "get_news": 75.0,
    # Order reconcile fan-out over daily order history.
    "kis_live_reconcile_orders": 90.0,
    "live_reconcile_orders": 90.0,
}


class ToolTimeoutMiddleware(Middleware):
    """Bound each ``tools/call`` with a per-tool time budget.

    Registered LAST in main.py so it is the innermost middleware (wraps the tool)
    while the Sentry middleware stays outermost and captures the raised ``ToolError``
    with the tool-call context (fastmcp 3.2.0 reverses the middleware list, so
    first-added = outermost).
    """

    def __init__(
        self,
        *,
        default_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
        overrides: dict[str, float] | None = None,
        enabled: bool = True,
    ) -> None:
        self._default = default_timeout_s
        self._overrides = dict(
            ELEVATED_TOOL_TIMEOUTS_S if overrides is None else overrides
        )
        self._enabled = enabled

    def _budget_for(self, tool_name: str) -> float:
        return self._overrides.get(tool_name, self._default)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if not self._enabled:
            return await call_next(context)
        tool_name = context.message.name
        budget = self._budget_for(tool_name)
        if budget <= 0:  # explicit exemption
            return await call_next(context)
        try:
            return await asyncio.wait_for(call_next(context), timeout=budget)
        except TimeoutError:
            logger.warning("mcp.tool.timeout tool=%s budget_s=%.0f", tool_name, budget)
            raise ToolError(
                f"Tool '{tool_name}' exceeded its {budget:.0f}s time budget "
                "and was cancelled."
            ) from None
