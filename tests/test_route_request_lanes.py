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
        "buy_analysis", "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    plan_b = L.build_route_plan(
        "buy_analysis", "kr",
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
        "buy_analysis", "kr",
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
        "market_brief", "kr",
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
        "discovery", "kr",
        registered_tools=registered,
        verdict_thresholds=_fake_thresholds("kr", "discovery"),
        policy_version=_VERSION,
    )
    tools = [s["tool"] for s in plan["standard_tool_sequence"]]
    assert "toss_place_order" not in tools
    assert "toss_place_order" not in plan["allowed_tools"]
    assert "toss_place_order" not in plan["blocked_actions"]


def test_hard_constraints_reference_policy_keys_not_numbers():
    plan = L.build_route_plan(
        "buy_analysis", "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    joined = " ".join(plan["hard_constraints"])
    assert "sell.loss_guard_min_multiple" in joined
    assert "1.01" not in joined
