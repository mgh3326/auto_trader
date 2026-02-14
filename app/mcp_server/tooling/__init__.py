"""MCP tooling modules for domain-based tool organization.

This package contains the refactored MCP tools split by domain:
- shared: Common utilities, normalizers, and constants
- market_data_quotes / market_data_indicators / market_data_registration
- fundamentals_handlers / fundamentals_sources_* / fundamentals_registration
- orders_history / orders_modify_cancel / orders_registration
- order_execution: Order execution pipeline helpers
- portfolio_holdings / portfolio_cash / portfolio_registration
- analysis_screening: Stock analysis and screening implementations
- analysis_screen_core: Stock screening core helpers
- analysis_rankings: Ranking and correlation helpers
- analysis_recommend: Recommend-stocks helpers
- registry: Tool registration orchestration
"""

from app.mcp_server.tooling.registry import register_all_tools

__all__ = ["register_all_tools"]
