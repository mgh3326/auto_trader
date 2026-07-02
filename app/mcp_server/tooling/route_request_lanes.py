"""Static lane definitions + pure route-plan builder for route_request (ROB-649).

No MCP dependency — fully unit-testable. Lane definitions are ported from the
machine-readable ``lanes:`` blocks of docs/playbooks/trading-decision-playbook.md
(ROB-643, the definition source) and kept in sync by
tests/test_route_request_registry_diff.py. Thresholds are NOT stored here — they
come from get_trading_policy (ROB-646); hard_constraints reference policy KEYS,
never values.
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import TOSS_LIVE_ORDER_TOOL_NAMES

# intent enum (the only free LLM choice) -> playbook lane
INTENT_TO_LANE: dict[str, str] = {
    "buy_analysis": "buy",
    "profit_taking": "sell",
    "discovery": "discovery",
    "market_brief": "bootstrap",
}

VALID_MARKETS: frozenset[str] = frozenset({"kr", "us", "crypto"})

# playbook lane -> get_trading_policy lane (bootstrap has no policy thresholds)
LANE_TO_POLICY_LANE: dict[str, str | None] = {
    "buy": "buy",
    "sell": "sell",
    "discovery": "discovery",
    "bootstrap": None,
}

# Ordered standard tool sequence per lane, ported from the playbook lanes: blocks.
LANE_SEQUENCES: dict[str, list[dict[str, Any]]] = {
    "bootstrap": [
        {
            "tool": "get_operating_briefing",
            "purpose": "holdings, pending orders, latest report, session_context, analysis_artifacts",
        },
        {
            "tool": "session_context_get_recent",
            "purpose": "yesterday's decision journal",
        },
        {
            "tool": "analysis_artifact_list",
            "purpose": "reusable prior analysis (metadata)",
        },
        {
            "tool": "analysis_artifact_get",
            "purpose": "on-demand body fetch for a specific artifact",
        },
        {"tool": "get_market_index", "purpose": "market regime"},
        {"tool": "get_fx_rate", "purpose": "FX"},
    ],
    "buy": [
        {
            "tool": "get_operating_briefing",
            "purpose": "load prior-session decisions + positions",
        },
        {"tool": "get_market_index", "purpose": "market regime"},
        {"tool": "get_fx_rate", "purpose": "FX"},
        {
            "tool": "analyze_stock_batch",
            "purpose": "RSI, honest consensus, support/resistance, per-account position (mode=quick, include_position, <=10)",
        },
        {
            "tool": "get_intraday_investor_flow",
            "purpose": "foreign-flow gate (recovery_gate)",
        },
        {
            "tool": "toss_place_order",
            "purpose": "execute buy — Toss preferred (fee-free); deep limit, no chasing",
        },
        {
            "tool": "kis_live_place_order",
            "purpose": "spend down KIS deposit; dry_run preview -> live",
        },
    ],
    "sell": [
        {
            "tool": "toss_get_positions",
            "purpose": "scan in-the-money / near-breakeven names",
        },
        {
            "tool": "analyze_stock_batch",
            "purpose": "confirm distance to resistance, RSI, upside",
        },
        {
            "tool": "toss_place_order",
            "purpose": "sell-into-strength split ladder just under resistance",
        },
        {
            "tool": "sell_ladder_fill_preview",
            "purpose": "ROB-477 bottom-anchor rung, fill-safety",
        },
    ],
    "discovery": [
        {
            "tool": "screen_stocks_snapshot",
            "purpose": "multi-source fan-out candidate pool",
        },
        {"tool": "get_top_stocks", "purpose": "losers fan-out"},
        {"tool": "get_momentum_candidates", "purpose": "momentum fan-out"},
        {"tool": "screen_stocks", "purpose": "value/RSI screen fan-out"},
        {"tool": "get_sector_peers", "purpose": "rotation-sector peers"},
        {"tool": "get_disclosures", "purpose": "rights-issue / overhang filter"},
        {"tool": "analyze_stock_batch", "purpose": "deep confirm on ranked survivors"},
        {"tool": "toss_place_order", "purpose": "winners only, support-line limit"},
    ],
}

# Per-lane hard-constraint summaries. Reference policy KEYS, never values.
HARD_CONSTRAINTS: dict[str, list[str]] = {
    "buy": [
        "recovery gate: deploy reserve only when >= recovery_gate.min_conditions_met of 4 conditions",
        "loss guard (sell-side): sell price >= avg * sell.loss_guard_min_multiple",
        "KRX tick rounding",
        "DAY order expiry at order.day_expiry_kst -> re-place next day",
        "no two-sided (buy+sell) resting orders on same Toss symbol",
        "over-concentration cap: portfolio.sector_cluster_cap_pct per sector cluster",
        "portfolio.max_symbols_per_theme per theme; add-not-cut (average down, no stop-loss)",
    ],
    "sell": [
        "loss guard: sell price >= avg * sell.loss_guard_min_multiple",
        "KRX tick rounding",
        "no two-sided (buy+sell) resting orders on same Toss symbol",
        "DAY order expiry at order.day_expiry_kst -> re-place next day",
        "preserve core lot; trim over-concentrated sectors first (portfolio.sector_cluster_cap_pct)",
    ],
    "discovery": [
        "over-concentration cap: portfolio.sector_cluster_cap_pct per sector cluster",
        "portfolio.max_symbols_per_theme per theme",
        "rights-issue / overhang filter before ranking",
        "per-symbol sizing: buy.per_symbol_notional_krw_range",
    ],
    "bootstrap": [
        "context-load only; no order mutation in this lane",
        "recovery gate frame: recovery_gate.min_conditions_met of 4",
        "account routing: buys prefer Toss (fee-free); KIS deposit spent down in-account",
    ],
}

MUTATION_TOOLS: frozenset[str] = frozenset(
    ORDER_TOOL_NAMES
    | KIS_LIVE_ORDER_TOOL_NAMES
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | TOSS_LIVE_ORDER_TOOL_NAMES
    | KIWOOM_MOCK_TOOL_NAMES
)

# Every non-mutation tool in the DEFAULT profile (computed 2026-07-02 as
# DEFAULT-profile tools minus MUTATION_TOOLS) plus route_request itself. The
# set-equality partition test (test_route_request_registry_diff.py) fails if a
# new DEFAULT tool is not classified here or in MUTATION_TOOLS — this is the
# drift guard the issue requires.
READ_ONLY_ADVISORY_TOOLS: frozenset[str] = frozenset(
    {
        "route_request",
        "analysis_artifact_get",
        "analysis_artifact_list",
        "analysis_artifact_save",
        "analyze_portfolio",
        "analyze_stock",
        "analyze_stock_batch",
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
        "get_top_stocks",
        "get_toss_ai_signal",
        "get_toss_buy_balance",
        "get_trade_journal",
        "get_trade_retrospectives",
        "get_trading_policy",
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
        "investment_watch_recommend",
        "list_active_journals",
        "list_active_watches",
        "modify_journal_entry",
        "research_session_get",
        "research_session_list_recent",
        "research_summary_get",
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
        "trade_retrospective_pending",
        "update_manual_holdings",
        "update_trade_journal",
    }
)

ALL_KNOWN_TOOLS: frozenset[str] = READ_ONLY_ADVISORY_TOOLS | MUTATION_TOOLS


def lane_tool_names(lane: str) -> set[str]:
    return {step["tool"] for step in LANE_SEQUENCES[lane]}


def build_route_plan(
    intent: str,
    market: str,
    *,
    registered_tools: set[str],
    verdict_thresholds: dict[str, Any],
    policy_version: dict[str, str],
) -> dict[str, Any]:
    """Assemble the deterministic route plan. Pure — no IO. Caller validates
    intent/market and resolves policy before calling."""
    lane = INTENT_TO_LANE[intent]
    seq_steps = [
        step for step in LANE_SEQUENCES[lane] if step["tool"] in registered_tools
    ]
    standard_tool_sequence = [
        {"step": i, "tool": step["tool"], "purpose": step["purpose"]}
        for i, step in enumerate(seq_steps, start=1)
    ]
    lane_own_mutation = lane_tool_names(lane) & MUTATION_TOOLS
    allowed = (lane_tool_names(lane) | set(READ_ONLY_ADVISORY_TOOLS)) & registered_tools
    blocked = (MUTATION_TOOLS - lane_own_mutation) & registered_tools
    return {
        "success": True,
        "intent": intent,
        "lane": lane,
        "market": market,
        "standard_tool_sequence": standard_tool_sequence,
        "allowed_tools": sorted(allowed),
        "blocked_actions": sorted(blocked),
        "verdict_thresholds": verdict_thresholds,
        "policy_version": policy_version,
        "hard_constraints": list(HARD_CONSTRAINTS[lane]),
    }


__all__ = [
    "INTENT_TO_LANE",
    "VALID_MARKETS",
    "LANE_TO_POLICY_LANE",
    "LANE_SEQUENCES",
    "HARD_CONSTRAINTS",
    "MUTATION_TOOLS",
    "READ_ONLY_ADVISORY_TOOLS",
    "ALL_KNOWN_TOOLS",
    "lane_tool_names",
    "build_route_plan",
]
