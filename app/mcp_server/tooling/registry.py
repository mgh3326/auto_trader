"""Tool registration orchestration for MCP server.

Profile → tool surface mapping
────────────────────────────────────────────────────────────────────────────
"default" (McpProfile.DEFAULT):
  All side-effect-free research tools + read-only portfolio tools +
  legacy ambiguous order tools (place_order / cancel_order / modify_order /
  get_order_history with account_mode switching) +
  typed kis_live_* and kis_mock_* variants (additive). Split-profile tools
  (crypto-only, Alpaca/us-dual paper, Kiwoom mock) are omitted.

"hermes-paper-kis" (McpProfile.HERMES_PAPER_KIS):
  All side-effect-free research tools + read-only portfolio tools +
  typed kis_mock_* variants ONLY.
  Explicitly omits: register_order_tools, register_kis_live_order_tools.

"crypto" (McpProfile.CRYPTO):
  Default research/read-only surface plus crypto-only research/regime tools.

"us-paper" (McpProfile.US_PAPER):
  Default research/read-only surface plus Alpaca paper and us_dual_paper tools.

"db-paper" (McpProfile.DB_PAPER):
  Default research/read-only surface plus internal DB paper simulator tools.

"kiwoom" (McpProfile.KIWOOM):
  Default research/read-only surface plus typed kiwoom_mock_* variants.

See app/mcp_server/profiles.py and docs in app/mcp_server/README.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import orders_kiwoom_variants
from app.mcp_server.tooling.alpaca_paper import register_alpaca_paper_tools
from app.mcp_server.tooling.alpaca_paper_ledger_read import (
    register_alpaca_paper_ledger_read_tools,
)
from app.mcp_server.tooling.alpaca_paper_orders import (
    register_alpaca_paper_orders_tools,
)
from app.mcp_server.tooling.alpaca_paper_preview import (
    register_alpaca_paper_preview_tools,
)
from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.fundamentals_registration import register_fundamentals_tools
from app.mcp_server.tooling.investment_hermes_handlers import (
    register_investment_hermes_tools,
)
from app.mcp_server.tooling.investment_reports_handlers import (
    register_investment_report_tools,
)
from app.mcp_server.tooling.investment_snapshots_registration import (
    register_investment_snapshots_tools,
)
from app.mcp_server.tooling.market_brief_registration import (
    register_market_brief_tools,
)
from app.mcp_server.tooling.market_data_registration import register_market_data_tools
from app.mcp_server.tooling.mock_loop_retro_registration import (
    register_mock_loop_retro_tools,
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
from app.mcp_server.tooling.portfolio_registration import register_portfolio_tools
from app.mcp_server.tooling.trade_journal_registration import (
    register_trade_journal_tools,
)
from app.mcp_server.tooling.trade_retrospective_registration import (
    register_trade_retrospective_tools,
)
from app.mcp_server.tooling.us_dual_paper import register_us_dual_paper_tools
from app.mcp_server.tooling.user_settings_registration import (
    register_user_settings_tools,
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
    include_crypto_tools = profile is McpProfile.CRYPTO
    register_market_data_tools(mcp)
    register_fundamentals_tools(mcp, include_crypto=include_crypto_tools)
    register_analysis_tools(mcp, include_crypto=include_crypto_tools)
    # ROB-488 — snapshot-backed report generation/Hermes tools are default-off
    # by physical registration, matching the investment_snapshot gate. The
    # implementation functions keep their disabled checks for direct/internal
    # callers and HTTP routes.
    snapshot_report_generator_enabled = (
        settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED
    )
    register_investment_report_tools(
        mcp,
        include_snapshot_generator=snapshot_report_generator_enabled,
    )
    if snapshot_report_generator_enabled:
        register_investment_hermes_tools(mcp)
    # ROB-447: register_market_report_tools removed — its get_market_reports /
    # get_latest_market_brief were silently shadowed by register_market_brief_tools
    # (registered later, default on_duplicate="warn" = last wins). The brief판
    # (per-symbol AI analysis history) is the operator-observed surface. The report판
    # SERVICE (app/services/market_report_service.py) stays — it is the n8n write path
    # + weekly_summary consumer; only its dead MCP tool registration is dropped.
    register_user_settings_tools(mcp)
    register_news_tools(mcp)
    register_market_brief_tools(mcp)

    # Always: live/mock account read-only tools and journals.
    register_portfolio_tools(mcp)
    register_trade_journal_tools(mcp)
    register_mock_loop_retro_tools(mcp)
    register_trade_retrospective_tools(mcp)

    # ROB-269 Phase 2 — investment-snapshot MCP surface. Gated by
    # ``settings.INVESTMENT_SNAPSHOTS_MCP_ENABLED`` so the 4 tools are
    # physically absent unless the flag is flipped post-PR-merge.
    if settings.INVESTMENT_SNAPSHOTS_MCP_ENABLED:
        register_investment_snapshots_tools(mcp)

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
    elif profile is McpProfile.US_PAPER:
        register_alpaca_paper_tools(mcp)
        register_alpaca_paper_preview_tools(mcp)
        register_us_dual_paper_tools(mcp)
        register_alpaca_paper_orders_tools(mcp)
        register_alpaca_paper_ledger_read_tools(mcp)
    elif profile is McpProfile.DB_PAPER:
        register_paper_account_tools(mcp)
        register_paper_analytics_tools(mcp)
        register_paper_journal_tools(mcp)
    elif profile is McpProfile.KIWOOM:
        orders_kiwoom_variants.register(mcp)
    elif profile is McpProfile.CRYPTO:
        pass


__all__ = ["register_all_tools"]
