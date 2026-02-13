"""MCP tooling modules for domain-based tool organization.

This package contains the refactored MCP tools split by domain:
- shared: Common utilities, normalizers, and constants
- market_data: Quote, OHLCV, and indicator tools
- fundamentals: News, profile, financials tools
- orders: Order placement and management tools
- portfolio: Holdings and position management tools
- analysis_screening: Stock analysis and screening tools
- registry: Tool registration orchestration
"""

from app.mcp_server.tooling.registry import register_all_tools

__all__ = ["register_all_tools"]
