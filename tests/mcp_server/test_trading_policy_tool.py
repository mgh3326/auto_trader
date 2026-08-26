from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.trading_policy_tools import get_trading_policy
from app.services.trading_policy_service import policy_version_stamp
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.asyncio
async def test_get_trading_policy_returns_thresholds_and_version():
    out = await get_trading_policy(market="kr", lane="buy")
    assert out["success"] is True
    assert out["version"] == policy_version_stamp()["version"]
    assert out["content_hash"]
    assert out["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert set(out["decision_rules"]) == {
        "buy.support_reserve_net",
        "buy.preplanned_support_ladder",
        "buy.winner_pullback_add",
        # §139차 — the index-ETF admission is a KR/US equity-universe rule.
        "buy.index_etf_candidate",
    }
    reserve = out["decision_rules"]["buy.support_reserve_net"]
    assert reserve["eligible_only_when_regular_gate_failure"] == "RSI_ONLY"
    assert reserve["honest_upside_reference"] == "decision_time_current_price"
    assert reserve["discount_below_support_pct_range"] == [5, 10]
    assert reserve["final_limit_distance_from_current_pct_range"] == [-15, -5]
    assert reserve["final_limit_distance_out_of_range"] == "EXCLUDE"
    assert reserve["tier_armed_required_cash_cap_pct"] == 50
    assert reserve["unknown_sector"] == "INELIGIBLE"
    assert reserve["cash_reservation"]["broker_orderable_unavailable_or_error"] == (
        "FAIL_CLOSED"
    )
    assert reserve["fill_triage"] == {
        "on_first_confirmed_fill": "FREEZE_NEW_SUBMITS",
        "cancellation_mode": "PROPOSAL_REQUIRES_APPROVAL",
        "broker_cancel_confirmation_required_before_releasing_cash": True,
        "same_session_rearm": False,
        "unknown_or_ambiguous_order_state": "KEEP_RESERVED_AND_BLOCK",
        "burst_key": ["broker_account_id", "currency", "market_session"],
    }
    assert reserve["priority_rules"] == {
        "allocation_order": [
            "dedupe_active_or_resting_same_symbol",
            "first_slot_eligible_new_candidate",
            "add_secondary_pool_only_after_r931_pass_and_full_a_limit_10",
        ],
        "same_symbol_active_or_resting": "DEDUPE_FIRST",
        "first_slot": "ELIGIBLE_NEW_CANDIDATE_FIRST",
        "add_candidate_rank": "SECONDARY_CANDIDATE_POOL",
        "add_candidate_r931_review_required": "PASS",
        "add_candidate_a_limit_10": "FULLY_SATISFIED",
        "max_add_symbols_per_market": 2,
        "same_intent_class_sort_order": [
            "support_strength_desc",
            "independent_support_source_count_desc",
            "honest_upside_pct_desc",
            "post_fill_sector_increase_asc",
            "required_cash_asc",
        ],
        "exact_tie_break": "NEW_BEFORE_ADD",
    }
    assert reserve["add_candidate"]["a_limit_lte_zero"] == "NO_ORDER"
    assert reserve["add_candidate"]["partial_A_limit_fill"] == "FORBIDDEN"
    assert reserve["toss_live_approval"] == (
        "HUMAN_APPROVAL_REQUIRED_UNTIL_VETO_WIRING"
    )


@pytest.mark.asyncio
async def test_get_trading_policy_returns_crypto_market_rules_and_stamp():
    out = await get_trading_policy(market="crypto", lane="buy")

    assert out["success"] is True
    assert out["version"] == policy_version_stamp()["version"]
    assert len(out["content_hash"]) == 12
    gate = out["market_rules"]["recovery_gate"]
    assert gate["min_conditions_met"] == 2
    assert gate["of"] == 2
    assert [context["id"] for context in gate["advisory_context"]] == [
        "fear_greed",
        "btc_kimchi_premium",
    ]
    assert out["market_rules"]["no_chasing"]["daily_change_pct_threshold"] is None


@pytest.mark.asyncio
async def test_get_trading_policy_returns_sell_trim_preplace_rule():
    out = await get_trading_policy(market="kr", lane="sell")
    assert out["success"] is True
    rule = out["decision_rules"]["sell.trim_preplace"]
    tiers = {tier["id"]: tier for tier in rule["tiers"]}
    assert tiers["de_minimis_trim_watch"]["action"] == "register_watch_instead_of_trim"
    assert tiers["single_share_full_exit_review"]["sizing"] == "full_position"
    assert (
        tiers["momentum_spike_profit_ladder"]["conditions"]["rsi_gate_exempt"] is True
    )
    assert (
        tiers["momentum_spike_profit_ladder"]["conditions"][
            "ladder_total_position_pct_max"
        ]
        == 33.3333
    )
    assert tiers["ultra_near_resistance"]["conditions"]["resistance_near_pct_max"] == 2
    assert tiers["watch_zone"]["action"] == "register_watch"
    # ROB-1298 §115차 — the zero-named-resistance fallback ships through the
    # same read tool, last in priority, reusing the existing trim sizing.
    ladder = tiers["breakeven_extension_ladder"]
    assert list(tiers)[-1] == "breakeven_extension_ladder"
    assert ladder["sizing"] == "existing_trim_rule"
    assert ladder["conditions"]["fresh_named_resistance_count_eq"] == 0
    assert ladder["conditions"]["markets"] == ["kr", "us", "crypto"]
    assert ladder["conditions"]["anchor_average_cost_multiples"] == [1.01, 1.05, 1.10]
    assert ladder["conditions"]["tick_snap_direction"] == "ceil"
    assert (
        ladder["conditions"]["anchor_lowest_rung_policy_key"]
        == "sell.loss_guard_min_multiple"
    )
    assert "no_resistance_reference" in rule["exclusions"]
    assert rule["tie_breaks"]["tier_priority"].endswith(
        "watch_zone > breakeven_extension_ladder"
    )
    reserve_trim = tiers["sell.breakeven_reserve_trim"]
    assert reserve_trim["conditions"]["anchor_operator"] == "max"
    assert reserve_trim["conditions"]["anchor_operands"] == [
        "average_cost_times_loss_guard",
        "d7_compliant_lowest_price",
    ]
    assert (
        reserve_trim["conditions"]["d7_min_expected_net_realized_gain_krw_policy_key"]
        == "sell.trim_min_expected_net_realized_gain_krw"
    )
    assert (
        reserve_trim["conditions"]["d7_scope_semantics"]
        == "one_share_net_realized_gain_not_total_trim"
    )
    assert (
        reserve_trim["conditions"]["d7_estimation_limit_semantics"]
        == "consumer_estimated_fees_and_taxes_required_no_fee_or_tax_model_added_by_this_tier"
    )
    assert reserve_trim["conditions"]["post_max_tick_snap_direction"] == "ceil"
    assert reserve_trim["conditions"]["regeneration"] == "daily_rep"
    assert (
        reserve_trim["conditions"]["submission_contract"]
        == "section_40_auto_approve_with_veto"
    )
    assert reserve_trim["conditions"]["watch_fallback"] == "anchor_uncomputable_only"
    assert reserve_trim["conditions"]["advisory"] is True
    assert reserve_trim["action"] == "preplace_resting_breakeven_reserve_trim"
    assert reserve_trim["sizing"] == "existing_trim_rule"
    assert rule["tie_breaks"]["tier_priority"].startswith(
        "de_minimis_trim_watch > sell.breakeven_reserve_trim >"
    )
    assert "single_share_position" not in rule["exclusions"]
    single_share = out["decision_rules"]["sell.single_share_exit"]
    assert single_share["activation_state"] == "shadow"
    assert single_share["proposal_enabled"] is False
    assert single_share["conditions"]["profit_pct_min"] == 8
    assert single_share["conditions"]["resistance_strength_min"] == "strong"
    assert single_share["conditions"]["resistance_distance_pct_min_exclusive"] == 6
    assert single_share["conditions"]["resistance_distance_pct_max"] == 15
    assert single_share["conditions"]["resistance_source_family_min"] == 2
    assert single_share["proposal"] == {
        "action": "full_exit_at_far_resistance",
        "sizing": "full_account_lot_exit",
        "approval": "telegram_manual",
        "auto_approve": False,
        "execution": "proposal_only",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["us", "crypto"])
async def test_get_trading_policy_hides_kr_single_share_exit_from_other_markets(
    market,
):
    out = await get_trading_policy(market=market, lane="sell")
    assert out["success"] is True
    assert "sell.single_share_exit" not in out["decision_rules"]


@pytest.mark.asyncio
async def test_get_trading_policy_returns_crash_day_advisory_with_version_echo():
    out = await get_trading_policy(market="kr", lane="buy")
    assert out["success"] is True
    assert out["crash_day"]["trigger"]["index_symbol"] == "069500"
    assert out["crash_day"]["trigger"]["index_gap_pct_max"] == -3.0
    assert out["crash_day"]["actions"]["new_entry_hold"] is True
    exception = out["crash_day"]["actions"]["new_entry_hold_exception"]
    assert exception["enabled"] is True
    assert exception["requires"] == {
        "standard_buy_gates": "all_pass_including_support_quality",
        "support_quality": "required",
        "price_zone": "strong_support",
        "gate_relaxation": "none",
    }
    assert exception["sizing"] == {
        "per_symbol_notional_multiplier": 0.5,
        "max_new_symbols": 1,
    }
    assert "즉석 판단 허용이 아니라" in exception["semantics"]
    assert "면제·완화·대체하지 않는다" in exception["semantics"]
    ladder = out["decision_rules"]["buy.preplanned_support_ladder"]
    assert ladder == {
        "semantics": (
            "Advisory only. A preplanned support ladder is retained only for a "
            "candidate that passes every standard buy gate; it does not relax, "
            "waive, replace, or bypass any gate."
        ),
        "enabled": True,
        "eligibility": "standard_buy_gates_pass",
        "rungs_max": 2,
        "per_rung_notional_multiplier": 0.5,
        "crash_day_behavior": "keep",
    }
    # advisory keys are echoed with the same version/content_hash stamp as
    # every other section of the response (ROB-932).
    assert out["version"] == "2026-08-26.3"
    assert out["content_hash"]


@pytest.mark.asyncio
async def test_get_trading_policy_returns_user_stances_advisory_with_version_echo():
    out = await get_trading_policy(market="kr", lane="buy")
    assert out["success"] is True
    stances = {s["id"]: s for s in out["user_stances"]}
    stance = stances["ai-demand-real-value-selective"]
    assert stance["review_date"] == "2026-10-17"
    # advisory keys are echoed with the same version/content_hash stamp as
    # every other section of the response (ROB-948, matching ROB-932).
    assert out["version"]
    assert out["content_hash"]


@pytest.mark.asyncio
async def test_get_trading_policy_returns_us_notional_usd_range_with_one_share_exception():
    out = await get_trading_policy(market="us", lane="buy")
    assert out["success"] is True
    us_range = out["thresholds"]["buy.per_symbol_notional_usd_range"]
    assert us_range["value"] == [150, 450]
    # §139차 — 700 -> 10000; the effective boundary is cash plus the USD 1,500
    # per-order auto-approve cap, not this number.
    assert us_range["one_share_exception"]["absolute_ceiling_usd"] == 10000
    assert us_range["one_share_exception"]["max_deep_rungs"] == 1


@pytest.mark.asyncio
async def test_get_trading_policy_unknown_key_explicit_error():
    out = await get_trading_policy(market="jp", lane="buy")
    assert out["success"] is False
    assert out["error"] == "unknown_key"
    assert "jp" in out["detail"]


def test_tool_registered_in_default_profile():
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert "get_trading_policy" in mcp.tools


def test_tool_registered_in_crypto_profile():
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.CRYPTO)
    assert "get_trading_policy" in mcp.tools
