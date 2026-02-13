"""MCP tooling modules for domain-based tool organization.

This package contains the refactored MCP tools split by domain:
- shared: Common utilities, normalizers, and constants
- market_data: Quote, OHLCV, and indicator tools
- fundamentals: News, profile, financials tools
- orders: Order placement and management tools
- order_execution: Order execution pipeline helpers
- portfolio: Holdings and position management tools
- analysis_screening: Stock analysis and screening tool registration
- analysis_screen_core: Stock screening core helpers
- analysis_rankings: Ranking and correlation helpers
- analysis_recommend: Recommend-stocks helpers
- legacy_tools: Legacy nested MCP tool implementations (temporary bridge)
- registry: Tool registration orchestration
"""

from app.mcp_server.tooling.registry import register_all_tools

__all__ = ["register_all_tools"]
