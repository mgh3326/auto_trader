import re
from copy import deepcopy
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import app.schemas.trading_policy as policy_schema
from app.mcp_server.tick_size import get_tick_size_kr
from app.schemas.trading_policy import (
    CrashDayNewEntryHoldException,
    PreplannedSupportLadderPolicy,
    SupportReserveNetDecisionRule,
    TradingPolicyDocument,
)
from app.services.order_proposals.auto_approve import _VETO_CAPABLE_ACCOUNT_MARKETS
from app.services.trading_policy_service import load_trading_policy

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"
_ROB1289_BASELINE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "trading_policy_rob1289_baseline.yaml"
)
_PLAYBOOK = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "playbooks"
    / "trading-decision-playbook.md"
)

_ROB1298_ADDITIVE_TIE_BREAK_KEYS = (
    "breakeven_extension_fallback_order",
    "breakeven_extension_exclusion_retained",
    "breakeven_extension_minimum_benefit",
)

_ROB1292_ALLOWED_POLICY_DELTAS = (
    ("order_proposals.auto_approve.per_order_cap.kr", 400000, 1000000),
    ("order_proposals.auto_approve.per_order_cap.us", 800, 1500),
    ("order_proposals.auto_approve.per_order_cap.crypto", 100000, 1000000),
    ("order_proposals.auto_approve.daily_cap.kr", 400000, 5000000),
    ("order_proposals.auto_approve.daily_cap.us", 5000, 20000),
    ("order_proposals.auto_approve.daily_cap.crypto", 300000, 5000000),
)


def _raw() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _policy_path_get(payload: dict, path: str):
    value = payload
    for key in path.split("."):
        value = value[key]
    return value


def _policy_path_set(payload: dict, path: str, value) -> None:
    keys = path.split(".")
    target = payload
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value


def _breakeven_reserve_trim_tier():
    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "sell.trim_preplace"
    ]
    return next(tier for tier in rule.tiers if tier.id == "sell.breakeven_reserve_trim")


def _threshold_decimal(doc: TradingPolicyDocument, key: str) -> Decimal:
    return Decimal(str(doc.thresholds[key].value))


def _sell_lane_machine_block() -> dict:
    text = _PLAYBOOK.read_text(encoding="utf-8")
    for block in re.findall(r"```yaml\n(.*?)```", text, re.DOTALL):
        if "# playbook-machine-readable: sell lane" in block:
            parsed = yaml.safe_load(block)
            return parsed["lanes"]["sell"]
    raise AssertionError("sell lane machine-readable block missing")


def _breakeven_reserve_trim_anchor(
    tier,
    *,
    average_cost: Decimal,
    d7_compliant_lowest_price: Decimal,
) -> Decimal:
    """Test-only interpreter for the declarative advisory anchor contract."""

    conditions = tier.conditions
    guard = _threshold_decimal(
        TradingPolicyDocument.model_validate(_raw()),
        str(conditions["anchor_guard_policy_key"]),
    )
    operands = {
        "average_cost_times_loss_guard": average_cost * guard,
        "d7_compliant_lowest_price": d7_compliant_lowest_price,
    }
    resolved = [operands[str(name)] for name in conditions["anchor_operands"]]
    operator = conditions["anchor_operator"]
    if operator == "max":
        return max(resolved)
    if operator == "min":
        return min(resolved)
    raise AssertionError(f"unsupported advisory anchor operator: {operator!r}")


def _breakeven_reserve_trim_post_max_tick_snap(
    tier,
    *,
    anchor: Decimal,
    tick_size: Decimal,
) -> Decimal:
    """Test-only interpreter for the declarative post-max tick direction."""

    direction = tier.conditions["post_max_tick_snap_direction"]
    rounding = {"ceil": ROUND_CEILING, "floor": ROUND_FLOOR}.get(direction)
    if rounding is None:
        raise AssertionError(f"unsupported post-max tick direction: {direction!r}")
    return (anchor / tick_size).to_integral_value(rounding=rounding) * tick_size


def _breakeven_reserve_trim_triggered(
    tier,
    *,
    pnl_pct: Decimal,
    current_price_multiple: Decimal,
) -> bool:
    """Test-only interpreter for the declarative pre-guard trigger band."""

    doc = TradingPolicyDocument.model_validate(_raw())
    conditions = tier.conditions
    lower_bound = -_threshold_decimal(
        doc,
        str(conditions["lot_pnl_pct_min_inclusive_negated_policy_key"]),
    )
    guard = _threshold_decimal(
        doc,
        str(conditions["lot_pre_guard_average_cost_multiple_max_exclusive_policy_key"]),
    )
    return pnl_pct >= lower_bound and current_price_multiple < guard


def test_shipped_config_validates():
    doc = TradingPolicyDocument.model_validate(_raw())
    assert doc.version == load_trading_policy().version
    assert doc.version == "2026-08-20.2"
    # verbatim seed values from the playbook policy_keys
    assert doc.thresholds["portfolio.sector_cluster_cap_pct"].value == 10
    assert doc.thresholds["sell.loss_guard_min_multiple"].value == 1.01
    assert doc.thresholds["screen.rsi_max"].value == 45
    assert doc.thresholds["buy.deep_limit_pct_range"].value == [-12, -3]
    assert doc.thresholds["portfolio.max_symbols_per_theme"].value == 2
    assert doc.thresholds["sell.momentum_spike_change_pct_min"].value == 10
    assert doc.thresholds["sell.single_share_profit_pct_min"].value == 8
    assert doc.thresholds["sell.trim_min_expected_net_realized_gain_krw"].value == 5000
    for key in (
        "sell.momentum_spike_change_pct_min",
        "sell.single_share_profit_pct_min",
        "sell.trim_min_expected_net_realized_gain_krw",
    ):
        semantics = doc.thresholds[key].semantics
        assert "측정으로 확정할 가설" in semantics
        assert "not adjustable by a runtime session" in semantics
    assert (
        "posture shadow completion"
        in doc.thresholds["sell.momentum_spike_change_pct_min"].semantics
    )
    assert set(doc.market_overrides.keys()) == {"kr", "us", "crypto"}
    assert "semis_memory" in doc.sector_clusters
    assert "sell.trim_preplace" in doc.decision_rules
    assert doc.posture.enabled is False
    assert doc.posture.mode == "shadow"
    assert doc.posture.states == [
        "RESTING",
        "CONDITIONAL",
        "ARMED_DEFERRED",
        "DISARMED",
        "EXPIRED_REARMABLE",
    ]
    assert doc.posture.policy_stamp_required is True
    trim_rule = doc.decision_rules["sell.trim_preplace"]
    assert trim_rule.lanes == ["sell"]
    assert [tier.id for tier in trim_rule.tiers] == [
        "de_minimis_trim_watch",
        "sell.breakeven_reserve_trim",
        "single_share_full_exit_review",
        "momentum_spike_profit_ladder",
        "rsi_confirmed_resistance",
        "ultra_near_resistance",
        "watch_zone",
        "breakeven_extension_ladder",
    ]
    assert trim_rule.tiers[0].action == "register_watch_instead_of_trim"
    assert trim_rule.tiers[1].sizing == "existing_trim_rule"
    assert trim_rule.tiers[2].sizing == "full_position"
    assert trim_rule.tiers[3].conditions["rsi_gate_exempt"] is True
    assert trim_rule.tiers[3].conditions["ladder_total_position_pct_max"] == 33.3333
    assert trim_rule.tiers[5].conditions["resistance_near_pct_max"] == 2
    assert trim_rule.tie_breaks["multiple_tiers_matched"] == (
        "first_matching_tier_wins"
    )
    assert trim_rule.tie_breaks["sell.upside_place_max_pct"] == "size_limit_only"
    assert "single_share_position" not in trim_rule.exclusions


def test_support_reserve_net_literal_policy_blocks_are_frozen():
    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "buy.support_reserve_net"
    ]

    assert isinstance(rule, SupportReserveNetDecisionRule)
    assert rule.lanes == ["buy"]

    # §Q1 literal: reserve-net is an RSI-only exception, never a wider gate.
    assert rule.regular_discovery_precedence is True
    assert rule.eligible_only_when_regular_gate_failure == "RSI_ONLY"
    assert rule.rsi_gate == "omitted_for_this_tier_only"
    assert rule.support_strength_min == "moderate"
    assert rule.independent_support_source_count_min == 2
    assert rule.independent_support_source_families == [
        "fib",
        "bb_lower",
        "volume_profile",
    ]
    assert rule.support_within_current_pct_max == 8
    assert rule.honest_upside_pct_min == 40
    assert rule.honest_upside_reference == "decision_time_current_price"
    assert rule.discount_below_support_pct_range == [5, 10]
    assert rule.final_limit_distance_from_current_pct_range == [-15, -5]
    assert rule.anchor_price_formula == "tick_floor(S × (1-d))"
    assert rule.final_limit_distance_out_of_range == "EXCLUDE"
    assert rule.order_type == "limit"
    assert rule.tif == "DAY"

    # §Q2 literal plus its accounting contract.
    assert rule.all_pending_buy_required_cash_hard_cap_pct == 90
    assert rule.tier_armed_required_cash_cap_pct == 50
    assert rule.max_owned_or_open_symbols_per_market == 2
    assert rule.max_active_orders_per_symbol == 1
    assert rule.max_symbols_per_sector_cluster == 1
    assert rule.unknown_sector == "INELIGIBLE"
    assert rule.auto_submit_notional.krw == 200000
    assert rule.auto_submit_notional.usd == 150
    assert rule.larger_notional_within_existing_band == "HUMAN_APPROVAL_REQUIRED"
    assert rule.daily_auto_cap_includes_all_buy_tiers is True
    assert rule.cash_reservation.net_orderable == (
        "fresh_broker_orderable_cash_minus_same_account_currency_pending_required_cash"
    )
    assert rule.cash_reservation.pending_required_cash_scope == "not_yet_reached_broker"
    assert rule.cash_reservation.required_cash_primary == (
        "preview_estimated_value_plus_fee"
    )
    assert rule.cash_reservation.required_cash_fallback == "quantity_times_limit_price"
    assert rule.cash_reservation.broker_orderable_unavailable_or_error == "FAIL_CLOSED"
    assert rule.cash_reservation.cancel_proposal_cash_reservation == (
        "KEEP_RESERVED_UNTIL_BROKER_TERMINAL_CONFIRMATION"
    )

    # §Q3 literal: the read-only triage consumer cannot release or rearm.
    triage = rule.fill_triage
    assert triage.on_first_confirmed_fill == "FREEZE_NEW_SUBMITS"
    assert triage.cancellation_mode == "PROPOSAL_REQUIRES_APPROVAL"
    assert triage.broker_cancel_confirmation_required_before_releasing_cash is True
    assert triage.same_session_rearm is False
    assert triage.unknown_or_ambiguous_order_state == "KEEP_RESERVED_AND_BLOCK"
    assert triage.burst_key == ["broker_account_id", "currency", "market_session"]

    # §Q4 literal: an add is full A_limit feasibility, not an undersized buy.
    add = rule.add_candidate
    assert add.r931_review_required == "PASS"
    assert add.r931_review_max_age_days == 7
    assert add.policy_table_max_age_hours == 36
    assert add.k_used == 0.10
    assert add.sizing_price == "proposed_limit_price"
    assert add.a_limit_lte_zero == "NO_ORDER"
    assert add.partial_A_limit_fill == "FORBIDDEN"
    assert add.max_add_symbols_per_market == 1
    assert add.max_reserve_net_add_fills_per_symbol_per_policy_version == 1
    assert add.same_day_rearm_after_fill is False
    assert add.crash_day_averaging_exemption is False

    # §Q2 lines 82–87: constrained cash is assigned in this exact order.
    priority = rule.priority_rules
    assert priority.allocation_order == [
        "dedupe_active_or_resting_same_symbol",
        "first_slot_eligible_new_candidate",
        "add_secondary_pool_only_after_r931_pass_and_full_a_limit_10",
    ]
    assert priority.same_symbol_active_or_resting == "DEDUPE_FIRST"
    assert priority.first_slot == "ELIGIBLE_NEW_CANDIDATE_FIRST"
    assert priority.add_candidate_rank == "SECONDARY_CANDIDATE_POOL"
    assert priority.add_candidate_r931_review_required == "PASS"
    assert priority.add_candidate_a_limit_10 == "FULLY_SATISFIED"
    assert priority.max_add_symbols_per_market == 1
    assert priority.same_intent_class_sort_order == [
        "support_strength_desc",
        "independent_support_source_count_desc",
        "honest_upside_pct_desc",
        "post_fill_sector_increase_asc",
        "required_cash_asc",
    ]
    assert priority.exact_tie_break == "NEW_BEFORE_ADD"

    prohibitions = rule.prohibitions
    assert prohibitions.no_new_add_or_deep_limit_rung_overlap is True
    assert prohibitions.aggregate_active_buy_by_beneficial_owner_across_accounts is True
    assert prohibitions.fresh_cost_basis_quantity_and_A_limit_before_next_day_reissue
    assert prohibitions.partial_A_limit_fill == "FORBIDDEN"
    assert prohibitions.candidate_zero_runtime_gate_relaxation == "FORBIDDEN"
    assert prohibitions.crash_day_averaging_exemption is False
    assert prohibitions.cancel_proposal_is_not_broker_cancellation is True
    assert prohibitions.unconfirmed_cancel_keeps_required_cash_reserved is True
    assert prohibitions.market_order == "FORBIDDEN"
    assert prohibitions.gtc == "FORBIDDEN"
    assert prohibitions.multi_rung == "FORBIDDEN"
    assert prohibitions.daily_regeneration == "REQUIRED"
    assert rule.toss_live_approval == "HUMAN_APPROVAL_REQUIRED_UNTIL_VETO_WIRING"


@pytest.mark.parametrize(
    ("path", "mutant_value"),
    [
        (("honest_upside_pct_min",), 39),
        (("independent_support_source_count_min",), 1),
        (("final_limit_distance_from_current_pct_range",), [-15, -4]),
        (("prohibitions", "candidate_zero_runtime_gate_relaxation"), "ALLOWED"),
    ],
)
def test_support_reserve_net_rejects_candidate_zero_gate_relaxation(
    path: tuple[str, ...], mutant_value: object
):
    """§Q4-5: candidate count zero never makes a runtime gate looser."""
    raw = _raw()
    target = raw["decision_rules"]["buy.support_reserve_net"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = mutant_value

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_support_reserve_net_rejects_armed_cap_above_50_percent():
    raw = _raw()
    raw["decision_rules"]["buy.support_reserve_net"][
        "tier_armed_required_cash_cap_pct"
    ] = 51

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_support_reserve_net_rejects_clamping_an_out_of_band_anchor():
    raw = _raw()
    raw["decision_rules"]["buy.support_reserve_net"][
        "final_limit_distance_out_of_range"
    ] = "CLAMP"

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_support_reserve_net_rejects_partial_A_limit_fill():
    raw = _raw()
    raw["decision_rules"]["buy.support_reserve_net"]["add_candidate"][
        "partial_A_limit_fill"
    ] = "ALLOWED"

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_support_reserve_net_a_limit_exactly_zero_is_no_order():
    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "buy.support_reserve_net"
    ]
    assert isinstance(rule, SupportReserveNetDecisionRule)

    a_limit = Decimal("0")
    proposed_limit_price = Decimal("12345")
    quantity_from_ceiling = (a_limit / proposed_limit_price).to_integral_value(
        rounding=ROUND_CEILING
    )
    outcome = "NO_ORDER" if a_limit <= Decimal("0") else "ELIGIBLE_TO_SIZE"
    proposed_order_quantity: Decimal | None = (
        None if a_limit <= Decimal("0") else quantity_from_ceiling
    )

    # `ceil(0 / price) == 0` must never become a zero-quantity order.
    assert quantity_from_ceiling == Decimal("0")
    assert outcome == rule.add_candidate.a_limit_lte_zero
    assert proposed_order_quantity is None


@pytest.mark.parametrize(
    ("path", "mutant_value"),
    [
        (("add_candidate", "a_limit_lte_zero"), "ALLOW_ZERO_QUANTITY_ORDER"),
        (
            ("priority_rules", "allocation_order"),
            [
                "first_slot_eligible_new_candidate",
                "dedupe_active_or_resting_same_symbol",
                "add_secondary_pool_only_after_r931_pass_and_full_a_limit_10",
            ],
        ),
        (("priority_rules", "first_slot"), "ADD_CANDIDATE_FIRST"),
        (
            ("priority_rules", "same_intent_class_sort_order"),
            [
                "independent_support_source_count_desc",
                "support_strength_desc",
                "honest_upside_pct_desc",
                "post_fill_sector_increase_asc",
                "required_cash_asc",
            ],
        ),
        (("priority_rules", "exact_tie_break"), "ADD_BEFORE_NEW"),
    ],
)
def test_support_reserve_net_rejects_priority_or_zero_order_contract_mutation(
    path: tuple[str, ...], mutant_value: object
):
    raw = _raw()
    target = raw["decision_rules"]["buy.support_reserve_net"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = mutant_value

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_support_reserve_net_boundary_values_are_inclusive_and_exact():
    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "buy.support_reserve_net"
    ]
    assert isinstance(rule, SupportReserveNetDecisionRule)
    assert rule.tier_armed_required_cash_cap_pct == 50
    assert rule.final_limit_distance_from_current_pct_range[0] == -15
    assert rule.final_limit_distance_from_current_pct_range[1] == -5
    assert rule.honest_upside_pct_min == 40


def test_support_reserve_net_keeps_toss_auto_veto_behind_dedicated_gate(monkeypatch):
    # TOSS-AUTO-FULL (#1844) added toss_live to the veto-capable set, so the
    # reserve-net invariant this test protects is no longer set membership:
    # a toss_live proposal must not auto-submit unless the operator armed the
    # dedicated ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED gate (default false).
    assert ("toss_live", "equity_kr") in _VETO_CAPABLE_ACCOUNT_MARKETS
    assert ("toss_live", "equity_us") in _VETO_CAPABLE_ACCOUNT_MARKETS

    from app.services.order_proposals import auto_approve

    monkeypatch.setattr(
        auto_approve.settings, "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED", False
    )
    for market in ("equity_kr", "equity_us"):
        assert not auto_approve._is_veto_capable_account_market("toss_live", market)

    monkeypatch.setattr(
        auto_approve.settings, "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED", True
    )
    for market in ("equity_kr", "equity_us"):
        assert auto_approve._is_veto_capable_account_market("toss_live", market)


def test_breakeven_reserve_trim_policy_contract_is_machine_readable():
    doc = TradingPolicyDocument.model_validate(_raw())
    rule = doc.decision_rules["sell.trim_preplace"]
    tier = _breakeven_reserve_trim_tier()

    assert [candidate.id for candidate in rule.tiers] == [
        "de_minimis_trim_watch",
        "sell.breakeven_reserve_trim",
        "single_share_full_exit_review",
        "momentum_spike_profit_ladder",
        "rsi_confirmed_resistance",
        "ultra_near_resistance",
        "watch_zone",
        "breakeven_extension_ladder",
    ]
    assert tier.conditions == {
        "markets": ["kr", "us", "crypto"],
        "lot_pnl_basis": "current_price_vs_average_cost",
        "lot_pnl_pct_min_inclusive_negated_policy_key": ("sell.breakeven_near_pct"),
        "lot_pre_guard_average_cost_multiple_max_exclusive_policy_key": (
            "sell.loss_guard_min_multiple"
        ),
        "anchor_operator": "max",
        "anchor_operands": [
            "average_cost_times_loss_guard",
            "d7_compliant_lowest_price",
        ],
        "anchor_guard_policy_key": "sell.loss_guard_min_multiple",
        "d7_min_expected_net_realized_gain_krw_policy_key": (
            "sell.trim_min_expected_net_realized_gain_krw"
        ),
        "d7_minimum_price_basis": (
            "one_share_expected_net_realized_gain_after_estimated_fees_and_taxes"
        ),
        "d7_scope_semantics": "one_share_net_realized_gain_not_total_trim",
        "d7_estimation_limit_semantics": (
            "consumer_estimated_fees_and_taxes_required_no_fee_or_tax_model_added_by_this_tier"
        ),
        "post_max_tick_snap_direction": "ceil",
        "resting_limit_order": True,
        "time_in_force": "DAY",
        "regeneration": "daily_rep",
        "day_expiry_policy_key": "order.day_expiry_kst",
        "submission_contract": "section_40_auto_approve_with_veto",
        "watch_fallback": "anchor_uncomputable_only",
        "advisory": True,
    }
    assert tier.action == "preplace_resting_breakeven_reserve_trim"
    assert tier.sizing == "existing_trim_rule"
    assert rule.tie_breaks["tier_priority"] == (
        "de_minimis_trim_watch > sell.breakeven_reserve_trim > "
        "single_share_full_exit_review > momentum_spike_profit_ladder > "
        "rsi_confirmed_resistance > ultra_near_resistance > watch_zone > "
        "breakeven_extension_ladder"
    )
    assert _threshold_decimal(doc, "sell.loss_guard_min_multiple") == Decimal("1.01")
    assert _threshold_decimal(doc, "sell.breakeven_near_pct") == Decimal("2")
    assert _threshold_decimal(
        doc, "sell.trim_min_expected_net_realized_gain_krw"
    ) == Decimal("5000")


def test_breakeven_reserve_trim_trigger_band_is_lower_inclusive_and_pre_guard():
    tier = _breakeven_reserve_trim_tier()

    assert _breakeven_reserve_trim_triggered(
        tier,
        pnl_pct=Decimal("-2"),
        current_price_multiple=Decimal("0.98"),
    )
    assert _breakeven_reserve_trim_triggered(
        tier,
        pnl_pct=Decimal("0.9999"),
        current_price_multiple=Decimal("1.009999"),
    )
    assert not _breakeven_reserve_trim_triggered(
        tier,
        pnl_pct=Decimal("-2.0001"),
        current_price_multiple=Decimal("0.979999"),
    )
    assert not _breakeven_reserve_trim_triggered(
        tier,
        pnl_pct=Decimal("1"),
        current_price_multiple=Decimal("1.01"),
    )


def test_breakeven_reserve_trim_anchor_never_below_guard_floor():
    tier = _breakeven_reserve_trim_tier()
    average_cost = Decimal("100")
    guard_floor = Decimal("101")

    anchor = _breakeven_reserve_trim_anchor(
        tier,
        average_cost=average_cost,
        d7_compliant_lowest_price=Decimal("100.50"),
    )

    assert anchor >= guard_floor
    assert anchor == guard_floor


def test_breakeven_reserve_trim_anchor_never_below_d7_floor():
    tier = _breakeven_reserve_trim_tier()
    d7_compliant_lowest_price = Decimal("105")

    anchor = _breakeven_reserve_trim_anchor(
        tier,
        average_cost=Decimal("100"),
        d7_compliant_lowest_price=d7_compliant_lowest_price,
    )

    assert anchor >= d7_compliant_lowest_price
    assert anchor == d7_compliant_lowest_price


def test_breakeven_reserve_trim_post_max_tick_snap_ceil_preserves_guard_floor():
    tier = _breakeven_reserve_trim_tier()
    guard_exact = Decimal("256875") * Decimal("1.01")
    tick_size = Decimal("500")
    floor_snap = (guard_exact / tick_size).to_integral_value(
        rounding=ROUND_FLOOR
    ) * tick_size

    snapped_anchor = _breakeven_reserve_trim_post_max_tick_snap(
        tier,
        anchor=guard_exact,
        tick_size=tick_size,
    )

    assert guard_exact == Decimal("259443.75")
    assert floor_snap == Decimal("259000")
    assert floor_snap < guard_exact
    assert snapped_anchor == Decimal("259500")
    assert snapped_anchor >= guard_exact


def test_sell_lane_machine_block_adds_breakeven_reserve_trim_without_tool_or_gate_drift():
    sell = _sell_lane_machine_block()
    policy_tier = next(
        step
        for step in sell["steps"]
        if step.get("policy_tier") == "sell.breakeven_reserve_trim"
    )

    assert [step["tool"] for step in sell["steps"] if "tool" in step] == [
        "toss_get_positions",
        "analyze_stock_batch",
        "sell_ladder_fill_preview",
        "order_proposal_create",
    ]
    assert sell["gates"] == ["loss_guard", "tick_rule", "toss_two_sided"]
    assert policy_tier == {
        "policy_tier": "sell.breakeven_reserve_trim",
        "advisory": True,
        "priority_source": (
            "decision_rules.sell.trim_preplace.tie_breaks.tier_priority"
        ),
        "trigger": {
            "pnl_pct_min_inclusive_negated_policy_key": ("sell.breakeven_near_pct"),
            "pre_guard_average_cost_multiple_max_exclusive_policy_key": (
                "sell.loss_guard_min_multiple"
            ),
        },
        "anchor": {
            "operator": "max",
            "operands": [
                "average_cost_times_loss_guard",
                "d7_compliant_lowest_price",
            ],
            "post_max_tick_snap_direction": "ceil",
        },
        "sizing": "existing_trim_rule",
        "time_in_force": "DAY",
        "regeneration": "daily_rep",
        "submission_contract": "section_40_auto_approve_with_veto",
        "watch_fallback": "anchor_uncomputable_only",
    }


def test_single_share_exit_rule_is_provisional_shadow_only():
    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "sell.single_share_exit"
    ]

    assert rule.activation_state == "shadow"
    assert rule.proposal_enabled is False
    assert rule.scope.markets == ["kr"]
    assert rule.scope.brokers == ["kis", "toss"]
    assert rule.scope.required_broker_inventory == ["kis", "toss"]
    assert rule.scope.order_routable_required is True
    assert rule.conditions.symbol_routable_sellable_quantity_eq == 1
    assert rule.conditions.profit_pct_min == 8
    assert rule.conditions.resistance_reference_required is True
    assert rule.conditions.resistance_strength_min == "strong"
    assert rule.conditions.resistance_distance_pct_min_exclusive == 6
    assert rule.conditions.resistance_distance_pct_max == 15
    assert rule.conditions.resistance_source_family_min == 2
    assert rule.conditions.quote_max_age_seconds == 300
    assert rule.conditions.resistance_max_age_seconds == 300
    assert rule.conditions.holdings_max_age_seconds == 300
    assert rule.conditions.open_orders_max_age_seconds == 300
    assert rule.conditions.open_actions_max_age_seconds == 300
    assert rule.conditions.captured_at_max_age_seconds == 300
    assert rule.conditions.snapshot_max_skew_seconds == 300
    assert rule.conditions.required_completed_bar_market == "XKRX"
    assert (
        rule.conditions.min_sell_price_multiple_policy_key
        == "sell.loss_guard_min_multiple"
    )
    assert rule.conditions.same_symbol_open_orders_max == 0
    assert rule.conditions.unresolved_open_actions_max == 0
    assert rule.conditions.loss_state_uses_existing_path == "loss_cut_only"
    assert rule.proposal.action == "full_exit_at_far_resistance"
    assert rule.proposal.sizing == "full_account_lot_exit"
    assert rule.proposal.approval == "telegram_manual"
    assert rule.proposal.auto_approve is False
    assert rule.proposal.execution == "proposal_only"
    assert rule.threshold_status == "provisional"
    assert rule.operator_approval_required is True
    assert "research must recalibrate this initial threshold" in (
        rule.recalibration_note
    )


def test_single_share_exit_rule_rejects_automatic_approval():
    raw = _raw()
    raw["decision_rules"]["sell.single_share_exit"]["proposal"]["auto_approve"] = True

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_single_share_exit_rule_rejects_live_activation_or_enabled_proposal():
    activation = _raw()
    activation["decision_rules"]["sell.single_share_exit"]["activation_state"] = "live"
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(activation)

    enabled = _raw()
    enabled["decision_rules"]["sell.single_share_exit"]["proposal_enabled"] = True
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(enabled)


def test_single_share_exit_rule_requires_both_kis_and_toss_inventory():
    raw = _raw()
    raw["decision_rules"]["sell.single_share_exit"]["scope"][
        "required_broker_inventory"
    ] = ["kis"]

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_single_share_exit_rule_requires_strong_resistance():
    raw = _raw()
    raw["decision_rules"]["sell.single_share_exit"]["conditions"][
        "resistance_strength_min"
    ] = "moderate"

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_raw_evidence_and_caller_completeness_contract_is_not_public():
    assert not hasattr(policy_schema, "SingleShareExitEvidenceSnapshot")
    assert not hasattr(policy_schema, "SingleShareExitBrokerAccountSnapshot")
    assert not hasattr(policy_schema, "SingleShareExitTargetIdentity")


def test_auto_approve_policy_has_conservative_market_caps():
    auto = TradingPolicyDocument.model_validate(_raw()).order_proposals.auto_approve

    assert auto.min_distance_pct > 0
    assert set(auto.per_order_cap) == {"kr", "us", "crypto"}
    assert set(auto.daily_cap) == {"kr", "us", "crypto"}
    for market, per_order in auto.per_order_cap.items():
        assert per_order > 0
        assert auto.daily_cap[market] >= per_order


def test_crypto_market_rules_preserve_report_derived_and_null_thresholds():
    doc = TradingPolicyDocument.model_validate(_raw())
    rules = doc.market_rules["crypto"]
    gate = rules.recovery_gate

    assert gate.min_conditions_met == 2
    assert gate.of == 2
    assert [condition.id for condition in gate.conditions] == [
        "alt_breadth_24h",
        "btc_long_short_ratio",
    ]
    assert (gate.conditions[0].operator, gate.conditions[0].threshold) == (
        "gt",
        50,
    )
    assert (gate.conditions[1].operator, gate.conditions[1].threshold) == (
        "lte",
        1.5,
    )
    assert [context.id for context in gate.advisory_context] == [
        "fear_greed",
        "btc_kimchi_premium",
    ]
    assert all(context.threshold is None for context in gate.advisory_context)
    assert rules.no_chasing.daily_change_pct_threshold is None
    assert rules.no_chasing.min_trade_value_24h_krw is None
    assert rules.support_resistance.source_priority == [
        "fibonacci",
        "value_area",
        "bb_lower",
        "bb_upper",
        "bb_middle",
        "volume_poc",
    ]


def test_decision_rule_schema_accepts_sell_trim_preplace_block():
    raw = _raw()
    raw["decision_rules"] = {
        "sell.trim_preplace": {
            "lanes": ["sell"],
            "semantics": "Tie-break resistance-near vs upside-rich sell signals.",
            "tiers": [
                {
                    "id": "rsi_confirmed_resistance",
                    "conditions": {
                        "rsi_min_policy_key": "sell.rsi_place_min",
                        "resistance_near_pct_max_policy_key": (
                            "sell.resistance_near_pct"
                        ),
                    },
                    "action": "preplace_small_trim_ladder",
                    "sizing": "small_trim_only",
                }
            ],
            "tie_breaks": {
                "sell.upside_place_max_pct": "size_limit_only",
            },
            "exclusions": ["single_share_position"],
        }
    }
    doc = TradingPolicyDocument.model_validate(raw)
    rule = doc.decision_rules["sell.trim_preplace"]
    assert rule.tiers[0].action == "preplace_small_trim_ladder"


def test_extra_key_rejected():
    raw = _raw()
    raw["unexpected_top_level"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_posture_rejects_sixth_or_missing_state():
    extra = _raw()
    extra["posture"]["states"].append("CATALYST_GAP_CAPTURE_V1")
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(extra)

    missing = _raw()
    missing["posture"]["states"].remove("EXPIRED_REARMABLE")
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(missing)


def test_posture_stage_one_rejects_non_shadow_mode():
    raw = _raw()
    raw["posture"]["mode"] = "sell_pilot"
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_extra_threshold_key_rejected():
    raw = _raw()
    raw["thresholds"]["screen.rsi_max"]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_extra_crypto_market_rule_key_rejected():
    raw = _raw()
    raw["market_rules"]["crypto"]["no_chasing"]["bogus"] = True
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_crash_day_trigger_and_actions_parse():
    doc = TradingPolicyDocument.model_validate(_raw())
    crash_day = doc.crash_day

    assert crash_day.trigger.index_symbol == "069500"
    assert crash_day.trigger.index_gap_pct_max == -3.0
    assert crash_day.actions.new_entry_hold is True
    exception = crash_day.actions.new_entry_hold_exception
    assert isinstance(exception, CrashDayNewEntryHoldException)
    assert exception.enabled is True
    assert exception.requires.standard_buy_gates == (
        "all_pass_including_support_quality"
    )
    assert exception.requires.support_quality == "required"
    assert exception.requires.price_zone == "strong_support"
    assert exception.requires.gate_relaxation == "none"
    assert exception.sizing.per_symbol_notional_multiplier == 0.5
    assert exception.sizing.max_new_symbols == 1
    assert "즉석 판단 허용이 아니라" in exception.semantics
    assert "전부" in exception.semantics
    assert "면제·완화·대체하지 않는다" in exception.semantics
    assert crash_day.actions.deep_rung_reprice_to_band_floor is True
    assert crash_day.actions.profit_trim_marketable_allowed is True
    assert crash_day.actions.defensive_brief_cross_check is True


def test_crash_day_extra_trigger_key_rejected():
    raw = _raw()
    raw["crash_day"]["trigger"]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_crash_day_extra_actions_key_rejected():
    raw = _raw()
    raw["crash_day"]["actions"]["bogus"] = True
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


@pytest.mark.parametrize(
    ("path", "mutant_value"),
    [
        (("standard_buy_gates",), "all_pass_except_rsi"),
        (("support_quality",), "optional"),
        (("gate_relaxation",), "allow_some_gates"),
    ],
)
def test_crash_day_new_entry_exception_rejects_gate_relaxation(
    path: tuple[str, ...], mutant_value: str
):
    raw = _raw()
    target = raw["crash_day"]["actions"]["new_entry_hold_exception"]["requires"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = mutant_value

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_crash_day_new_entry_exception_rejects_typo_key():
    raw = _raw()
    raw["crash_day"]["actions"]["new_entry_hold_exception"]["sizing"]["bogus"] = 1

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_preplanned_support_ladder_parses_and_rejects_typo_key():
    raw = _raw()
    doc = TradingPolicyDocument.model_validate(raw)
    ladder = doc.decision_rules["buy.preplanned_support_ladder"]

    assert isinstance(ladder, PreplannedSupportLadderPolicy)
    assert ladder.lanes == ["buy"]
    assert ladder.enabled is True
    assert ladder.eligibility == "standard_buy_gates_pass"
    assert ladder.rungs_max == 2
    assert ladder.per_rung_notional_multiplier == 0.5
    assert ladder.crash_day_behavior == "keep"

    raw["decision_rules"]["buy.preplanned_support_ladder"]["bogus"] = True
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_rob_1289_policy_loader_roundtrip_preserves_both_new_blocks():
    loaded = load_trading_policy()
    reparsed = TradingPolicyDocument.model_validate(loaded.model_dump())

    assert reparsed.model_dump() == loaded.model_dump()
    assert "buy.preplanned_support_ladder" in reparsed.decision_rules
    assert reparsed.crash_day.actions.new_entry_hold_exception.enabled is True


def test_rob_1289_preserves_all_preexisting_policy_keys_and_values():
    baseline = yaml.safe_load(_ROB1289_BASELINE.read_text(encoding="utf-8"))
    current_raw = _raw()
    # The pre-change document cannot satisfy the new required fields until
    # those two additive blocks are copied in; they are removed before compare.
    baseline["decision_rules"]["buy.preplanned_support_ladder"] = current_raw[
        "decision_rules"
    ]["buy.preplanned_support_ladder"]
    baseline["crash_day"]["actions"]["new_entry_hold_exception"] = current_raw[
        "crash_day"
    ]["actions"]["new_entry_hold_exception"]
    # ROB-1298 KEY_DIFF — the §115차 tier is appended to the current document
    # only. The schema now requires tie_breaks.tier_priority to match the
    # declared tier order, so the baseline copy is given the same appended tier
    # and priority string before parsing; both are then stripped so the
    # remaining comparison is a closed equivalence over every other key.
    baseline_trim = baseline["decision_rules"]["sell.trim_preplace"]
    current_trim = current_raw["decision_rules"]["sell.trim_preplace"]
    baseline_trim["semantics"] = current_trim["semantics"]
    baseline_trim["tiers"] = baseline_trim["tiers"] + [current_trim["tiers"][-1]]
    baseline_trim["tie_breaks"]["tier_priority"] = current_trim["tie_breaks"][
        "tier_priority"
    ]
    baseline_dump = TradingPolicyDocument.model_validate(baseline).model_dump()
    current_dump = TradingPolicyDocument.model_validate(current_raw).model_dump()

    current_dump["version"] = baseline_dump["version"]
    # `source` is provenance prose (the machine-readable analogue of a comment).
    # It may only be *extended*, never rewritten, so the baseline text has to
    # remain a prefix of it before it is normalized away.
    assert current_dump["source"].startswith(baseline_dump["source"])
    assert "ROB-1298" in current_dump["source"]
    current_dump["source"] = baseline_dump["source"]
    del current_dump["decision_rules"]["buy.preplanned_support_ladder"]
    del current_dump["crash_day"]["actions"]["new_entry_hold_exception"]
    del baseline_dump["decision_rules"]["buy.preplanned_support_ladder"]
    del baseline_dump["crash_day"]["actions"]["new_entry_hold_exception"]

    normalized_current_dump = deepcopy(current_dump)
    for path, baseline_value, current_value in _ROB1292_ALLOWED_POLICY_DELTAS:
        assert _policy_path_get(baseline_dump, path) == baseline_value
        assert _policy_path_get(current_dump, path) == current_value
        _policy_path_set(normalized_current_dump, path, baseline_value)

    # ROB-1298 — the appended tier, the three additive tie_break notes, the
    # tier_priority string, and the rule semantics prose are the only
    # sell.trim_preplace deltas; strip exactly those and nothing else.
    for dump in (normalized_current_dump, baseline_dump):
        trim = dump["decision_rules"]["sell.trim_preplace"]
        assert trim["tiers"][-1]["id"] == "breakeven_extension_ladder"
        del trim["tiers"][-1]
        assert (
            trim["tie_breaks"]
            .pop("tier_priority")
            .endswith("> watch_zone > breakeven_extension_ladder")
        )
        del trim["semantics"]
    for key in _ROB1298_ADDITIVE_TIE_BREAK_KEYS:
        assert (
            key
            in normalized_current_dump["decision_rules"]["sell.trim_preplace"][
                "tie_breaks"
            ]
        )
        del normalized_current_dump["decision_rules"]["sell.trim_preplace"][
            "tie_breaks"
        ][key]

    # Only the six explicitly enumerated cap deltas and the enumerated
    # §115차 additions are accepted; every other pre-existing key/value,
    # including the retained exclusions list, must still
    # match the ROB-1289 baseline exactly.
    assert normalized_current_dump == baseline_dump


def test_crash_day_missing_block_rejected():
    raw = _raw()
    del raw["crash_day"]
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_user_stance_ai_demand_selective_parses():
    doc = TradingPolicyDocument.model_validate(_raw())
    stances = {stance.id: stance for stance in doc.user_stances}
    stance = stances["ai-demand-real-value-selective"]

    assert stance.stance.startswith("AI 수요는 실사용 관점에서 실재")
    assert len(stance.implications) == 4
    assert (
        "3배 레버리지 ETF(SOXL류)는 눌림 보유 수단에서 기본 제외 (변동성 감쇠)"
        in stance.implications
    )
    assert stance.risk_scenario.startswith("효율 충격")
    assert stance.review_condition.startswith("하이퍼스케일러 AI capex 감소 가이던스")
    assert stance.review_date == "2026-10-17"


def test_user_stance_extra_key_rejected():
    raw = _raw()
    raw["user_stances"][0]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_user_stance_missing_required_field_rejected():
    raw = _raw()
    del raw["user_stances"][0]["risk_scenario"]
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_user_stance_invalid_review_date_rejected():
    raw = _raw()
    raw["user_stances"][0]["review_date"] = "not-a-date"
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_user_stances_missing_block_rejected():
    raw = _raw()
    del raw["user_stances"]
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_kr_notional_range_semantics_scoped_to_kr_lane():
    doc = TradingPolicyDocument.model_validate(_raw())
    kr_range = doc.thresholds["buy.per_symbol_notional_krw_range"]

    assert kr_range.value == [200000, 400000]
    assert "KR lane only" in kr_range.semantics


def test_us_notional_usd_range_parses_with_one_share_exception():
    doc = TradingPolicyDocument.model_validate(_raw())
    us_range = doc.thresholds["buy.per_symbol_notional_usd_range"]

    assert us_range.lanes == ["buy", "discovery"]
    assert us_range.value == [150, 450]
    assert us_range.unit == "usd"
    exception = us_range.one_share_exception
    assert exception is not None
    assert exception.enabled is True
    assert exception.absolute_ceiling_usd == 700
    assert exception.max_deep_rungs == 1


def test_other_thresholds_default_one_share_exception_to_none():
    doc = TradingPolicyDocument.model_validate(_raw())
    assert doc.thresholds["screen.rsi_max"].one_share_exception is None


def test_us_notional_usd_range_one_share_exception_extra_key_rejected():
    raw = _raw()
    raw["thresholds"]["buy.per_symbol_notional_usd_range"]["one_share_exception"][
        "bogus"
    ] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_us_notional_usd_range_one_share_exception_missing_required_field_rejected():
    raw = _raw()
    del raw["thresholds"]["buy.per_symbol_notional_usd_range"]["one_share_exception"][
        "max_deep_rungs"
    ]
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


# ---------------------------------------------------------------------------
# ROB-1298 §115차 — breakeven extension ladder (zero named resistance fallback)
# ---------------------------------------------------------------------------

_LADDER_TIER_ID = "breakeven_extension_ladder"

# Exact transcription of the Upbit KRW price-unit table documented on
# ``app.services.brokers.upbit.orders.adjust_price_to_upbit_unit``. Pinned to
# that production helper by
# ``test_crypto_krw_tick_transcription_matches_the_production_upbit_grid``.
_UPBIT_KRW_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000000"), Decimal("1000")),
    (Decimal("1000000"), Decimal("500")),
    (Decimal("500000"), Decimal("100")),
    (Decimal("100000"), Decimal("50")),
    (Decimal("10000"), Decimal("10")),
    (Decimal("1000"), Decimal("5")),
    (Decimal("100"), Decimal("1")),
)


def _crypto_krw_tick(price: Decimal) -> Decimal:
    for lower, tick in _UPBIT_KRW_TICK_BANDS:
        if price >= lower:
            return tick
    raise AssertionError(f"sub-100 KRW crypto price out of scope: {price}")


def _market_tick(market: str, price: Decimal) -> Decimal:
    """Market-native sell-side tick grid the tier's ``tick_grid`` names."""

    if market == "crypto":
        return _crypto_krw_tick(price)
    if market == "kr":
        return Decimal(get_tick_size_kr(float(price)))
    if market == "us":
        return Decimal("0.01")
    raise AssertionError(f"unsupported market: {market!r}")


def _breakeven_extension_ladder_tier():
    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "sell.trim_preplace"
    ]
    return next(tier for tier in rule.tiers if tier.id == _LADDER_TIER_ID)


def _breakeven_extension_rungs(
    tier,
    *,
    market: str,
    average_cost: Decimal,
) -> list[Decimal]:
    """Test-only interpreter for the declarative rung contract.

    Nothing here invents policy: the anchor basis, the multiples, the rung
    count, and the snap direction are all read out of the YAML tier.
    """

    conditions = tier.conditions
    assert conditions["anchor_basis"] == "average_cost"
    rounding = {"ceil": ROUND_CEILING, "floor": ROUND_FLOOR}[
        str(conditions["tick_snap_direction"])
    ]
    multiples = conditions["anchor_average_cost_multiples"]
    assert len(multiples) == conditions["rungs_max"]

    rungs: list[Decimal] = []
    for multiple in multiples:
        raw = average_cost * Decimal(str(multiple))
        tick = _market_tick(market, raw)
        rungs.append((raw / tick).to_integral_value(rounding=rounding) * tick)
    return rungs


def _ladder_matches(tier, *, market: str, fresh_named_resistance_count: int) -> bool:
    """Test-only interpreter for the declarative eligibility contract."""

    conditions = tier.conditions
    return (
        market in conditions["markets"]
        and fresh_named_resistance_count
        == conditions["fresh_named_resistance_count_eq"]
    )


def test_crypto_krw_tick_transcription_matches_the_production_upbit_grid():
    from app.services.brokers.upbit.orders import adjust_price_to_upbit_unit

    for probe in (
        Decimal("3168337"),
        Decimal("3485170.7"),
        Decimal("1234567"),
        Decimal("777777"),
        Decimal("123456"),
        Decimal("54321"),
        Decimal("4321"),
        Decimal("321"),
    ):
        tick = _crypto_krw_tick(probe)
        snapped = Decimal(str(adjust_price_to_upbit_unit(float(probe))))
        assert snapped % tick == 0, (probe, tick, snapped)
        assert abs(snapped - probe) <= tick


def test_breakeven_extension_ladder_reproduces_eth_rungs():
    """AC1 — ETH 실데이터 평단 3,168,337 (crypto/Upbit KRW, tick 1,000).

    🔴 Rung 3 does NOT reproduce the value stated in the issue AC. Under the
    tier's own declared ``tick_snap_direction: ceil`` the third rung is
    3,486,000, not 3,485,000. 3,485,000 is the *floor* of 3,168,337 × 1.10 =
    3,485,170.7, and no tick size on the Upbit KRW grid makes a ceil land on
    it. Rungs 1 and 2 match the AC exactly and are ceil results, so the AC is
    internally inconsistent on rung 3 rather than the snap direction being
    wrong. This test pins the computed values and the delta instead of bending
    the policy to fit the stated number.
    """

    tier = _breakeven_extension_ladder_tier()
    rungs = _breakeven_extension_rungs(
        tier, market="crypto", average_cost=Decimal("3168337")
    )

    assert rungs == [
        Decimal("3201000"),
        Decimal("3327000"),
        Decimal("3486000"),
    ]
    # AC1 rungs 1-2 reproduce verbatim.
    assert rungs[:2] == [Decimal("3201000"), Decimal("3327000")]
    # AC1 rung 3 delta, stated rather than hidden.
    ac1_stated_third_rung = Decimal("3485000")
    raw_third_rung = Decimal("3168337") * Decimal("1.10")
    assert raw_third_rung == Decimal("3485170.70")
    assert rungs[2] - ac1_stated_third_rung == Decimal("1000")
    assert ac1_stated_third_rung < raw_third_rung  # i.e. it is a floor, not a ceil
    # Every rung must clear its own raw anchor after snapping (ceil, never below).
    for rung, multiple in zip(
        rungs, tier.conditions["anchor_average_cost_multiples"], strict=True
    ):
        assert rung >= Decimal("3168337") * Decimal(str(multiple))


def test_breakeven_extension_ladder_lowest_rung_reuses_the_loss_guard_formula():
    doc = TradingPolicyDocument.model_validate(_raw())
    tier = _breakeven_extension_ladder_tier()
    conditions = tier.conditions

    guard_key = str(conditions["anchor_lowest_rung_policy_key"])
    assert guard_key == "sell.loss_guard_min_multiple"
    guard = _threshold_decimal(doc, guard_key)
    multiples = conditions["anchor_average_cost_multiples"]

    # 평단×1.01 = loss_guard 하한과 동일 산식 — the literal is the threshold.
    assert Decimal(str(multiples[0])) == guard
    assert [Decimal(str(value)) for value in multiples] == [
        Decimal("1.01"),
        Decimal("1.05"),
        Decimal("1.10"),
    ]
    # The guard threshold itself is untouched by this change.
    assert doc.thresholds["sell.loss_guard_min_multiple"].value == 1.01


def test_breakeven_extension_ladder_applies_to_kr_us_and_crypto():
    tier = _breakeven_extension_ladder_tier()
    assert tier.conditions["markets"] == ["kr", "us", "crypto"]

    # KR uses the production KRX sell-side grid; US uses the cent grid.
    kr_rungs = _breakeven_extension_rungs(
        tier, market="kr", average_cost=Decimal("71300")
    )
    assert kr_rungs == [Decimal("72100"), Decimal("74900"), Decimal("78500")]
    for rung in kr_rungs:
        assert rung % Decimal(get_tick_size_kr(float(rung))) == 0

    us_rungs = _breakeven_extension_rungs(
        tier, market="us", average_cost=Decimal("187.42")
    )
    assert us_rungs == [Decimal("189.30"), Decimal("196.80"), Decimal("206.17")]


def test_breakeven_extension_ladder_never_matches_a_holding_with_named_resistance():
    """AC2 — BTC-style holding (fresh named resistance present) stays excluded."""

    tier = _breakeven_extension_ladder_tier()

    for market in ("kr", "us", "crypto"):
        # ETH/LINK case — zero fresh named resistance, the tier is reachable.
        assert _ladder_matches(tier, market=market, fresh_named_resistance_count=0)
        # BTC case — any named resistance at all keeps the tier out.
        for count in (1, 2, 5):
            assert not _ladder_matches(
                tier, market=market, fresh_named_resistance_count=count
            )

    # The tier carries no resistance-proximity condition, so it cannot express
    # an opinion about a holding that still has a resistance frame.
    assert not [key for key in tier.conditions if "resistance_near_pct" in key]


def test_breakeven_extension_ladder_is_last_so_resistance_tiers_keep_priority():
    """AC6 — FALLBACK_ORDER pinned in both the tier order and tie_breaks."""

    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "sell.trim_preplace"
    ]
    tier_ids = [tier.id for tier in rule.tiers]

    assert tier_ids[-1] == _LADDER_TIER_ID
    resistance_tiers = [
        "single_share_full_exit_review",
        "momentum_spike_profit_ladder",
        "rsi_confirmed_resistance",
        "ultra_near_resistance",
        "watch_zone",
    ]
    for name in resistance_tiers:
        assert tier_ids.index(name) < tier_ids.index(_LADDER_TIER_ID)

    assert rule.tie_breaks["multiple_tiers_matched"] == "first_matching_tier_wins"
    assert [part.strip() for part in rule.tie_breaks["tier_priority"].split(">")] == (
        tier_ids
    )
    assert rule.tie_breaks["breakeven_extension_fallback_order"] == (
        "named_resistance_tiers_first_extension_ladder_only_when_zero_fresh"
        "_named_resistance"
    )
    assert rule.tie_breaks["breakeven_extension_minimum_benefit"] == (
        "de_minimis_trim_watch_still_preempts"
    )
    # D7 minimum-benefit watch still preempts the new tier.
    assert tier_ids[0] == "de_minimis_trim_watch"


def test_breakeven_extension_ladder_keeps_the_no_resistance_reference_exclusion():
    """AC4 / NO_EXCLUSION_REMOVAL — the exclusion is retained, not deleted."""

    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "sell.trim_preplace"
    ]
    assert rule.exclusions == ["no_resistance_reference", "composite_gates"]
    assert rule.tie_breaks["breakeven_extension_exclusion_retained"] == (
        "no_resistance_reference_stays_in_exclusions_dedicated_tier_not"
        "_exclusion_removal"
    )
    assert (
        _breakeven_extension_ladder_tier().conditions["matched_exclusion_case"]
        == "no_resistance_reference"
    )


def test_breakeven_extension_ladder_reuses_existing_trim_sizing():
    """AC7 / SIZING_REUSE — no new quantity rule is introduced."""

    tier = _breakeven_extension_ladder_tier()
    assert tier.sizing == "existing_trim_rule"
    assert tier.action == "preplace_resting_breakeven_extension_ladder"

    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "sell.trim_preplace"
    ]
    # The only sizing token this tier uses is one an existing tier already uses.
    existing_sizings = {
        candidate.sizing for candidate in rule.tiers if candidate.id != _LADDER_TIER_ID
    }
    assert tier.sizing in existing_sizings
    # No quantity/percentage key is declared on the tier at all.
    assert not [
        key
        for key in tier.conditions
        if any(token in key for token in ("quantity", "position_pct", "notional"))
    ]


def _ladder_mutant(mutate) -> dict:
    raw = _raw()
    rule = raw["decision_rules"]["sell.trim_preplace"]
    mutate(rule)
    return raw


def _mutate_drop_exclusion(rule) -> None:
    rule["exclusions"].remove("no_resistance_reference")


def _mutate_move_ladder_ahead_of_resistance_tiers(rule) -> None:
    ladder = rule["tiers"].pop()
    rule["tiers"].insert(2, ladder)


def _mutate_allow_any_resistance_count(rule) -> None:
    rule["tiers"][-1]["conditions"]["fresh_named_resistance_count_eq"] = 1


def _mutate_add_resistance_proximity_condition(rule) -> None:
    rule["tiers"][-1]["conditions"]["resistance_near_pct_max"] = 6


def _mutate_invent_new_sizing(rule) -> None:
    rule["tiers"][-1]["sizing"] = "breakeven_extension_third_position"


def _mutate_snap_down(rule) -> None:
    rule["tiers"][-1]["conditions"]["tick_snap_direction"] = "floor"


def _mutate_rung_below_average_cost(rule) -> None:
    rule["tiers"][-1]["conditions"]["anchor_average_cost_multiples"] = [
        0.99,
        1.05,
        1.10,
    ]


def _mutate_unordered_rungs(rule) -> None:
    rule["tiers"][-1]["conditions"]["anchor_average_cost_multiples"] = [
        1.05,
        1.01,
        1.10,
    ]


def _mutate_rung_count_mismatch(rule) -> None:
    rule["tiers"][-1]["conditions"]["rungs_max"] = 2


def _mutate_detach_from_loss_guard_key(rule) -> None:
    rule["tiers"][-1]["conditions"]["anchor_lowest_rung_policy_key"] = (
        "sell.breakeven_near_pct"
    )


def _mutate_stale_tier_priority(rule) -> None:
    rule["tie_breaks"]["tier_priority"] = rule["tie_breaks"]["tier_priority"].replace(
        " > breakeven_extension_ladder", ""
    )


def _mutate_drop_fallback_only_flag(rule) -> None:
    rule["tiers"][-1]["conditions"]["resistance_tier_fallback_only"] = False


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_drop_exclusion,
        _mutate_move_ladder_ahead_of_resistance_tiers,
        _mutate_allow_any_resistance_count,
        _mutate_add_resistance_proximity_condition,
        _mutate_invent_new_sizing,
        _mutate_snap_down,
        _mutate_rung_below_average_cost,
        _mutate_unordered_rungs,
        _mutate_rung_count_mismatch,
        _mutate_detach_from_loss_guard_key,
        _mutate_stale_tier_priority,
        _mutate_drop_fallback_only_flag,
    ],
    ids=[
        "exclusion_removed",
        "ladder_before_resistance_tiers",
        "matches_when_resistance_exists",
        "carries_resistance_proximity_condition",
        "invents_new_sizing_rule",
        "snaps_rungs_down",
        "rung_below_average_cost",
        "rungs_not_ascending",
        "rung_count_mismatch",
        "detached_from_loss_guard_key",
        "stale_tier_priority",
        "fallback_only_flag_dropped",
    ],
)
def test_breakeven_extension_ladder_mutants_are_rejected(mutate):
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(_ladder_mutant(mutate))


def test_rob_1298_leaves_loss_guard_d7_and_auto_approve_gates_untouched():
    """AC5 / GATES_UNTOUCHED — asserted against the ROB-1289 baseline document."""

    baseline = yaml.safe_load(_ROB1289_BASELINE.read_text(encoding="utf-8"))
    current = _raw()

    # Loss guard and D7 minimum-benefit thresholds: byte-identical blocks.
    for key in (
        "sell.loss_guard_min_multiple",
        "sell.loss_cut_max_slip",
        "sell.breakeven_near_pct",
        "sell.trim_min_expected_net_realized_gain_krw",
    ):
        assert current["thresholds"][key] == baseline["thresholds"][key]

    # Auto-approve gate: only the enumerated cap deltas already enumerated by
    # ROB-1292 differ; every other auto-approve key is unchanged by ROB-1298.
    current_auto = deepcopy(current["order_proposals"]["auto_approve"])
    baseline_auto = deepcopy(baseline["order_proposals"]["auto_approve"])
    for path, baseline_value, _current_value in _ROB1292_ALLOWED_POLICY_DELTAS:
        suffix = path.removeprefix("order_proposals.auto_approve.")
        _policy_path_set(current_auto, suffix, baseline_value)
    assert current_auto == baseline_auto

    # The de_minimis (D7) watch tier itself is unchanged.
    baseline_trim = baseline["decision_rules"]["sell.trim_preplace"]
    current_trim = current["decision_rules"]["sell.trim_preplace"]
    assert current_trim["tiers"][0] == baseline_trim["tiers"][0]
    assert current_trim["tiers"][1] == baseline_trim["tiers"][1]
