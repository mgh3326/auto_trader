"""MCP tooling modules for domain-based tool organization.

This package contains the refactored MCP tools split by domain:
- shared: Common utilities, normalizers, and constants
- market_data_quotes / market_data_indicators / market_data_registration
- fundamentals_handlers / fundamentals_sources_common / fundamentals_sources_yfinance / fundamentals_sources_naver / fundamentals_sources_finnhub / fundamentals_registration
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

from importlib import import_module
from typing import Any

# ROB-447: MARKET_REPORT_TOOL_NAMES / register_market_report_tools removed — the
# report판 MCP tools were dead (shadowed by the brief판). The report판 SERVICE
# (app/services/market_report_service.py) stays for the n8n write path + weekly_summary.
__all__ = [
    "INVESTMENT_HERMES_TOOL_NAMES",
    "INVESTMENT_SNAPSHOTS_TOOL_NAMES",
    "TRADE_PROFILE_TOOL_NAMES",
    "NEWS_TOOL_NAMES",
    "TRADE_JOURNAL_TOOL_NAMES",
    "register_all_tools",
    "register_investment_hermes_tools",
    "register_investment_snapshots_tools",
    "register_trade_journal_tools",
    "register_trade_profile_tools",
    "register_news_tools",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "INVESTMENT_HERMES_TOOL_NAMES": (
        "app.mcp_server.tooling.investment_hermes_handlers",
        "INVESTMENT_HERMES_TOOL_NAMES",
    ),
    "register_investment_hermes_tools": (
        "app.mcp_server.tooling.investment_hermes_handlers",
        "register_investment_hermes_tools",
    ),
    "INVESTMENT_SNAPSHOTS_TOOL_NAMES": (
        "app.mcp_server.tooling.investment_snapshots_registration",
        "INVESTMENT_SNAPSHOTS_TOOL_NAMES",
    ),
    "register_investment_snapshots_tools": (
        "app.mcp_server.tooling.investment_snapshots_registration",
        "register_investment_snapshots_tools",
    ),
    "NEWS_TOOL_NAMES": (
        "app.mcp_server.tooling.news_registration",
        "NEWS_TOOL_NAMES",
    ),
    "register_news_tools": (
        "app.mcp_server.tooling.news_registration",
        "register_news_tools",
    ),
    "register_all_tools": (
        "app.mcp_server.tooling.registry",
        "register_all_tools",
    ),
    "TRADE_JOURNAL_TOOL_NAMES": (
        "app.mcp_server.tooling.trade_journal_registration",
        "TRADE_JOURNAL_TOOL_NAMES",
    ),
    "register_trade_journal_tools": (
        "app.mcp_server.tooling.trade_journal_registration",
        "register_trade_journal_tools",
    ),
    "TRADE_PROFILE_TOOL_NAMES": (
        "app.mcp_server.tooling.trade_profile_registration",
        "TRADE_PROFILE_TOOL_NAMES",
    ),
    "register_trade_profile_tools": (
        "app.mcp_server.tooling.trade_profile_registration",
        "register_trade_profile_tools",
    ),
}


def __getattr__(name: str) -> Any:
    """Load public registration helpers lazily.

    Importing this package should be side-effect-light so tools such as coverage can
    resolve a specific submodule without importing the whole MCP registry tree first.
    """
    try:
        module_name, attr_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
