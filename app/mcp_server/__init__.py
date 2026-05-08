"""MCP server package exports.

Keep this package import side-effect-light: importing a specific MCP helper
submodule must not pull the full tool registry/order stack into read-only API
paths. Load registry exports lazily on demand.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["register_all_tools"]

AVAILABLE_TOOL_NAMES = [
    "search_symbol",
    "get_quote",
    "get_orderbook",
    "get_holdings",
    "get_position",
    "place_order",
    "get_ohlcv",
    "get_indicators",
    "get_news",
    "get_company_profile",
    "get_crypto_profile",
    "get_financials",
    "get_insider_transactions",
    "get_earnings_calendar",
    "get_investor_trends",
    "get_investment_opinions",
    "get_valuation",
    "get_short_interest",
    "get_kimchi_premium",
    "get_funding_rate",
    "get_market_index",
    "get_support_resistance",
    "get_sector_peers",
    "get_cash_balance",
    "cancel_order",
    "simulate_avg_cost",
    "update_manual_holdings",
    "analyze_stock",
    "analyze_portfolio",
    "get_disclosures",
    "get_correlation",
    "get_top_stocks",
    "get_dividends",
    "get_fear_greed_index",
    "get_order_history",
    "modify_order",
    "screen_stocks",
    "recommend_stocks",
    "manage_watch_alerts",
    "get_asset_profile",
    "set_asset_profile",
]


def __getattr__(name: str) -> Any:
    if name == "register_all_tools":
        value = getattr(import_module("app.mcp_server.tooling.registry"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
