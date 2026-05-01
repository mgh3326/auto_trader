"""Tool registration orchestration for MCP server.

Profile → tool surface mapping
────────────────────────────────────────────────────────────────────────────
"default" (McpProfile.DEFAULT):
  All side-effect-free research tools + read-only portfolio tools +
  legacy ambiguous order tools (place_order / cancel_order / modify_order /
  get_order_history with account_mode switching) +
  typed kis_live_* and kis_mock_* variants (additive).

"hermes-paper-kis" (McpProfile.HERMES_PAPER_KIS):
  All side-effect-free research tools + read-only portfolio tools +
  typed kis_mock_* variants ONLY.
  Explicitly omits: register_order_tools, register_kis_live_order_tools.

See app/mcp_server/profiles.py and docs in app/mcp_server/README.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.alpaca_paper import register_alpaca_paper_tools
from app.mcp_server.tooling.alpaca_paper_orders import (
    register_alpaca_paper_orders_tools,
)
from app.mcp_server.tooling.alpaca_paper_preview import (
    register_alpaca_paper_preview_tools,
)
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
from app.mcp_server.tooling.orders_kis_variants import (
    register_kis_live_order_tools,
    register_kis_mock_order_tools,
)
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
from app.mcp_server.tooling.paperclip_comment_registration import (
    register_paperclip_comment_tools,
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


def register_all_tools(mcp: FastMCP, profile: McpProfile = McpProfile.DEFAULT) -> None:
    """Register MCP tools according to the given profile.

    Side-effect-free research and read-only tools are always registered.
    Side-effect order tool registration depends on profile:
      - DEFAULT: legacy ambiguous tools + typed kis_live_* + typed kis_mock_*
      - HERMES_PAPER_KIS: typed kis_mock_* only (live surface absent)
    """
    # Always: side-effect-free research + read-only tools
    register_market_data_tools(mcp)
    register_fundamentals_tools(mcp)
    register_analysis_tools(mcp)
    register_watch_alert_tools(mcp)
    register_trade_profile_tools(mcp)
    register_market_report_tools(mcp)
    register_user_settings_tools(mcp)
    register_news_tools(mcp)
    register_market_brief_tools(mcp)
    register_alpaca_paper_tools(mcp)
    register_alpaca_paper_preview_tools(mcp)
    register_alpaca_paper_orders_tools(mcp)

    # Always: read-only with account_mode (mock-safe via ROB-28)
    register_portfolio_tools(mcp)
    register_trade_journal_tools(mcp)
    register_paperclip_comment_tools(mcp)
    register_execution_comment_tools(mcp)
    register_paper_account_tools(mcp)
    register_paper_analytics_tools(mcp)
    register_paper_journal_tools(mcp)

    # Profile-gated: side-effect order surfaces
    if profile is McpProfile.DEFAULT:
        # Preserve today's behavior: ambiguous account_mode tools for legacy callers.
        # Typed kis_live_* and kis_mock_* are additive — new typed callers use them.
        register_order_tools(mcp)
        register_kis_live_order_tools(mcp)
        register_kis_mock_order_tools(mcp)
    elif profile is McpProfile.HERMES_PAPER_KIS:
        # Paper-only: only mock-pinned order surface. Live surface is physically absent.
        register_kis_mock_order_tools(mcp)
        # Intentionally NOT: register_order_tools, register_kis_live_order_tools


__all__ = ["register_all_tools"]
