"""Tool registration orchestration for MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.execution_comment_registration import (
    register_execution_comment_tools,
)
from app.mcp_server.tooling.fundamentals_registration import register_fundamentals_tools
from app.mcp_server.tooling.market_brief_registration import (
    register_market_brief_tools,
)
from app.mcp_server.tooling.market_data_registration import register_market_data_tools
from app.mcp_server.tooling.market_report_registration import (
    register_market_report_tools,
)
from app.mcp_server.tooling.news_registration import register_news_tools
from app.mcp_server.tooling.orders_registration import register_order_tools
from app.mcp_server.tooling.paper_account_registration import (
    register_paper_account_tools,
)
from app.mcp_server.tooling.paper_analytics_registration import (
    register_paper_analytics_tools,
)
from app.mcp_server.tooling.paper_journal_registration import (
    register_paper_journal_tools,
)
from app.mcp_server.tooling.portfolio_registration import register_portfolio_tools
from app.mcp_server.tooling.trade_journal_registration import (
    register_trade_journal_tools,
)
from app.mcp_server.tooling.trade_profile_registration import (
    register_trade_profile_tools,
)
from app.mcp_server.tooling.user_settings_registration import (
    register_user_settings_tools,
)
from app.mcp_server.tooling.watch_alerts_registration import (
    register_watch_alert_tools,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_all_tools(mcp: FastMCP) -> None:
    register_market_data_tools(mcp)
    register_portfolio_tools(mcp)
    register_order_tools(mcp)
    register_fundamentals_tools(mcp)
    register_analysis_tools(mcp)
    register_watch_alert_tools(mcp)
    register_trade_profile_tools(mcp)
    register_market_report_tools(mcp)
    register_user_settings_tools(mcp)
    register_news_tools(mcp)
    register_trade_journal_tools(mcp)
    register_paper_account_tools(mcp)
    register_paper_analytics_tools(mcp)
    register_paper_journal_tools(mcp)
    register_execution_comment_tools(mcp)
    register_market_brief_tools(mcp)


__all__ = ["register_all_tools"]
