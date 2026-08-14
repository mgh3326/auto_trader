"""KR-only Kiwoom mock MCP profile registration (ROB-1159).

Least-privilege split of ``MCP_PROFILE=kiwoom``.

``MCP_PROFILE=kiwoom`` registers **both** Kiwoom mock namespaces
unconditionally (``app/mcp_server/tooling/registry.py``, KIWOOM branch): the
eight KR ``kiwoom_mock_*`` tools **and** the seven US ``kiwoom_mock_us_*``
tools, four of which are mutations
(``KIWOOM_MOCK_US_MUTATION_TOOL_NAMES``). Unlike the DEFAULT profile — where
the US namespace is behind ``settings.kiwoom_mock_us_enabled`` (ROB-867) — the
KIWOOM branch has no such gate, so selecting that profile *physically exposes*
the US mutation surface even for a session that only needs KR reads/orders
(for example KR-B1, whose forced profile it is).

``MCP_PROFILE=kiwoom_kr`` is that profile minus the whole US namespace and
every foreign direct broker mutation:

- Same shared read-only research/account surface (this profile does **not**
  early-return before the "Always" block), so it is a drop-in replacement for
  ``kiwoom`` in a KR session.
- Exactly the eight KR ``kiwoom_mock_*`` tools as its order surface, including
  the ROB-1155 ``kiwoom_mock_get_order_detail`` (kt00007) read.
- The broad shared block's KIS mock mirror executor is physically absent; the
  whole profile is driven through a closed-world exact-set proxy, so a future
  foreign alias is dropped even when no central mutation-name list knows it.
- The US registrar is never invoked. Its exported name sets are imported only
  as negative contract evidence; no US tool implementation is registered.
- The KR registrar has a second eight-name exact-set proxy so a future US (or
  any other unlisted) registration added inside
  ``orders_kiwoom_variants.register`` is also dropped at registration time.

🔴 The KR order path is untouched: this module only chooses what is registered.
``dmst_stex_tp=KRX`` pinning, ``MOCK_REJECTED_EXCHANGES``, the
``dry_run=False`` + ``confirm=True`` double gate, and every place/cancel/modify
body live in ``orders_kiwoom_variants`` / ``app/services/brokers/kiwoom/`` and
are reused as-is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from app.core.config import settings
from app.mcp_server.tooling.analysis_bundle_handlers import ANALYSIS_BUNDLE_TOOL_NAMES
from app.mcp_server.tooling.analysis_readonly_registration import _AllowlistedMCP
from app.mcp_server.tooling.investment_hermes_handlers import (
    INVESTMENT_HERMES_TOOL_NAMES,
)
from app.mcp_server.tooling.investment_snapshots_registration import (
    INVESTMENT_SNAPSHOTS_TOOL_NAMES,
)
from app.mcp_server.tooling.order_proposal_tools import ORDER_PROPOSAL_TOOL_NAMES
from app.mcp_server.tooling.orders_kiwoom_us_variants import (
    KIWOOM_MOCK_US_MUTATION_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_kiwoom_variants import (
    register as register_kiwoom_mock_tools,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


# The complete order surface of MCP_PROFILE=kiwoom_kr: the KR namespace only.
KIWOOM_KR_TOOL_NAMES: set[str] = set(KIWOOM_MOCK_TOOL_NAMES)

# Re-exported so regression tests can name the four excluded US mutations.
# Importing this name metadata does not invoke the US registrar or register any
# US implementation on the profile.
KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES: set[str] = set(
    KIWOOM_MOCK_US_MUTATION_TOOL_NAMES
)

# Closed-world base inventory for the entire ``kiwoom_kr`` profile when its
# four optional shared feature gates are disabled. This literal is deliberately
# independent of registrar-owned ``*_TOOL_NAMES`` collections: a new tool must
# cross this profile-specific review boundary before it can be exposed. The
# runtime proxy in ``registry.register_all_tools`` drops every unlisted name.
KIWOOM_KR_BASE_PROFILE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "analysis_artifact_get",
        "analysis_artifact_list",
        "analysis_artifact_save",
        "analyze_portfolio",
        "analyze_stock",
        "analyze_stock_batch",
        "discover_buy_candidates_fanout",
        "execution_ledger_fill_events_list_recent",
        "forecast_resolve",
        "forecast_save",
        "get_analyst_consensus",
        "get_available_capital",
        "get_cash_balance",
        "get_company_profile",
        "get_correlation",
        "get_cost_basis_distribution",
        "get_crypto_catalysts",
        "get_crypto_fear_greed",
        "get_crypto_funding_rate",
        "get_crypto_long_short_ratio",
        "get_crypto_market_regime",
        "get_crypto_open_interest",
        "get_crypto_order_flow",
        "get_crypto_profile",
        "get_crypto_social",
        "get_crypto_top_movers",
        "get_disclosures",
        "get_dividends",
        "get_earnings_calendar",
        "get_execution_strength",
        "get_financials",
        "get_forecast_calibration",
        "get_forecasts",
        "get_fx_rate",
        "get_holdings",
        "get_holdings_news",
        "get_indicators",
        "get_insider_transactions",
        "get_intraday_investor_flow",
        "get_investment_opinions",
        "get_investor_trends",
        "get_kimchi_premium",
        "get_krx_session_health",
        "get_latest_market_brief",
        "get_market_index",
        "get_market_issues",
        "get_market_news",
        "get_market_reports",
        "get_mock_loop_retrospective",
        "get_momentum_candidates",
        "get_news",
        "get_ohlcv",
        "get_operating_briefing",
        "get_orderbook",
        "get_portfolio_allocation",
        "get_position",
        "get_quote",
        "get_retail_sentiment",
        "get_retrospective_aggregate",
        "get_sector_peers",
        "get_short_interest",
        "get_support_resistance",
        "get_theme_events",
        "get_top_stocks",
        "get_toss_ai_signal",
        "get_toss_buy_balance",
        "get_trade_journal",
        "get_trade_retrospectives",
        "get_trading_policy",
        "get_trading_scoreboard",
        "get_upbit_altseason",
        "get_upbit_index",
        "get_user_setting",
        "get_valuation",
        "investment_report_activate_watch",
        "investment_report_add_items",
        "investment_report_context_get",
        "investment_report_create",
        "investment_report_decide_item",
        "investment_report_delta_get",
        "investment_report_get",
        "investment_report_list",
        "investment_report_set_status",
        "investment_report_update",
        "investment_watch_create",
        "investment_watch_events_list_recent",
        "investment_watch_expire",
        "investment_watch_recommend",
        "investment_watch_void",
        "kiwoom_mock_cancel_order",
        "kiwoom_mock_get_order_detail",
        "kiwoom_mock_get_order_history",
        "kiwoom_mock_get_orderable_cash",
        "kiwoom_mock_get_positions",
        "kiwoom_mock_modify_order",
        "kiwoom_mock_place_order",
        "kiwoom_mock_preview_order",
        "list_active_journals",
        "list_active_watches",
        "modify_journal_entry",
        "research_session_get",
        "research_session_list_recent",
        "research_summary_get",
        "route_request",
        "save_trade_journal",
        "save_trade_retrospective",
        "screen_stocks",
        "screen_stocks_snapshot",
        "search_symbol",
        "session_context_append",
        "session_context_get_recent",
        "set_user_setting",
        "stage_analysis_get",
        "suggest_order_account",
        "sweep_expired_watches",
        "trade_retrospective_pending",
        "update_manual_holdings",
        "update_trade_journal",
        "watch_downside_register_sweep",
    }
)

_SNAPSHOT_GENERATOR_TOOL_NAMES = {"investment_report_generate_from_bundle"}


def kiwoom_kr_profile_tool_names() -> set[str]:
    """Return the exact active profile inventory for the current feature gates."""
    names = set(KIWOOM_KR_BASE_PROFILE_TOOL_NAMES)
    if settings.ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED:
        names.update(ANALYSIS_BUNDLE_TOOL_NAMES)
    if settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED:
        names.update(_SNAPSHOT_GENERATOR_TOOL_NAMES)
        names.update(INVESTMENT_HERMES_TOOL_NAMES)
    if settings.INVESTMENT_SNAPSHOTS_MCP_ENABLED:
        names.update(INVESTMENT_SNAPSHOTS_TOOL_NAMES)
    if settings.ORDER_PROPOSALS_ENABLED:
        names.update(ORDER_PROPOSAL_TOOL_NAMES)
    return names


def restrict_kiwoom_kr_profile_tools(mcp: FastMCP) -> FastMCP:
    """Drop every registration outside the active closed-world profile set."""
    return cast("FastMCP", _AllowlistedMCP(mcp, kiwoom_kr_profile_tool_names()))


def register_kiwoom_kr_tools(mcp: FastMCP) -> None:
    """Register the KR-only Kiwoom mock order surface.

    The allowlist proxy is load-bearing, not decorative: it is what keeps this
    profile KR-only if the KR registrar ever grows a non-KR tool.
    """
    filtered = cast("FastMCP", _AllowlistedMCP(mcp, KIWOOM_KR_TOOL_NAMES))
    register_kiwoom_mock_tools(filtered)


__all__ = [
    "KIWOOM_KR_BASE_PROFILE_TOOL_NAMES",
    "KIWOOM_KR_EXCLUDED_US_MUTATION_TOOL_NAMES",
    "KIWOOM_KR_TOOL_NAMES",
    "kiwoom_kr_profile_tool_names",
    "register_kiwoom_kr_tools",
    "restrict_kiwoom_kr_profile_tools",
]
