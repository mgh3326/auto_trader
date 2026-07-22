# tests/test_route_request_lanes.py
from __future__ import annotations

from app.mcp_server.tooling import route_request_lanes as L


def _fake_thresholds(market: str, lane: str, *, empty: bool = False) -> dict:
    return {
        "market": market,
        "lane": lane,
        "version": "V",
        "content_hash": "H",
        "thresholds": {} if empty else {"screen.rsi_max": {"value": 45}},
    }


_VERSION = {"version": "V", "content_hash": "H"}
_ALL = set(L.ALL_KNOWN_TOOLS)


def test_intent_to_lane_covers_all_four_intents():
    assert set(L.INTENT_TO_LANE) == {
        "buy_analysis",
        "profit_taking",
        "discovery",
        "market_brief",
    }
    assert L.INTENT_TO_LANE["market_brief"] == "bootstrap"


def test_buckets_partition_and_are_disjoint():
    assert L.READ_ONLY_ADVISORY_TOOLS.isdisjoint(L.MUTATION_TOOLS)
    assert L.ALL_KNOWN_TOOLS == L.READ_ONLY_ADVISORY_TOOLS | L.MUTATION_TOOLS
    assert "route_request" in L.READ_ONLY_ADVISORY_TOOLS


def test_build_route_plan_buy_shape_is_deterministic():
    plan_a = L.build_route_plan(
        "buy_analysis",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    plan_b = L.build_route_plan(
        "buy_analysis",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    assert plan_a == plan_b
    assert plan_a["success"] is True
    assert plan_a["lane"] == "buy"
    assert plan_a["market"] == "kr"
    assert plan_a["policy_version"] == _VERSION
    steps = [s["tool"] for s in plan_a["standard_tool_sequence"]]
    assert steps[0] == "get_operating_briefing"
    assert "toss_place_order" in steps
    # step numbers are contiguous 1..n
    assert [s["step"] for s in plan_a["standard_tool_sequence"]] == list(
        range(1, len(steps) + 1)
    )


def test_blocked_actions_excludes_lanes_own_mutation_tools():
    plan = L.build_route_plan(
        "buy_analysis",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    # buy lane's own place tools are allowed, not blocked
    assert "toss_place_order" not in plan["blocked_actions"]
    assert "kis_live_place_order" not in plan["blocked_actions"]
    # a non-buy mutation tool is blocked
    assert "toss_cancel_order" in plan["blocked_actions"]
    assert "toss_place_order" in plan["allowed_tools"]


def test_market_brief_blocks_all_mutation_and_has_empty_thresholds():
    plan = L.build_route_plan(
        "market_brief",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "bootstrap", empty=True),
        policy_version=_VERSION,
    )
    assert plan["lane"] == "bootstrap"
    assert plan["verdict_thresholds"]["thresholds"] == {}
    # bootstrap sequence is all read-only -> every mutation tool is blocked
    assert set(plan["blocked_actions"]) == (L.MUTATION_TOOLS & _ALL)


def test_profile_intersection_drops_unregistered_tools():
    registered = _ALL - {"toss_place_order"}
    plan = L.build_route_plan(
        "discovery",
        "kr",
        registered_tools=registered,
        verdict_thresholds=_fake_thresholds("kr", "discovery"),
        policy_version=_VERSION,
    )
    tools = [s["tool"] for s in plan["standard_tool_sequence"]]
    assert "toss_place_order" not in tools
    assert "toss_place_order" not in plan["allowed_tools"]
    assert "toss_place_order" not in plan["blocked_actions"]


# --- ROB-658: market-aware execution tool mapping -----------------------------

# crypto/US profiles register the generic place_order surface but not the
# KR-centric toss/kis execution tools.
_CRYPTO_MUTATION = {
    "place_order",
    "modify_order",
    "cancel_order",
    "buy_ladder_fill_preview",
    "sell_ladder_fill_preview",
    "get_order_history",
    "live_reconcile_orders",
    "kis_mock_reconciliation_run",
}
_CRYPTO_REGISTERED = set(L.READ_ONLY_ADVISORY_TOOLS) | _CRYPTO_MUTATION


def test_crypto_buy_does_not_block_generic_place_order():
    plan = L.build_route_plan(
        "buy_analysis",
        "crypto",
        registered_tools=_CRYPTO_REGISTERED,
        verdict_thresholds=_fake_thresholds("crypto", "buy"),
        policy_version=_VERSION,
    )
    # the real crypto execution tool must not be advised as blocked (ROB-658)
    assert "place_order" not in plan["blocked_actions"]
    # ...and it is surfaced as an allowed tool + a concrete execution step
    assert "place_order" in plan["allowed_tools"]
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    assert "place_order" in steps
    # KR execution tools are unregistered on crypto -> dropped, never injected
    assert "toss_place_order" not in steps
    assert "kis_live_place_order" not in steps
    # other generic mutations remain blocked (only place is the sanctioned step)
    assert "modify_order" in plan["blocked_actions"]
    assert "cancel_order" in plan["blocked_actions"]
    # step numbers stay contiguous after injection
    assert [s["step"] for s in plan["standard_tool_sequence"]] == list(
        range(1, len(steps) + 1)
    )


def test_crypto_sell_and_discovery_surface_generic_place_order():
    for intent in ("profit_taking", "discovery"):
        plan = L.build_route_plan(
            intent,
            "crypto",
            registered_tools=_CRYPTO_REGISTERED,
            verdict_thresholds=_fake_thresholds("crypto", L.INTENT_TO_LANE[intent]),
            policy_version=_VERSION,
        )
        assert "place_order" not in plan["blocked_actions"]
        assert "place_order" in plan["allowed_tools"]
        assert "place_order" in [s["tool"] for s in plan["standard_tool_sequence"]]


def test_crypto_market_brief_still_blocks_all_mutation():
    # bootstrap has no execution step -> even crypto's place_order stays blocked
    plan = L.build_route_plan(
        "market_brief",
        "crypto",
        registered_tools=_CRYPTO_REGISTERED,
        verdict_thresholds=_fake_thresholds("crypto", "bootstrap", empty=True),
        policy_version=_VERSION,
    )
    assert "place_order" in plan["blocked_actions"]
    assert set(plan["blocked_actions"]) == (L.MUTATION_TOOLS & _CRYPTO_REGISTERED)


def test_kr_buy_still_blocks_generic_place_order_no_regression():
    # KR routes execution through toss/kis; the generic place_order stays blocked.
    plan = L.build_route_plan(
        "buy_analysis",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    assert "place_order" in plan["blocked_actions"]
    assert "place_order" not in plan["allowed_tools"]
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    assert "place_order" not in steps
    # KR execution tools are still the sanctioned, non-blocked path
    assert "toss_place_order" not in plan["blocked_actions"]


def test_hard_constraints_reference_policy_keys_not_numbers():
    plan = L.build_route_plan(
        "buy_analysis",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    joined = " ".join(plan["hard_constraints"])
    assert "sell.loss_guard_min_multiple" in joined
    assert "1.01" not in joined


def test_buy_discovery_have_negative_class_constraint():
    # ROB-712 — buy + discovery lanes must encode the negative-class recording
    # convention: deferred_no_action items keep confidence + a resolvable
    # forecast so calibration isn't censored.
    for lane in ("buy", "discovery"):
        joined = " ".join(L.HARD_CONSTRAINTS[lane]).lower()
        assert "deferred_no_action" in joined
        assert "confidence" in joined
        assert "forecast" in joined


def test_buy_discovery_end_with_missed_opportunity_session_hook():
    for lane in ("buy", "discovery"):
        assert L.LANE_SEQUENCES[lane][-1]["tool"] == "missed_opportunity_save"
        joined = " ".join(L.HARD_CONSTRAINTS[lane]).lower()
        assert "2%" in joined
        assert "zero new buys" in joined
        assert "d+5" in joined


# --- ROB-660: sell lane account routing ---------------------------------------


def test_sell_lane_surfaces_kis_place_and_toss_cancel_as_steps():
    plan = L.build_route_plan(
        "profit_taking",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "sell"),
        policy_version=_VERSION,
    )
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    for tool in ("toss_cancel_order", "kis_live_place_order"):
        assert tool in steps, tool
        assert tool in plan["allowed_tools"], tool
        assert tool not in plan["blocked_actions"], tool
    # step numbers stay contiguous 1..n after the two inserts
    assert [s["step"] for s in plan["standard_tool_sequence"]] == list(
        range(1, len(steps) + 1)
    )


def test_sell_lane_history_helpers_allowed_but_not_sequenced():
    plan = L.build_route_plan(
        "profit_taking",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "sell"),
        policy_version=_VERSION,
    )
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    for tool in ("kis_live_get_order_history", "toss_get_order_history"):
        assert tool in plan["allowed_tools"], tool
        assert tool not in plan["blocked_actions"], tool
        assert tool not in steps, tool


def test_buy_lane_history_helpers_allowed_but_not_sequenced():
    # ROB-666: buy lane needs the order-status helpers to confirm a buy fill and
    # to check KIS regular-session survival after 15:30 (ROB-657 rule). Symmetric
    # to the sell-lane allowance (ROB-660): allowed, never in the ordered sequence.
    plan = L.build_route_plan(
        "buy_analysis",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    for tool in ("kis_live_get_order_history", "toss_get_order_history"):
        assert tool in plan["allowed_tools"], tool
        assert tool not in plan["blocked_actions"], tool
        assert tool not in steps, tool


def test_sell_lane_routing_does_not_leak_into_buy_lane():
    buy = L.build_route_plan(
        "buy_analysis",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    # sell-lane-only execution tools remain blocked in the buy lane (no cross-lane
    # leak). The order-history helpers are now allowed in both lanes (ROB-666), so
    # they are asserted separately in test_buy_lane_history_helpers_allowed_*.
    assert "toss_cancel_order" in buy["blocked_actions"]


def test_sell_lane_new_kr_tools_dropped_when_unregistered_on_crypto():
    plan = L.build_route_plan(
        "profit_taking",
        "crypto",
        registered_tools=_CRYPTO_REGISTERED,
        verdict_thresholds=_fake_thresholds("crypto", "sell"),
        policy_version=_VERSION,
    )
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    assert "toss_cancel_order" not in steps
    assert "kis_live_place_order" not in steps
    assert "toss_cancel_order" not in plan["allowed_tools"]
    # ROB-658 generic execution injection still fires on crypto sell
    assert "place_order" in steps
    assert "place_order" not in plan["blocked_actions"]


def test_sell_lane_hard_constraints_document_routing_and_cancel_first():
    plan = L.build_route_plan(
        "profit_taking",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "sell"),
        policy_version=_VERSION,
    )
    joined = " ".join(plan["hard_constraints"])
    assert "holding account" in joined
    assert "kis_live_place_order" in joined
    assert "toss_cancel_order" in joined
