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
    "get_financials",
    "get_insider_transactions",
    "get_earnings_calendar",
    "get_investor_trends",
    "get_intraday_investor_flow",
    "get_investment_opinions",
    "get_valuation",
    "get_short_interest",
    "get_market_index",
    "get_fx_rate",
    "suggest_order_account",
    "get_support_resistance",
    "get_sector_peers",
    "get_cash_balance",
    "cancel_order",
    "update_manual_holdings",
    "analyze_stock",
    "analyze_portfolio",
    "get_disclosures",
    "get_correlation",
    "get_top_stocks",
    "get_dividends",
    "get_order_history",
    "modify_order",
    "screen_stocks",
    # ROB-359: recommend_stocks is registry-hidden (parked); not on the tool surface.
    "get_mock_loop_retrospective",
    "save_trade_retrospective",
    "get_trade_retrospectives",
    "get_retrospective_aggregate",
    "trade_retrospective_pending",
    # ROB-650: resolvable forecast ledger
    "forecast_save",
    "forecast_resolve",
    "get_forecasts",
    "get_forecast_calibration",
    # Crypto research tools
    "get_crypto_profile",
    "get_kimchi_premium",
    "get_crypto_funding_rate",
    "get_crypto_open_interest",
    "get_crypto_long_short_ratio",
    "get_crypto_fear_greed",
]


def __getattr__(name: str) -> Any:
    if name == "register_all_tools":
        value = getattr(import_module("app.mcp_server.tooling.registry"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
