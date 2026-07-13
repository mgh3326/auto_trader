"""Account-read MCP profile registration for TradingCodex adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

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
from app.mcp_server.tooling.orders_kiwoom_us_variants import (
    KIWOOM_MOCK_US_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import (
    KIWOOM_MOCK_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import (
    register as register_kiwoom_mock_tools,
)
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


KIWOOM_MOCK_ACCOUNT_READ_TOOL_NAMES: set[str] = {
    "kiwoom_mock_get_positions",
    "kiwoom_mock_get_orderable_cash",
    "kiwoom_mock_get_order_history",
}

ACCOUNT_READ_TOOL_NAMES: set[str] = {
    "get_holdings",
    "toss_get_positions",
    "get_cash_balance",
    "toss_get_orderable_cash",
    "get_order_history",
    "kis_live_get_order_history",
    "toss_get_order_history",
} | KIWOOM_MOCK_ACCOUNT_READ_TOOL_NAMES

ACCOUNT_READ_FORBIDDEN_TOOL_NAMES: set[str] = (
    (ORDER_TOOL_NAMES - {"get_order_history"})
    | (KIS_LIVE_ORDER_TOOL_NAMES - {"kis_live_get_order_history"})
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | (KIWOOM_MOCK_TOOL_NAMES - KIWOOM_MOCK_ACCOUNT_READ_TOOL_NAMES)
    | KIWOOM_MOCK_US_TOOL_NAMES
    | PAPER_LIMIT_ORDER_TOOL_NAMES
    | (
        TOSS_LIVE_ORDER_TOOL_NAMES
        - {
            "toss_get_order_history",
            "toss_get_positions",
            "toss_get_orderable_cash",
        }
    )
    | ANALYSIS_ARTIFACT_TOOL_NAMES
    | FORECAST_TOOL_NAMES
    | SESSION_CONTEXT_TOOL_NAMES
    | USER_SETTINGS_TOOL_NAMES
    | {
        "get_position",
        "get_available_capital",
        "update_manual_holdings",
        "list_active_watches",
    }
)


def register_account_read_tools(mcp: FastMCP) -> None:
    """Register the physically restricted account-read surface."""
    filtered = cast("FastMCP", _AllowlistedMCP(mcp, ACCOUNT_READ_TOOL_NAMES))
    register_portfolio_tools(filtered)
    register_order_tools(filtered)
    register_kis_live_order_tools(filtered)
    register_toss_live_order_tools(filtered)
    register_kiwoom_mock_tools(filtered)


__all__ = [
    "ACCOUNT_READ_FORBIDDEN_TOOL_NAMES",
    "ACCOUNT_READ_TOOL_NAMES",
    "KIWOOM_MOCK_ACCOUNT_READ_TOOL_NAMES",
    "register_account_read_tools",
]
