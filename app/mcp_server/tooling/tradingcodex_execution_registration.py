"""TradingCodex execution MCP profile registration.

This profile is narrower than the default MCP surface and more privileged than
account_read. It is only for the TradingCodex BrokerAdapter live gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from app.mcp_server.tooling.account_read_registration import ACCOUNT_READ_TOOL_NAMES
from app.mcp_server.tooling.analysis_artifact_registration import (
    ANALYSIS_ARTIFACT_TOOL_NAMES,
)
from app.mcp_server.tooling.analysis_readonly_registration import _AllowlistedMCP
from app.mcp_server.tooling.forecast_registration import FORECAST_TOOL_NAMES
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
    register_kis_live_order_tools,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import (
    ORDER_TOOL_NAMES,
    register_order_tools,
)
from app.mcp_server.tooling.orders_toss_variants import (
    TOSS_LIVE_ORDER_TOOL_NAMES,
    register_toss_live_order_tools,
)
from app.mcp_server.tooling.paper_limit_order_handler import (
    PAPER_LIMIT_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.portfolio_registration import register_portfolio_tools
from app.mcp_server.tooling.session_context_registration import (
    SESSION_CONTEXT_TOOL_NAMES,
)
from app.mcp_server.tooling.user_settings_registration import USER_SETTINGS_TOOL_NAMES

if TYPE_CHECKING:
    from fastmcp import FastMCP


TRADINGCODEX_EXECUTION_TOOL_NAMES: set[str] = ACCOUNT_READ_TOOL_NAMES | {
    "place_order",
    "cancel_order",
    "kis_live_place_order",
    "kis_live_cancel_order",
    "toss_preview_order",
    "toss_place_order",
    "toss_cancel_order",
    "sell_ladder_fill_preview",
    "buy_ladder_fill_preview",
}

TRADINGCODEX_EXECUTION_FORBIDDEN_TOOL_NAMES: set[str] = (
    (ORDER_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | (KIS_LIVE_ORDER_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | KIWOOM_MOCK_TOOL_NAMES
    | PAPER_LIMIT_ORDER_TOOL_NAMES
    | (TOSS_LIVE_ORDER_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | ANALYSIS_ARTIFACT_TOOL_NAMES
    | FORECAST_TOOL_NAMES
    | SESSION_CONTEXT_TOOL_NAMES
    | USER_SETTINGS_TOOL_NAMES
    | {
        "get_available_capital",
        "get_position",
        "update_manual_holdings",
        "list_active_watches",
    }
)


def register_tradingcodex_execution_tools(mcp: FastMCP) -> None:
    """Register only the TradingCodex broker execution allowlist."""
    filtered = cast("FastMCP", _AllowlistedMCP(mcp, TRADINGCODEX_EXECUTION_TOOL_NAMES))
    register_portfolio_tools(filtered)
    register_order_tools(filtered)
    register_kis_live_order_tools(filtered)
    register_toss_live_order_tools(filtered)


__all__ = [
    "TRADINGCODEX_EXECUTION_FORBIDDEN_TOOL_NAMES",
    "TRADINGCODEX_EXECUTION_TOOL_NAMES",
    "register_tradingcodex_execution_tools",
]
