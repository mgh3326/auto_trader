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
    assert set(out["decision_rules"]) == {"buy.support_reserve_net"}
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
    # advisory keys are echoed with the same version/content_hash stamp as
    # every other section of the response (ROB-932).
    assert out["version"]
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
    assert us_range["one_share_exception"]["absolute_ceiling_usd"] == 700
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
