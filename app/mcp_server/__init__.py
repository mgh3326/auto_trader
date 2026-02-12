"""auto_trader MCP server (Market Data Tools)

This package exposes a small set of read-only tools over MCP (HTTP/SSE).
"""

from app.mcp_server.tools import register_tools

__all__ = ["register_tools"]

# Available MCP tools (registered via register_tools function)
AVAILABLE_TOOL_NAMES = [
    "get_holdings",
    "get_positions_by_account",
    "screen_stocks",
    "get_stock_price",
    "analyze_stock",
    "trade_stock",
    "cancel_order",
    "get_orders",
    "get_account_balance",
    "get_favorite_stocks",
    "list_recent_filings",
]
