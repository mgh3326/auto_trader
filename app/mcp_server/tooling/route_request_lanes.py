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

from app.mcp_server.tooling.alpaca_paper import ALPACA_PAPER_READONLY_TOOL_NAMES
from app.mcp_server.tooling.alpaca_paper_automated_orders import (
    ALPACA_PAPER_AUTOMATED_TOOL_NAMES,
)
from app.mcp_server.tooling.alpaca_paper_orders import ALPACA_PAPER_MUTATING_TOOL_NAMES
from app.mcp_server.tooling.alpaca_paper_preview import ALPACA_PAPER_PREVIEW_TOOL_NAMES
from app.mcp_server.tooling.market_quote_snapshot_tools import (
    MARKET_QUOTE_SNAPSHOT_TOOL_NAMES,
)
from app.mcp_server.tooling.mirror_counterfactual_registration import (
    MIRROR_COUNTERFACTUAL_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_us_variants import (
    KIWOOM_MOCK_US_MUTATION_TOOL_NAMES,
    KIWOOM_MOCK_US_READ_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import TOSS_LIVE_ORDER_TOOL_NAMES
from app.mcp_server.tooling.us_dual_paper import US_DUAL_PAPER_TOOL_NAMES

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
            "tool": "order_proposal_create",
            "purpose": "create place proposal; Telegram human approval is required before proposal-owned revalidation and submit",
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
            "tool": "sell_ladder_fill_preview",
            "purpose": "ROB-477 bottom-anchor rung, fill-safety",
        },
        {
            "tool": "order_proposal_create",
            "purpose": "create place/cancel/replace proposal; Telegram human approval is required before proposal-owned revalidation and submit",
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
        "order intent: order_proposal_create only; Telegram human approval required",
        "accepted/resting is not a fill; broker evidence reconcile is required",
        "negative class: record each reviewed-but-rejected candidate as a "
        "decision_bucket=deferred_no_action item with confidence + rejection "
        "reason, and leave a resolvable forecast_save (price_target with required "
        "outcome_rule_version='window-touch-v1-high-gte-low-lte', e.g. "
        "'no +X% within N days') so calibration isn't censored (ROB-712)",
    ],
    "sell": [
        "loss guard: sell price >= avg * sell.loss_guard_min_multiple",
        "KRX tick rounding",
        "no two-sided (buy+sell) resting orders on same Toss symbol",
        "DAY order expiry at order.day_expiry_kst -> re-place next day",
        "preserve core lot; trim over-concentrated sectors first (portfolio.sector_cluster_cap_pct)",
        "order intent: order_proposal_create only; Telegram human approval required",
        "sell from the holding account selected in the proposal",
        "same-symbol buy pending -> separate cancel proposal and confirmed broker evidence before the sell proposal",
        "accepted/resting is not a fill; broker evidence reconcile is required",
    ],
    "discovery": [
        "over-concentration cap: portfolio.sector_cluster_cap_pct per sector cluster",
        "portfolio.max_symbols_per_theme per theme",
        "rights-issue / overhang filter before ranking",
        "per-symbol sizing: buy.per_symbol_notional_krw_range",
        "negative class: record each reviewed-but-rejected candidate as a "
        "decision_bucket=deferred_no_action item with confidence + rejection "
        "reason, and leave a resolvable forecast_save (price_target with required "
        "outcome_rule_version='window-touch-v1-high-gte-low-lte', e.g. "
        "'no +X% within N days') so calibration isn't censored (ROB-712)",
    ],
    "bootstrap": [
        "context-load only; no order mutation in this lane",
        "recovery gate frame: recovery_gate.min_conditions_met of 4",
        "account routing: buys prefer Toss (fee-free); KIS deposit spent down in-account",
    ],
}

ROUTE_CONTRACT_VERSION = "proposal-led-v1"
PROPOSAL_TOOL = "order_proposal_create"
PROPOSAL_LED_LANES: frozenset[str] = frozenset({"buy", "sell"})

# The legacy MUTATION_TOOLS bucket intentionally remains public and broad for
# backwards compatibility. ROB-1045 overlays explicit, disjoint action classes
# so proposal-led lanes can fail closed on direct broker mutations without also
# blocking pure previews, reconcile reads/writes, or read/status helpers.
DIRECT_BROKER_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "alpaca_paper_cancel_order",
        "alpaca_paper_automated_submit_order",
        "alpaca_paper_submit_order",
        "cancel_order",
        "kis_live_cancel_order",
        "kis_live_modify_order",
        "kis_live_place_order",
        "kis_mock_cancel_order",
        "kis_mock_mirror_execute_report",
        "kis_mock_modify_order",
        "kis_mock_place_order",
        "kiwoom_mock_cancel_order",
        "kiwoom_mock_modify_order",
        "kiwoom_mock_place_order",
        "kiwoom_mock_us_cancel_order",
        "kiwoom_mock_us_modify_order",
        "kiwoom_mock_us_place_order",
        "modify_order",
        "paper_cancel_pending_order",
        "paper_place_limit_order",
        "place_order",
        "toss_cancel_order",
        "toss_modify_order",
        "toss_place_order",
    }
)

PROPOSAL_LED_TOOLS: frozenset[str] = frozenset({PROPOSAL_TOOL})
PROPOSAL_LIFECYCLE_TOOLS: frozenset[str] = frozenset(
    {
        "order_proposal_expire_sweep",
        "order_proposal_redispatch",
        "order_proposal_void",
    }
)
ORDER_PROPOSAL_READ_TOOLS: frozenset[str] = frozenset(
    {
        "order_proposal_get",
        "order_proposal_list",
        "order_proposal_list_expired_defensive",
    }
)

PREVIEW_REVALIDATION_TOOLS: frozenset[str] = frozenset(
    {
        "alpaca_paper_automated_preview_order",
        "buy_ladder_fill_preview",
        "kiwoom_mock_preview_order",
        "kiwoom_mock_us_preview_order",
        "sell_ladder_fill_preview",
        "toss_preview_order",
    }
)

RECONCILE_TOOLS: frozenset[str] = frozenset(
    {
        "alpaca_paper_reconcile_orders",
        "kis_live_reconcile_orders",
        "kis_mock_reconciliation_run",
        "live_reconcile_orders",
        "paper_reconcile_orders",
        "toss_reconcile_orders",
    }
)

# These are read/status/non-broker helper tools that remain in MUTATION_TOOLS
# because that legacy bucket predates the action taxonomy.
STATUS_HELPER_TOOLS: frozenset[str] = frozenset(
    {
        "get_order_history",
        "kis_live_get_order_history",
        "kis_mock_get_order_history",
        "kiwoom_mock_get_order_history",
        # ROB-1155: kt00007 read-only order-detail lookup. Lands in the legacy
        # MUTATION_TOOLS bucket only because KIWOOM_MOCK_TOOL_NAMES is unioned in
        # wholesale; it never calls the order client.
        "kiwoom_mock_get_order_detail",
        "kiwoom_mock_get_orderable_cash",
        "kiwoom_mock_get_positions",
        "toss_get_order_history",
        "toss_get_orderable_cash",
        "toss_get_positions",
        # ROB-971: lifecycle writes for direct investment watches (local DB state
        # mutation only, no direct broker order placement).
        "investment_watch_void",
        "investment_watch_expire",
        "sweep_expired_watches",
    }
)

_LEGACY_MUTATION_TOOLS: frozenset[str] = frozenset(
    ORDER_TOOL_NAMES
    | ALPACA_PAPER_AUTOMATED_TOOL_NAMES
    | KIS_LIVE_ORDER_TOOL_NAMES
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | TOSS_LIVE_ORDER_TOOL_NAMES
    | KIWOOM_MOCK_TOOL_NAMES
    | KIWOOM_MOCK_US_MUTATION_TOOL_NAMES
    | MIRROR_COUNTERFACTUAL_TOOL_NAMES
    # ROB-908/ROB-953: Alpaca paper confirm-gated mutations — submit/cancel plus
    # alpaca_paper_reconcile_orders, which reads the broker read-only but WRITES
    # lifecycle state to review.alpaca_paper_order_ledger. Flag-
    # gated in DEFAULT (settings.alpaca_paper_default_tools_enabled, default off);
    # the read/preview/us_dual/ledger surface is read-only and lives in
    # READ_ONLY_ADVISORY_TOOLS. The automated preview/submit pair is US_PAPER-
    # only (ROB-842) but is classified above because route_request is present
    # on that profile too.
    | frozenset(ALPACA_PAPER_MUTATING_TOOL_NAMES)
    | frozenset(
        {
            # ROB-703: paper resting-limit sim mutations (paper-table writes only,
            # no live/Upbit broker mutation). paper_list_pending_orders is read-only
            # and lives in READ_ONLY_ADVISORY_TOOLS.
            "paper_place_limit_order",
            "paper_cancel_pending_order",
            "paper_reconcile_orders",
            # ROB-971: lifecycle writes for direct investment watches.
            "investment_watch_void",
            "investment_watch_expire",
            "sweep_expired_watches",
        }
    )
)

MUTATION_TOOLS: frozenset[str] = (
    _LEGACY_MUTATION_TOOLS | PROPOSAL_LED_TOOLS | PROPOSAL_LIFECYCLE_TOOLS
)

# ROB-658's market-aware direct execution mapping remains only for discovery,
# which the operator explicitly excluded from ROB-1045. Buy/sell never consult
# this mapping and never fall back to a direct place tool.
MARKET_EXECUTION_TOOLS: dict[str, frozenset[str]] = {
    "kr": frozenset(),
    "us": frozenset({"place_order"}),
    "crypto": frozenset({"place_order"}),
}

# Direct placement tools used only to preserve discovery's ROB-658 fallback.
_PLACE_ORDER_TOOLS: frozenset[str] = frozenset(
    {"place_order", "toss_place_order", "kis_live_place_order"}
)

# Discovery keeps its direct Toss preview precursor. Proposal-led buy/sell do
# not expose broker previews: fresh preview/revalidation is owned internally by
# the proposal approval subsystem. sell_ladder_fill_preview remains a pure
# pre-proposal fill-safety step because it is explicitly sequenced in sell.
PREVIEW_TOOLS: frozenset[str] = frozenset({"toss_preview_order"})

# ROB-660 / ROB-666: per-lane allowed-only helper tools. The order-status tools
# (kis_live_get_order_history / toss_get_order_history) are read-only in reality
# but bucketed in MUTATION_TOOLS for registry partitioning, so build_route_plan
# would otherwise block them even in the lane that needs them. The sell lane needs
# them to confirm a cancel took effect and to check sell-order fill status
# (ROB-660); the buy lane needs them to confirm a buy fill and to check KIS
# regular-session survival after the 15:30 expiry (ROB-657 rule) so it can
# re-place (ROB-666). They are un-blocked here (allowed) WITHOUT entering the
# ordered sequence (confirmation helpers, not workflow steps) or the playbook YAML.
# Parallels MARKET_EXECUTION_TOOLS (ROB-658) as an allowed supplement. A minimal
# per-lane allowance (not a MUTATION_TOOLS -> READ_ONLY reclassification) keeps
# discovery/bootstrap unchanged.
LANE_EXTRA_ALLOWED: dict[str, frozenset[str]] = {
    "buy": frozenset({"kis_live_get_order_history", "toss_get_order_history"}),
    "sell": frozenset({"kis_live_get_order_history", "toss_get_order_history"}),
}

# Registered reconcile helpers are conditional allowed helpers for proposal-led
# lanes. They are deliberately not ordered because route_request has no broker
# or account_mode input with which to select one.
LANE_RECONCILE_ALLOWED: dict[str, frozenset[str]] = {
    "buy": RECONCILE_TOOLS,
    "sell": RECONCILE_TOOLS,
}

# Purpose text for discovery's legacy market execution injection.
_MARKET_EXEC_PURPOSE: dict[str, str] = {
    "discovery": "execute buy on ranked winners via generic place_order (crypto/US limit)",
}

# Every non-mutation tool in the DEFAULT profile (computed 2026-07-02 as
# DEFAULT-profile tools minus MUTATION_TOOLS) plus route_request itself. The
# set-equality partition test (test_route_request_registry_diff.py) fails if a
# new DEFAULT tool is not classified here or in MUTATION_TOOLS — this is the
# drift guard the issue requires.
READ_ONLY_ADVISORY_TOOLS: frozenset[str] = frozenset(
    {
        *KIWOOM_MOCK_US_READ_TOOL_NAMES,
        # ROB-908: Alpaca paper read surface, flag-gated in DEFAULT
        # (settings.alpaca_paper_default_tools_enabled, default off). Covers the
        # account/positions/orders/assets/fills + ledger reads
        # (ALPACA_PAPER_READONLY_TOOL_NAMES), the pure-validator preview
        # (ALPACA_PAPER_PREVIEW_TOOL_NAMES — no side effects, does not submit),
        # and the read-only us_dual capability/state/preview trio
        # (US_DUAL_PAPER_TOOL_NAMES, submit_enabled always False). The
        # confirm-gated submit/cancel mutations and the DB-writing
        # alpaca_paper_reconcile_orders live in MUTATION_TOOLS.
        *ALPACA_PAPER_READONLY_TOOL_NAMES,
        *ALPACA_PAPER_PREVIEW_TOOL_NAMES,
        *US_DUAL_PAPER_TOOL_NAMES,
        *MARKET_QUOTE_SNAPSHOT_TOOL_NAMES,
        *ORDER_PROPOSAL_READ_TOOLS,
        "route_request",
        "analysis_artifact_get",
        "analysis_artifact_list",
        "analysis_artifact_save",
        "analysis_bundle_create",
        "analysis_bundle_get",
        "analyze_portfolio",
        "analyze_stock",
        "analyze_stock_batch",
        # ROB-907: read-only Demo ledger status (flag-gated —
        # settings.binance_demo_scalping_enabled). The mutation-path submit
        # tool this once shared a gate comment with was removed (ROB-1147).
        "binance_demo_ledger_status",
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
        "investment_watch_recommend",
        "list_active_journals",
        "list_active_watches",
        "investment_watch_events_list_recent",
        "modify_journal_entry",
        "paper_list_pending_orders",
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
        # ROB-866: detection sweep (no broker order mutation; alert-only side effect
        # like session_context_append, which is likewise advisory-classified).
        "toss_detect_manual_activity",
        "trade_retrospective_pending",
        "update_manual_holdings",
        "update_trade_journal",
        # ROB-928: notify-only downside watch sweep (dry_run default; only
        # mutation is an investment_watch_alerts INSERT, no broker/order path).
        "watch_downside_register_sweep",
    }
)

ALL_KNOWN_TOOLS: frozenset[str] = READ_ONLY_ADVISORY_TOOLS | MUTATION_TOOLS


def ordered_lane_tool_names(lane: str) -> list[str]:
    return [step["tool"] for step in LANE_SEQUENCES[lane]]


def lane_tool_names(lane: str) -> set[str]:
    """Compatibility set view; ordered_lane_tool_names is the contract source."""
    return set(ordered_lane_tool_names(lane))


def _route_contract(
    lane: str,
    *,
    registered_tools: set[str] | None,
) -> dict[str, Any]:
    proposal_led = lane in PROPOSAL_LED_LANES
    required_tools = sorted(PROPOSAL_LED_TOOLS) if proposal_led else []
    missing_required_tools = (
        required_tools
        if registered_tools is None
        else sorted(set(required_tools) - registered_tools)
    )
    execution_ready = registered_tools is not None and not missing_required_tools

    if proposal_led:
        execution_mode = "proposal_led"
        proposal_tool: str | None = PROPOSAL_TOOL
        approval_channel = "telegram"
        human_approval_required = True
        preview_owner = "proposal_revalidation"
        reconcile_requirement = "broker_evidence"
    elif lane == "discovery":
        execution_mode = "legacy_direct"
        proposal_tool = None
        approval_channel = "not_applicable"
        human_approval_required = False
        preview_owner = "lane_operator"
        reconcile_requirement = "legacy_unspecified"
    else:
        execution_mode = "read_only"
        proposal_tool = None
        approval_channel = "not_applicable"
        human_approval_required = False
        preview_owner = "not_applicable"
        reconcile_requirement = "not_applicable"

    return {
        "version": ROUTE_CONTRACT_VERSION,
        "state": "ready" if execution_ready else "degraded",
        "execution_mode": execution_mode,
        "execution_ready": execution_ready,
        "proposal_tool": proposal_tool,
        "approval_channel": approval_channel,
        "human_approval_required": human_approval_required,
        "preview_owner": preview_owner,
        "reconcile_requirement": reconcile_requirement,
        "required_tools": required_tools,
        "missing_required_tools": missing_required_tools,
    }


def build_registry_unavailable_plan(
    intent: str,
    market: str,
    *,
    verdict_thresholds: dict[str, Any],
    policy_version: dict[str, str],
) -> dict[str, Any]:
    """Return a stable fail-closed response when live registry state is unknown."""
    lane = INTENT_TO_LANE[intent]
    return {
        "success": False,
        "error": "registry_introspection_unavailable",
        "degraded": True,
        "intent": intent,
        "lane": lane,
        "market": market,
        "standard_tool_sequence": [],
        "allowed_tools": [],
        "blocked_actions": sorted(DIRECT_BROKER_MUTATION_TOOLS),
        "blocked_actions_basis": "static_fail_closed",
        "route_contract": _route_contract(lane, registered_tools=None),
        "verdict_thresholds": verdict_thresholds,
        "policy_version": policy_version,
        "hard_constraints": list(HARD_CONSTRAINTS[lane]),
    }


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
    lane_tools = lane_tool_names(lane)
    proposal_led = lane in PROPOSAL_LED_LANES
    lane_place_tools = (
        lane_tools & _PLACE_ORDER_TOOLS if lane == "discovery" else frozenset()
    )
    # Only the explicitly out-of-scope discovery lane retains ROB-658's generic
    # crypto/US direct-place fallback.
    market_exec = (
        MARKET_EXECUTION_TOOLS.get(market, frozenset())
        if lane_place_tools
        else frozenset()
    )

    seq_steps = [
        step
        for step in LANE_SEQUENCES[lane]
        if step["tool"] in registered_tools
        and (not proposal_led or step["tool"] not in DIRECT_BROKER_MUTATION_TOOLS)
    ]
    if lane_place_tools and not (lane_place_tools & registered_tools):
        for tool in sorted(market_exec & registered_tools):
            seq_steps.append({"tool": tool, "purpose": _MARKET_EXEC_PURPOSE[lane]})

    standard_tool_sequence = [
        {"step": i, "tool": step["tool"], "purpose": step["purpose"]}
        for i, step in enumerate(seq_steps, start=1)
    ]
    # Discovery still surfaces its direct Toss preview precursor. Proposal-led
    # lanes leave broker preview/revalidation to the approval subsystem.
    lane_preview = PREVIEW_TOOLS if lane == "discovery" else frozenset()
    lane_extra = LANE_EXTRA_ALLOWED.get(lane, frozenset())
    lane_reconcile = LANE_RECONCILE_ALLOWED.get(lane, frozenset())
    safe_lane_tools = (
        lane_tools - DIRECT_BROKER_MUTATION_TOOLS if proposal_led else lane_tools
    )
    allowed_candidates = (
        safe_lane_tools
        | market_exec
        | lane_preview
        | lane_extra
        | lane_reconcile
        | set(READ_ONLY_ADVISORY_TOOLS)
    )
    allowed = allowed_candidates & registered_tools
    blocked = (MUTATION_TOOLS & registered_tools) - allowed
    route_contract = _route_contract(lane, registered_tools=registered_tools)
    success = route_contract["execution_ready"]

    result: dict[str, Any] = {
        "success": success,
        "degraded": not success,
        "intent": intent,
        "lane": lane,
        "market": market,
        "standard_tool_sequence": standard_tool_sequence,
        "allowed_tools": sorted(allowed),
        "blocked_actions": sorted(blocked),
        "blocked_actions_basis": "live_registered_surface",
        "route_contract": route_contract,
        "verdict_thresholds": verdict_thresholds,
        "policy_version": policy_version,
        "hard_constraints": list(HARD_CONSTRAINTS[lane]),
    }
    if not success:
        result["error"] = "required_route_tool_unavailable"
    return result


__all__ = [
    "INTENT_TO_LANE",
    "VALID_MARKETS",
    "LANE_TO_POLICY_LANE",
    "LANE_SEQUENCES",
    "HARD_CONSTRAINTS",
    "ROUTE_CONTRACT_VERSION",
    "PROPOSAL_TOOL",
    "PROPOSAL_LED_LANES",
    "DIRECT_BROKER_MUTATION_TOOLS",
    "PROPOSAL_LED_TOOLS",
    "PROPOSAL_LIFECYCLE_TOOLS",
    "ORDER_PROPOSAL_READ_TOOLS",
    "PREVIEW_REVALIDATION_TOOLS",
    "RECONCILE_TOOLS",
    "STATUS_HELPER_TOOLS",
    "MARKET_EXECUTION_TOOLS",
    "PREVIEW_TOOLS",
    "LANE_EXTRA_ALLOWED",
    "LANE_RECONCILE_ALLOWED",
    "MUTATION_TOOLS",
    "READ_ONLY_ADVISORY_TOOLS",
    "ALL_KNOWN_TOOLS",
    "ordered_lane_tool_names",
    "lane_tool_names",
    "build_registry_unavailable_plan",
    "build_route_plan",
]
