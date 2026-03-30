"""MCP tooling modules for domain-based tool organization.

This package contains the refactored MCP tools split by domain:
- shared: Common utilities, normalizers, and constants
- market_data_quotes / market_data_indicators / market_data_registration
- fundamentals_handlers / fundamentals_sources_naver / fundamentals_sources_* / fundamentals_registration
- orders_history / orders_modify_cancel / orders_registration
- order_execution: Order execution pipeline helpers
- portfolio_holdings / portfolio_cash / portfolio_registration
- analysis_screening: Stock analysis and screening implementations
- analysis_screen_core: Stock screening core helpers
- analysis_rankings: Ranking and correlation helpers
- analysis_recommend: Recommend-stocks helpers
- registry: Tool registration orchestration
"""

from __future__ import annotations

from app.mcp_server.tooling.news_registration import (
    NEWS_TOOL_NAMES,
    register_news_tools,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.trade_journal_registration import (
    TRADE_JOURNAL_TOOL_NAMES,
    register_trade_journal_tools,
)
from app.mcp_server.tooling.trade_profile_registration import (
    TRADE_PROFILE_TOOL_NAMES,
    register_trade_profile_tools,
)
from app.mcp_server.tooling.watch_alerts_registration import (
    WATCH_ALERT_TOOL_NAMES,
    register_watch_alert_tools,
)

__all__ = [
    "WATCH_ALERT_TOOL_NAMES",
    "TRADE_PROFILE_TOOL_NAMES",
    "NEWS_TOOL_NAMES",
    "TRADE_JOURNAL_TOOL_NAMES",
    "register_all_tools",
    "register_trade_journal_tools",
    "register_trade_profile_tools",
    "register_watch_alert_tools",
    "register_news_tools",
]
