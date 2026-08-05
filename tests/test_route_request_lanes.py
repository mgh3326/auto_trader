from __future__ import annotations

from itertools import combinations

import pytest

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
_PROPOSAL_FIELDS = {
    "version",
    "state",
    "execution_mode",
    "execution_ready",
    "proposal_tool",
    "approval_channel",
    "human_approval_required",
    "preview_owner",
    "reconcile_requirement",
    "required_tools",
    "missing_required_tools",
}
_EXPECTED_LANES = {
    "buy_analysis": ("buy", "proposal_led"),
    "profit_taking": ("sell", "proposal_led"),
    "discovery": ("discovery", "legacy_direct"),
    "market_brief": ("bootstrap", "read_only"),
}


def _plan(
    intent: str,
    market: str,
    *,
    registered: set[str] | None = None,
    purpose: str | None = None,
) -> dict:
    lane = L.INTENT_TO_LANE[intent]
    return L.build_route_plan(
        intent,
        market,
        registered_tools=_ALL if registered is None else registered,
        verdict_thresholds=_fake_thresholds(market, lane, empty=lane == "bootstrap"),
        policy_version=_VERSION,
        purpose=purpose,
    )


def _step_tools(plan: dict) -> list[str]:
    return [step["tool"] for step in plan["standard_tool_sequence"]]


def test_intent_to_lane_covers_all_four_intents():
    assert L.INTENT_TO_LANE == {
        "buy_analysis": "buy",
        "profit_taking": "sell",
        "discovery": "discovery",
        "market_brief": "bootstrap",
    }


def test_action_taxonomy_is_disjoint_and_total():
    action_classes = (
        L.DIRECT_BROKER_MUTATION_TOOLS,
        L.PROPOSAL_LED_TOOLS,
        L.PROPOSAL_LIFECYCLE_TOOLS,
        L.PREVIEW_REVALIDATION_TOOLS,
        L.RECONCILE_TOOLS,
        L.STATUS_HELPER_TOOLS,
    )
    for left, right in combinations(action_classes, 2):
        assert left.isdisjoint(right)
    assert frozenset().union(*action_classes) == L.MUTATION_TOOLS
    assert L.READ_ONLY_ADVISORY_TOOLS.isdisjoint(L.MUTATION_TOOLS)
    assert L.ALL_KNOWN_TOOLS == L.READ_ONLY_ADVISORY_TOOLS | L.MUTATION_TOOLS
    assert L.ORDER_PROPOSAL_READ_TOOLS <= L.READ_ONLY_ADVISORY_TOOLS
    assert L.PROPOSAL_LED_TOOLS == {"order_proposal_create"}


def test_account_cleanup_route_allows_only_preflighted_alpaca_submit():
    plan = _plan(
        "profit_taking",
        "us",
        purpose=L.ACCOUNT_CLEANUP_PURPOSE,
    )
    direct = L.ACCOUNT_CLEANUP_DIRECT_TOOL

    assert plan["success"] is True
    assert plan["purpose"] == L.ACCOUNT_CLEANUP_PURPOSE
    assert plan["route_contract"]["version"] == "cleanup-reduce-only-v1"
    assert plan["route_contract"]["execution_mode"] == "cleanup_reduce_only"
    assert plan["route_contract"]["required_tools"] == sorted(
        L.ACCOUNT_CLEANUP_REQUIRED_TOOLS
    )
    assert _step_tools(plan) == [step["tool"] for step in L.ACCOUNT_CLEANUP_SEQUENCE]
    assert direct in plan["allowed_tools"]
    assert direct not in plan["blocked_actions"]
    assert (L.DIRECT_BROKER_MUTATION_TOOLS - {direct}) & _ALL <= set(
        plan["blocked_actions"]
    )


def test_account_cleanup_route_fails_closed_without_preflight_tool():
    registered = _ALL - {"alpaca_paper_execution_preflight_check"}
    plan = _plan(
        "profit_taking",
        "us",
        registered=registered,
        purpose=L.ACCOUNT_CLEANUP_PURPOSE,
    )

    assert plan["success"] is False
    assert plan["error"] == "required_route_tool_unavailable"
    assert (
        "alpaca_paper_execution_preflight_check"
        in plan["route_contract"]["missing_required_tools"]
    )
    assert L.ACCOUNT_CLEANUP_DIRECT_TOOL not in plan["allowed_tools"]
    assert L.ACCOUNT_CLEANUP_DIRECT_TOOL in plan["blocked_actions"]
    assert L.ACCOUNT_CLEANUP_DIRECT_TOOL not in _step_tools(plan)


@pytest.mark.parametrize(
    ("intent", "expected_lane", "execution_mode"),
    [
        (intent, lane, execution_mode)
        for intent, (lane, execution_mode) in _EXPECTED_LANES.items()
    ],
)
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
def test_route_semantic_contract_matrix(
    intent: str,
    expected_lane: str,
    execution_mode: str,
    market: str,
):
    plan = _plan(intent, market)
    sequence = plan["standard_tool_sequence"]
    steps = _step_tools(plan)
    contract = plan["route_contract"]

    assert plan["success"] is True
    assert plan["degraded"] is False
    assert plan["intent"] == intent
    assert plan["lane"] == expected_lane
    assert plan["market"] == market
    assert plan["policy_version"] == _VERSION
    assert set(contract) == _PROPOSAL_FIELDS
    assert contract["version"] == "proposal-led-v1"
    assert contract["state"] == "ready"
    assert contract["execution_mode"] == execution_mode
    assert contract["execution_ready"] is True
    assert [step["step"] for step in sequence] == list(range(1, len(steps) + 1))
    assert steps == L.ordered_lane_tool_names(expected_lane)
    assert len(steps) == len(set(steps))
    assert set(plan["allowed_tools"]).isdisjoint(plan["blocked_actions"])
    assert plan["blocked_actions_basis"] == "live_registered_surface"

    registered_direct = L.DIRECT_BROKER_MUTATION_TOOLS & _ALL
    if expected_lane in {"buy", "sell"}:
        assert contract == {
            "version": "proposal-led-v1",
            "state": "ready",
            "execution_mode": "proposal_led",
            "execution_ready": True,
            "proposal_tool": "order_proposal_create",
            "approval_channel": "telegram",
            "human_approval_required": True,
            "preview_owner": "proposal_revalidation",
            "reconcile_requirement": "broker_evidence",
            "required_tools": ["order_proposal_create"],
            "missing_required_tools": [],
        }
        assert steps.count("order_proposal_create") == 1
        assert steps[-1] == "order_proposal_create"
        assert "order_proposal_create" in plan["allowed_tools"]
        assert registered_direct <= set(plan["blocked_actions"])
        assert registered_direct.isdisjoint(plan["allowed_tools"])
        assert registered_direct.isdisjoint(steps)
    elif expected_lane == "discovery":
        assert contract["proposal_tool"] is None
        assert contract["approval_channel"] == "not_applicable"
        assert contract["human_approval_required"] is False
        assert contract["preview_owner"] == "lane_operator"
        assert contract["reconcile_requirement"] == "legacy_unspecified"
        assert contract["required_tools"] == []
        assert contract["missing_required_tools"] == []
        # Operator decision: discovery is explicitly out of ROB-1045 scope.
        assert "toss_place_order" in steps
        assert "toss_place_order" in plan["allowed_tools"]
        assert "toss_place_order" not in plan["blocked_actions"]
    else:
        assert contract["proposal_tool"] is None
        assert contract["approval_channel"] == "not_applicable"
        assert contract["human_approval_required"] is False
        assert contract["preview_owner"] == "not_applicable"
        assert contract["reconcile_requirement"] == "not_applicable"
        assert contract["required_tools"] == []
        assert contract["missing_required_tools"] == []
        assert set(plan["blocked_actions"]) == (L.MUTATION_TOOLS & _ALL)


@pytest.mark.parametrize("intent", ["buy_analysis", "profit_taking"])
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
def test_proposal_tool_missing_fails_closed(
    intent: str,
    market: str,
):
    registered = _ALL - {"order_proposal_create"}
    plan = _plan(intent, market, registered=registered)
    steps = _step_tools(plan)

    assert plan["success"] is False
    assert plan["error"] == "required_route_tool_unavailable"
    assert plan["degraded"] is True
    assert plan["route_contract"]["state"] == "degraded"
    assert plan["route_contract"]["execution_ready"] is False
    assert plan["route_contract"]["required_tools"] == ["order_proposal_create"]
    assert plan["route_contract"]["missing_required_tools"] == ["order_proposal_create"]
    assert "order_proposal_create" not in steps
    assert L.DIRECT_BROKER_MUTATION_TOOLS.isdisjoint(steps)
    assert L.DIRECT_BROKER_MUTATION_TOOLS & registered <= set(plan["blocked_actions"])
    assert L.DIRECT_BROKER_MUTATION_TOOLS.isdisjoint(plan["allowed_tools"])
    assert "place_order" not in steps
    assert plan["blocked_actions_basis"] == "live_registered_surface"


@pytest.mark.parametrize("intent", ["buy_analysis", "profit_taking"])
def test_proposal_lane_allows_reconcile_helpers_without_sequencing(intent: str):
    plan = _plan(intent, "kr")
    allowed = set(plan["allowed_tools"])
    blocked = set(plan["blocked_actions"])
    steps = set(_step_tools(plan))

    assert L.RECONCILE_TOOLS & _ALL <= allowed
    assert (L.RECONCILE_TOOLS & _ALL).isdisjoint(blocked)
    assert L.RECONCILE_TOOLS.isdisjoint(steps)


def test_sell_fill_safety_preview_precedes_proposal():
    plan = _plan("profit_taking", "kr")
    steps = _step_tools(plan)

    assert steps[-2:] == ["sell_ladder_fill_preview", "order_proposal_create"]
    assert "sell_ladder_fill_preview" in plan["allowed_tools"]
    assert "sell_ladder_fill_preview" not in plan["blocked_actions"]


@pytest.mark.parametrize("intent", ["buy_analysis", "profit_taking"])
def test_broker_preview_is_not_an_operator_step_for_proposal_lanes(intent: str):
    plan = _plan(intent, "kr")

    assert "toss_preview_order" not in _step_tools(plan)
    assert "toss_preview_order" not in plan["allowed_tools"]
    assert "toss_preview_order" in plan["blocked_actions"]


def test_profile_intersection_drops_unregistered_discovery_tool():
    registered = _ALL - {"toss_place_order"}
    plan = _plan("discovery", "kr", registered=registered)

    assert "toss_place_order" not in _step_tools(plan)
    assert "toss_place_order" not in plan["allowed_tools"]
    assert "toss_place_order" not in plan["blocked_actions"]


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


def test_crypto_discovery_keeps_legacy_generic_place_order_injection():
    plan = _plan("discovery", "crypto", registered=_CRYPTO_REGISTERED)
    steps = _step_tools(plan)

    assert plan["success"] is True
    assert plan["route_contract"]["execution_mode"] == "legacy_direct"
    assert "place_order" in steps
    assert "place_order" in plan["allowed_tools"]
    assert "place_order" not in plan["blocked_actions"]
    assert "toss_place_order" not in steps


@pytest.mark.parametrize("intent", ["buy_analysis", "profit_taking"])
def test_crypto_proposal_lanes_never_restore_generic_place_order(intent: str):
    registered = _CRYPTO_REGISTERED | {"order_proposal_create"}
    plan = _plan(intent, "crypto", registered=registered)

    assert plan["success"] is True
    assert "place_order" not in _step_tools(plan)
    assert "place_order" not in plan["allowed_tools"]
    assert "place_order" in plan["blocked_actions"]


def test_hard_constraints_reference_policy_and_proposal_contract():
    buy = " ".join(_plan("buy_analysis", "kr")["hard_constraints"])
    sell = " ".join(_plan("profit_taking", "kr")["hard_constraints"])

    assert "sell.loss_guard_min_multiple" in buy
    assert "1.01" not in buy
    for joined in (buy, sell):
        assert "order_proposal_create" in joined
        assert "Telegram human approval" in joined
        assert "broker evidence" in joined
        assert "toss_place_order" not in joined
        assert "kis_live_place_order" not in joined
        assert "toss_cancel_order" not in joined


def test_buy_discovery_have_negative_class_constraint():
    for lane in ("buy", "discovery"):
        joined = " ".join(L.HARD_CONSTRAINTS[lane]).lower()
        assert "deferred_no_action" in joined
        assert "confidence" in joined
        assert "forecast" in joined
        assert "outcome_rule_version='window-touch-v1-high-gte-low-lte'" in joined
