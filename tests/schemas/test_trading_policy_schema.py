from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import app.schemas.trading_policy as policy_schema
from app.schemas.trading_policy import (
    SupportReserveNetDecisionRule,
    TradingPolicyDocument,
)
from app.services.order_proposals.auto_approve import _VETO_CAPABLE_ACCOUNT_MARKETS
from app.services.trading_policy_service import load_trading_policy

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"


def _raw() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _breakeven_reserve_trim_tier():
    rule = TradingPolicyDocument.model_validate(_raw()).decision_rules[
        "sell.trim_preplace"
    ]
    return next(tier for tier in rule.tiers if tier.id == "sell.breakeven_reserve_trim")


def _threshold_decimal(doc: TradingPolicyDocument, key: str) -> Decimal:
    return Decimal(str(doc.thresholds[key].value))


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


def test_support_reserve_net_keeps_toss_outside_auto_veto_capable_combinations():
    assert all(
        account_mode != "toss_live" for account_mode, _ in _VETO_CAPABLE_ACCOUNT_MARKETS
    )




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
        "rsi_confirmed_resistance > ultra_near_resistance > watch_zone"
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
