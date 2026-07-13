"""Tool registration orchestration for MCP server.

Profile → tool surface mapping
────────────────────────────────────────────────────────────────────────────
"default" (McpProfile.DEFAULT):
  All side-effect-free research tools (crypto research included — ROB-503) +
  read-only portfolio tools +
  legacy ambiguous order tools (place_order / cancel_order / modify_order /
  get_order_history with account_mode switching) +
  typed kis_live_* and kis_mock_* variants (additive). Split-profile tools
  (Alpaca/us-dual paper, DB paper, Kiwoom mock) are omitted.

"hermes-paper-kis" (McpProfile.HERMES_PAPER_KIS):
  All side-effect-free research tools + read-only portfolio tools +
  typed kis_mock_* variants ONLY.
  Explicitly omits: register_order_tools, register_kis_live_order_tools.

"crypto" (McpProfile.CRYPTO):
  Default research/read-only surface (crypto research tools register on every
  profile since ROB-503), the generic account_mode order tools (crypto live
  trading entry point), and live_reconcile_orders (US/crypto evidence-gated
  settle).

"us-paper" (McpProfile.US_PAPER):
  Default research/read-only surface plus Alpaca paper and us_dual_paper tools.

"db-paper" (McpProfile.DB_PAPER):
  Default research/read-only surface plus internal DB paper simulator tools.

"kiwoom" (McpProfile.KIWOOM):
  Default research/read-only surface plus typed kiwoom_mock_* variants.

"shadow-replay" (McpProfile.SHADOW_REPLAY):
  ROB-697 M1 — frozen-context replay ONLY. Registers EXACTLY
  investment_report_get_hermes_context (read-only) + get_trading_policy +
  route_request, then returns before the "Always" block. Deliberately omits
  every live-fetch tool (market_data/analysis/news/fundamentals), every
  mutation/order tool, and the 4 Hermes WRITE tools — this is the load-bearing
  validity guard so a headless replay session cannot leak live market data or
  persist anything.

"analysis_readonly" (McpProfile.ANALYSIS_READONLY):
  Codex/headless read/analysis allowlist only. Registers operating briefing,
  policy/route, selected market/fundamental/analysis/holdings tools,
  toss_get_positions, and explicitly labeled analysis persistence. No order,
  cancel, modify, reconcile, preview, settings, watch mutation, admin, or manual
  holdings mutation tools are registered.

"account_read" (McpProfile.ACCOUNT_READ):
  TradingCodex adapter account-read allowlist only. Registers holdings, cash,
  and read-only order-history tools needed for account synchronization. No
  order placement, cancel, modify, preview, reconcile, persistence, settings,
  watch, admin, report-write, or manual-holdings mutation tools are registered.

"tradingcodex_execution" (McpProfile.TRADINGCODEX_EXECUTION):
  TradingCodex broker execution allowlist only. Registers account reads,
  policy/route advisory reads, account-routing suggestion, USD/KRW FX read,
  watch read tools (active watches + delivered watch events), learning-loop
  reads/writes (forecasts + trade retrospectives with explicit created_by
  provenance), dry-run/preview, live place, cancel, and ladder fill-preview
  tools required by the reviewed BrokerAdapter. No modify, reconcile, settings,
  watch mutation/activation, report-write, KIS mock, Kiwoom, Alpaca, or paper
  simulator tools are registered.

See app/mcp_server/profiles.py and docs in app/mcp_server/README.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import orders_kiwoom_variants
from app.mcp_server.tooling.account_read_registration import (
    register_account_read_tools,
)
from app.mcp_server.tooling.account_routing_registration import (
    register_account_routing_tools,
)
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
from app.mcp_server.tooling.analysis_artifact_registration import (
    register_analysis_artifact_tools,
)
from app.mcp_server.tooling.analysis_bundle_handlers import (
    register_analysis_bundle_tools,
)
from app.mcp_server.tooling.analysis_readonly_registration import (
    register_analysis_readonly_tools,
)
from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.execution_ledger_events import (
    register_execution_ledger_event_tools,
)
from app.mcp_server.tooling.forecast_registration import register_forecast_tools
from app.mcp_server.tooling.fundamentals_registration import register_fundamentals_tools
from app.mcp_server.tooling.investment_hermes_handlers import (
    register_hermes_context_read_only,
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
from app.mcp_server.tooling.mirror_counterfactual_registration import (
    register_mirror_counterfactual_tools,
)
from app.mcp_server.tooling.mock_loop_retro_registration import (
    register_mock_loop_retro_tools,
)
from app.mcp_server.tooling.news_registration import register_news_tools
from app.mcp_server.tooling.operating_briefing_registration import (
    register_operating_briefing_tools,
)
from app.mcp_server.tooling.order_proposal_tools import register_order_proposal_tools
from app.mcp_server.tooling.orders_kis_variants import (
    register_kis_live_order_tools,
    register_kis_mock_order_tools,
    register_live_reconcile_tools,
)
from app.mcp_server.tooling.orders_registration import register_order_tools
from app.mcp_server.tooling.orders_toss_variants import (
    register_toss_live_order_tools,
)
from app.mcp_server.tooling.paper_account_registration import (
    register_paper_account_tools,
)
from app.mcp_server.tooling.paper_analytics_registration import (
    register_paper_analytics_tools,
)
from app.mcp_server.tooling.paper_journal_registration import (
    register_paper_journal_tools,
)
from app.mcp_server.tooling.paper_limit_order_handler import (
    register_paper_limit_order_tools,
)
from app.mcp_server.tooling.portfolio_registration import register_portfolio_tools
from app.mcp_server.tooling.route_request_registration import (
    register_route_request_tools,
)
from app.mcp_server.tooling.session_context_registration import (
    register_session_context_tools,
)
from app.mcp_server.tooling.toss_manual_activity_tools import (
    register_toss_manual_activity_tools,
)
from app.mcp_server.tooling.trade_journal_registration import (
    register_trade_journal_tools,
)
from app.mcp_server.tooling.trade_retrospective_registration import (
    register_trade_retrospective_tools,
)
from app.mcp_server.tooling.trading_policy_registration import (
    register_trading_policy_tools,
)
from app.mcp_server.tooling.trading_scoreboard_registration import (
    register_trading_scoreboard_tools,
)
from app.mcp_server.tooling.tradingcodex_execution_registration import (
    register_tradingcodex_execution_tools,
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
    if profile is McpProfile.SHADOW_REPLAY:
        # ROB-697 M1 — frozen-context replay ONLY: read the bundle + policy +
        # lane procedure. Deliberately NO live-fetch (market_data/analysis/
        # news), NO mutation, NO report-write. The agent returns its decision
        # as JSON; it does not persist. This early return is the load-bearing
        # validity guard, so it must come before the "Always" block below.
        register_hermes_context_read_only(mcp)  # investment_report_get_hermes_context
        register_trading_policy_tools(mcp)  # get_trading_policy (versioned thresholds)
        register_route_request_tools(mcp)  # route_request (lane procedure)
        return

    if profile is McpProfile.ANALYSIS_READONLY:
        # ROB-745 — Codex/headless analysis surface. Allowlist-only and returns
        # before the normal "Always" block so unlisted research, account,
        # settings, watch, report-write, and order tools are physically absent.
        register_analysis_readonly_tools(mcp)
        return

    if profile is McpProfile.ACCOUNT_READ:
        # ROB-760 — TradingCodex account adapter surface. Allowlist-only and
        # returns before the normal "Always" block so research, persistence,
        # settings, watch, preview, reconcile, and mutation tools are absent.
        register_account_read_tools(mcp)
        return

    if profile is McpProfile.TRADINGCODEX_EXECUTION:
        # ROB-762 — TradingCodex broker execution surface. Allowlist-only and
        # returns before the normal default block so broad research, settings,
        # watch, modify, reconcile, and persistence tools are physically absent.
        register_tradingcodex_execution_tools(mcp)
        return

    # Always: side-effect-free research + read-only tools
    register_market_data_tools(mcp)
    register_fundamentals_tools(mcp)
    register_analysis_tools(mcp)
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
    register_session_context_tools(mcp)
    register_analysis_artifact_tools(mcp)
    if settings.ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED:
        register_analysis_bundle_tools(mcp)
    register_operating_briefing_tools(mcp)
    # ROB-646 — read-only policy thresholds + version stamp; always registered
    # so every profile can cite the stamp when recording a verdict.
    register_trading_policy_tools(mcp)
    # ROB-649 — advisory lane router; always registered (read-only, no order
    # surface). Intersects lane tool sequences with the live-registered surface.
    register_route_request_tools(mcp)
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
    register_account_routing_tools(mcp)
    register_trade_journal_tools(mcp)
    # ROB-755 — execution ledger fill event read tool; read-only, always registered.
    register_execution_ledger_event_tools(mcp)
    register_mock_loop_retro_tools(mcp)
    register_trade_retrospective_tools(mcp)
    register_forecast_tools(mcp)
    register_trading_scoreboard_tools(mcp)
    register_mirror_counterfactual_tools(mcp)
    # ROB-713 — setup-tagged trade-journal aggregates; read-only, registered
    # unconditionally like the forecast tools it parallels.

    # ROB-269 Phase 2 — investment-snapshot MCP surface. Gated by
    # ``settings.INVESTMENT_SNAPSHOTS_MCP_ENABLED`` so the 3 read tools are
    # physically absent unless the flag is flipped post-PR-merge.
    if settings.INVESTMENT_SNAPSHOTS_MCP_ENABLED:
        register_investment_snapshots_tools(mcp)

    # ROB-816 — order_proposals SOT ledger read/create surface. Gated by
    # ``settings.ORDER_PROPOSALS_ENABLED`` (default off). No approve/submit
    # tool is registered anywhere — approval is Telegram-only (PR 2).
    if settings.ORDER_PROPOSALS_ENABLED:
        register_order_proposal_tools(mcp)

    # Profile-gated: side-effect order surfaces
    if profile is McpProfile.DEFAULT:
        # ROB-703: paper resting-limit sim tools (pure simulation, no live/Upbit mutation).
        register_paper_limit_order_tools(mcp)
        # Preserve today's behavior: ambiguous account_mode tools for legacy callers.
        # Typed kis_live_* and kis_mock_* are additive — new typed callers use them.
        register_order_tools(mcp)
        register_kis_live_order_tools(mcp)
        register_kis_mock_order_tools(mcp)
        register_live_reconcile_tools(mcp)
        register_toss_live_order_tools(mcp)
        # ROB-866: Toss manual-activity detection sweep (read-only; alert-only).
        register_toss_manual_activity_tools(mcp)
        # ROB-601: optionally surface kiwoom_mock_* in the operator DEFAULT
        # session so analyze→approval→order can run through kiwoom mock without
        # switching to the isolated KIWOOM profile (which drops every other
        # broker's order surface). Gated by ``settings.kiwoom_mock_enabled`` so
        # the tools are physically absent unless the operator opts in; each tool
        # still fail-closes on missing credentials at call time.
        if settings.kiwoom_mock_enabled:
            orders_kiwoom_variants.register(mcp)
        if settings.binance_demo_scalping_enabled:
            from app.mcp_server.tooling.binance_demo_scalping_handler import (
                register_binance_demo_scalping_tools,
            )

            register_binance_demo_scalping_tools(mcp)
    elif profile is McpProfile.HERMES_PAPER_KIS:
        # Paper-only: only mock-pinned order surface. Live surface is physically absent.
        register_kis_mock_order_tools(mcp)
        # Intentionally NOT: register_order_tools, register_kis_live_order_tools
    elif profile is McpProfile.US_PAPER:
        from app.mcp_server.tooling.alpaca_paper_automated_orders import (
            register_alpaca_paper_automated_orders_tools,
        )

        register_alpaca_paper_tools(mcp)
        register_alpaca_paper_preview_tools(mcp)
        register_us_dual_paper_tools(mcp)
        register_alpaca_paper_orders_tools(mcp)
        register_alpaca_paper_automated_orders_tools(mcp)
        register_alpaca_paper_ledger_read_tools(mcp)
    elif profile is McpProfile.DB_PAPER:
        register_paper_account_tools(mcp)
        register_paper_analytics_tools(mcp)
        register_paper_journal_tools(mcp)
    elif profile is McpProfile.KIWOOM:
        orders_kiwoom_variants.register(mcp)
    elif profile is McpProfile.CRYPTO:
        # Crypto live trading enters through the generic account_mode order
        # tools (the only MCP entry point for Upbit orders, with ROB-407
        # inline reconcile) and settles through live_reconcile_orders.
        # Without these a crypto session could research but never trade.
        register_order_tools(mcp)
        register_live_reconcile_tools(mcp)


__all__ = ["register_all_tools"]
