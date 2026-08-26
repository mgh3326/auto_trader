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
from app.services.order_proposals.auto_approve import (
    _VETO_CAPABLE_ACCOUNT_MARKETS,
    AutoApproveLimits,
    classify_sell_profit,
)
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

# §142차 (2026-08-23) — the ONLY delta this bugfix makes to a pre-existing
# tier: seven additive condition keys on sell.breakeven_reserve_trim that
# declare the effective post-max anchor. No pre-existing key or value is
# touched, which is what the closed-equivalence test below re-proves.
_S142_RESERVE_TRIM_ADDITIVE_CONDITION_KEYS = (
    "post_max_effective_anchor_operator",
    "post_max_effective_anchor_operands",
    "post_max_effective_anchor_band_policy_key",
    "post_max_effective_anchor_reason",
    "post_max_effective_anchor_band_comparison_unchanged",
    "post_max_effective_anchor_since_policy_version",
    "post_max_effective_anchor_retroactive",
)

_ROB1292_ALLOWED_POLICY_DELTAS = (
    ("order_proposals.auto_approve.per_order_cap.kr", 400000, 2000000),
    ("order_proposals.auto_approve.per_order_cap.us", 800, 1500),
    # §145차 (2026-08-23): 1000000 -> 5000000 (operator cap re-definition).
    ("order_proposals.auto_approve.per_order_cap.crypto", 100000, 5000000),
    ("order_proposals.auto_approve.daily_cap.kr", 400000, 5000000),
    ("order_proposals.auto_approve.daily_cap.us", 5000, 20000),
    # §145차 (2026-08-23): 5000000 -> 10000000, kept proportional to the
    # per-order cap so the §71차 demotion does not reappear on the crypto lane.
    ("order_proposals.auto_approve.daily_cap.crypto", 300000, 10000000),
)

# §139차 (2026-08-22) — value deltas outside the auto-approve block. Kept in a
# separate tuple because _ROB1292_ALLOWED_POLICY_DELTAS is also consumed by a
# test that strips the "order_proposals.auto_approve." prefix from every entry.
# Key tuples, not dotted strings: policy threshold keys contain dots
# ("buy.per_symbol_notional_usd_range"), so the dotted-path helpers used for
# the auto-approve block cannot address them.
_S139_ALLOWED_POLICY_DELTAS = (
    (
        (
            "thresholds",
            "buy.per_symbol_notional_usd_range",
            "one_share_exception",
            "absolute_ceiling_usd",
        ),
        700,
        10000,
    ),
)


def _raw() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _policy_path_get(payload: dict, path: str):
    value = payload
    for key in path.split("."):
        value = value[key]
    return value


def _policy_keys_get(payload: dict, keys: tuple[str, ...]):
    value = payload
    for key in keys:
        value = value[key]
    return value


def _policy_keys_set(payload: dict, keys: tuple[str, ...], value) -> None:
    target = payload
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value


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


def _breakeven_band_pct(policy_key: str) -> Decimal:
    """Resolve the §40차 band from its declared dotted policy key."""

    value = _raw()
    for part in policy_key.split("."):
        value = value[part]
    return Decimal(str(value))


def _first_valid_tick_strictly_above(market: str, value: Decimal) -> Decimal:
    """Smallest price on the market grid that is > ``value`` (never ==)."""

    tick = _market_tick(market, value)
    snapped = (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if snapped <= value:
        snapped += tick
    # A band-boundary crossing must still land on the grid that governs the
    # resulting price; every KRX/Upbit band boundary is a multiple of the
    # coarser tick, so this holds -- assert it rather than assume it.
    assert snapped % _market_tick(market, snapped) == 0, (market, value, snapped)
    return snapped


def _band_clearing_effective_anchor(
    conditions,
    *,
    prefix: str,
    market: str,
    average_cost: Decimal,
    tick_ceiled_raw_anchor: Decimal,
    raw_operand: str,
) -> Decimal:
    """Test-only interpreter for the §142차 effective-anchor contract.

    Nothing is invented here: the operator, the operand names, and the band
    policy key are all read out of the YAML tier.
    """

    band_pct = _breakeven_band_pct(
        str(conditions[f"{prefix}_band_policy_key"]),
    )
    band_edge = average_cost * (Decimal("1") + band_pct / Decimal("100"))
    operands = {
        raw_operand: tick_ceiled_raw_anchor,
        "first_valid_tick_strictly_above_average_cost_"
        "times_one_plus_breakeven_band": _first_valid_tick_strictly_above(
            market, band_edge
        ),
    }
    resolved = [operands[str(name)] for name in conditions[f"{prefix}_operands"]]
    operator = conditions[f"{prefix}_operator"]
    if operator == "max":
        return max(resolved)
    raise AssertionError(f"unsupported effective-anchor operator: {operator!r}")


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
    assert doc.version == "2026-08-26.3"
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


def test_s156_scope_addendum_pins_version_and_preserves_auto_approve_keyset():
    """§156 ②④⑤ changes no auto-approve key or default-mode setting."""
    current = _raw()
    baseline = yaml.safe_load(_ROB1289_BASELINE.read_text(encoding="utf-8"))
    current_auto = deepcopy(current["order_proposals"]["auto_approve"])
    baseline_auto = deepcopy(baseline["order_proposals"]["auto_approve"])

    assert current["version"] == "2026-08-26.3"
    assert "§156차 auto-approval authorization revision 2026-08-26" in current["source"]
    assert "§156차 scope addendum ④⑤ 2026-08-26" in current["source"]
    assert "§156차 final scope addendum ② 2026-08-26" in current["source"]
    assert (
        "earlier pending-canonical-tier-evidence wording is superseded"
        in current["source"]
    )
    assert (
        "deliberately relaxed from a reserve-net admission backstop"
        in current["source"]
    )
    assert set(current_auto) == {
        "min_distance_pct",
        "per_order_cap",
        "daily_cap",
        "breakeven_band_pct",
        "round_trip_cost_bps",
    }
    for path, baseline_value, current_value in _ROB1292_ALLOWED_POLICY_DELTAS:
        assert _policy_path_get(current, path) == current_value
        suffix = path.removeprefix("order_proposals.auto_approve.")
        _policy_path_set(current_auto, suffix, baseline_value)
    assert current_auto == baseline_auto


def test_s156_scope_addendum_preserves_cap_keys_and_separate_hard_gates():
    """The cap value survives while only its reserve-net admission role changes."""
    current = _raw()
    baseline = yaml.safe_load(_ROB1289_BASELINE.read_text(encoding="utf-8"))
    current_cap = current["thresholds"]["portfolio.sector_cluster_cap_pct"]
    baseline_cap = baseline["thresholds"]["portfolio.sector_cluster_cap_pct"]
    current_reserve = current["decision_rules"]["buy.support_reserve_net"]
    baseline_reserve = baseline["decision_rules"]["buy.support_reserve_net"]

    assert set(current_cap) == set(baseline_cap)
    assert current_cap["lanes"] == baseline_cap["lanes"]
    assert current_cap["value"] == baseline_cap["value"] == 10
    assert "advisory only" in current_cap["semantics"]
    for key in ("unknown_sector", "max_symbols_per_sector_cluster"):
        assert current_reserve[key] == baseline_reserve[key]
    current_theme = current["thresholds"]["portfolio.max_symbols_per_theme"]
    baseline_theme = baseline["thresholds"]["portfolio.max_symbols_per_theme"]
    assert set(current_theme) == set(baseline_theme)
    for key in ("lanes", "value", "unit"):
        assert current_theme[key] == baseline_theme[key]
    assert "sector concentration is surfaced" in current_theme["semantics"]


def test_support_reserve_net_literal_policy_prefix_is_frozen():
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


def test_s148_clarifies_scope_and_preserves_remaining_policy_literals() -> None:
    doc = TradingPolicyDocument.model_validate(_raw())
    rule = doc.decision_rules["buy.support_reserve_net"]
    assert doc.version == "2026-08-26.3"
    assert (
        "§148차 A(k) eligibility wording contradiction resolution 2026-08-24"
        in doc.source
    )
    assert "Q4 permitted class: policy-clause contradiction resolution" in doc.source
    assert "threshold/value/enum changes 0" in doc.source
    assert "application-scope clarification only" in doc.source

    current = _raw()
    baseline = yaml.safe_load(_ROB1289_BASELINE.read_text(encoding="utf-8"))
    current_reserve = current["decision_rules"]["buy.support_reserve_net"]
    baseline_reserve = baseline["decision_rules"]["buy.support_reserve_net"]

    # The five load-bearing boundaries are byte-for-byte unchanged.
    for key in (
        "support_strength_min",
        "independent_support_source_count_min",
        "independent_support_source_families",
        "support_within_current_pct_max",
        "honest_upside_pct_min",
    ):
        assert current_reserve[key] == baseline_reserve[key]
    assert (
        current["thresholds"]["screen.support_within_pct"]
        == baseline["thresholds"]["screen.support_within_pct"]
    )
    assert (
        current["thresholds"]["screen.rsi_max"]
        == baseline["thresholds"]["screen.rsi_max"]
    )

    discovery_semantics = current["thresholds"]["screen.support_within_pct"][
        "semantics"
    ]
    assert discovery_semantics == "strong support must be within this distance"
    reserve_semantics = current_reserve["semantics"]
    assert "support_strength_min (moderate)" in reserve_semantics
    assert "independent_support_source_count_min (2)" in reserve_semantics
    assert "not discovery's strong support requirement" in reserve_semantics
    assert "failure of any other gate" in reserve_semantics
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
    assert add.max_add_symbols_per_market == 2
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
    assert priority.max_add_symbols_per_market == 2
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
        # §142차 (2026-08-23) — break-even band edge repair.
        "post_max_effective_anchor_operator": "max",
        "post_max_effective_anchor_operands": [
            "tick_ceil_post_max_anchor",
            (
                "first_valid_tick_strictly_above_average_cost_"
                "times_one_plus_breakeven_band"
            ),
        ],
        "post_max_effective_anchor_band_policy_key": (
            "order_proposals.auto_approve.breakeven_band_pct"
        ),
        "post_max_effective_anchor_reason": (
            "section_40_breakeven_band_comparison_is_inclusive"
        ),
        "post_max_effective_anchor_band_comparison_unchanged": True,
        "post_max_effective_anchor_since_policy_version": "2026-08-23.1",
        "post_max_effective_anchor_retroactive": False,
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
            # §142차 (2026-08-23) — the playbook's machine block carries the
            # same effective anchor as the policy tier, or a session reading
            # only the playbook would keep placing rungs on the band edge.
            "post_max_effective_anchor": {
                "operator": "max",
                "operands": [
                    "tick_ceil_post_max_anchor",
                    (
                        "first_valid_tick_strictly_above_average_cost_"
                        "times_one_plus_breakeven_band"
                    ),
                ],
                "band_policy_key": ("order_proposals.auto_approve.breakeven_band_pct"),
                "band_comparison_unchanged": True,
                "since_policy_version": "2026-08-23.1",
                "retroactive": False,
            },
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
    # §142차 (2026-08-23) KEY_DIFF — the effective post-max anchor keys are new
    # on sell.breakeven_reserve_trim and are now required by the schema, so the
    # baseline copy is given the current values before parsing. Assert they are
    # genuinely absent from the baseline first, then strip them from BOTH dumps
    # below so the remaining comparison stays a closed equivalence.
    baseline_reserve_trim = baseline_trim["tiers"][1]
    current_reserve_trim = current_trim["tiers"][1]
    assert baseline_reserve_trim["id"] == "sell.breakeven_reserve_trim"
    assert current_reserve_trim["id"] == "sell.breakeven_reserve_trim"
    for key in _S142_RESERVE_TRIM_ADDITIVE_CONDITION_KEYS:
        assert key not in baseline_reserve_trim["conditions"]
        baseline_reserve_trim["conditions"][key] = current_reserve_trim["conditions"][
            key
        ]
    # §136차 (2026-08-21) — A(k) 사이징/커버리지 델타. 현행 스키마 핀(k 0.20,
    # Literal[2], 신설 면제 키)이 baseline을 파싱할 수 있도록 사전 패치하되,
    # baseline 원값을 먼저 고정 확인한다.
    reserve_base = baseline["decision_rules"]["buy.support_reserve_net"]
    reserve_cur = current_raw["decision_rules"]["buy.support_reserve_net"]
    assert reserve_base["add_candidate"]["max_add_symbols_per_market"] == 1
    reserve_base["add_candidate"]["max_add_symbols_per_market"] = 2
    assert reserve_base["priority_rules"]["max_add_symbols_per_market"] == 1
    reserve_base["priority_rules"]["max_add_symbols_per_market"] = 2
    assert "owned_symbol_add_exempt_from_symbol_cap" not in reserve_base
    reserve_base["owned_symbol_add_exempt_from_symbol_cap"] = reserve_cur[
        "owned_symbol_add_exempt_from_symbol_cap"
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

    # §148차 (2026-08-24) — additive semantics-only clarification. The
    # contradiction repair is allowed to extend the prose, but it may not
    # change any threshold, value, or family enumeration. Normalize this one
    # field to the baseline after pinning the exact boundary language so the
    # closed-equivalence comparison remains exhaustive.
    baseline_reserve_net = baseline_dump["decision_rules"]["buy.support_reserve_net"]
    current_reserve_net = current_dump["decision_rules"]["buy.support_reserve_net"]
    assert (
        current_reserve_net["semantics"].startswith(baseline_reserve_net["semantics"])
        is False
    )
    assert "own support_strength_min (moderate)" in current_reserve_net["semantics"]
    assert (
        "independent_support_source_count_min (2)" in current_reserve_net["semantics"]
    )
    assert "not discovery's strong support" in current_reserve_net["semantics"]
    assert "failure of any other gate" in current_reserve_net["semantics"]
    current_reserve_net["semantics"] = baseline_reserve_net["semantics"]
    del current_dump["decision_rules"]["buy.preplanned_support_ladder"]
    del current_dump["crash_day"]["actions"]["new_entry_hold_exception"]
    del baseline_dump["decision_rules"]["buy.preplanned_support_ladder"]
    del baseline_dump["crash_day"]["actions"]["new_entry_hold_exception"]

    # §127차 (2026-08-21) — exactly two deltas: the additive
    # buy.winner_pullback_add rule, and the concurrent-new-entry slot count
    # inside the stance prose moving 1→2. Strip those and nothing else, and
    # pin both sides' wording so a silent rewrite of either fails here.
    assert "buy.winner_pullback_add" not in baseline_dump["decision_rules"]
    assert current_dump["decision_rules"]["buy.winner_pullback_add"]["exclusions"] == [
        "breakout_chase",
        "market_order_momentum_add",
    ]
    del current_dump["decision_rules"]["buy.winner_pullback_add"]
    # §147차 (2026-08-24) — the stance prose delta is now an ABOLITION, not a
    # raise: the baseline's count limit is gone entirely and orderable cash is
    # the only bound. Pin both the removal and the fact that the quality and
    # diversification devices are still named in the same sentence, so a
    # rewrite that quietly drops them fails here.
    assert "동시 신규 최대 1종목" in baseline_dump["user_stances"][0]["implications"][1]
    current_stance = current_dump["user_stances"][0]["implications"][1]
    assert "동시 신규 종목 수 제한 없음(§147차 2026-08-24 철폐" in current_stance
    assert "상한은 주문가능 현금뿐" in current_stance
    # the count limit is gone in BOTH of its historical forms
    assert "동시 신규 최대 2종목" not in current_stance
    assert "동시 신규 최대 1종목" not in current_stance
    # ...and the non-count guards are still asserted by the same clause
    assert "섹터 클러스터 집중도" in current_stance
    assert "테마당 종목 수 캡" in current_stance
    assert "notional 밴드" in current_stance
    current_dump["user_stances"][0]["implications"][1] = baseline_dump["user_stances"][
        0
    ]["implications"][1]

    # §129차 (2026-08-21) → §147차 (2026-08-24) — REVERSE-APPLIED. §129차 added
    # buy.new_entry_overflow as an additive delta over the ROB-1289 baseline;
    # §147차 deleted it, because its only reason to exist was to relieve the
    # slot count that §147차 abolished. The baseline never had the rule and the
    # current document no longer has it, so the two sides now agree here with
    # nothing to strip. Pin the absence on BOTH sides so a silent
    # re-introduction fails this closed-equivalence test rather than shipping.
    assert "buy.new_entry_overflow" not in baseline_dump["decision_rules"]
    assert "buy.new_entry_overflow" not in current_dump["decision_rules"]

    # §139차 (2026-08-22) — two additive decision rules and exactly one value
    # delta outside them. Pin the load-bearing fields of both new rules here so
    # a later silent rewrite (a waived gate, a raised cap, a dropped
    # retirement bar) fails this closed-equivalence test rather than shipping.
    assert "buy.index_etf_candidate" not in baseline_dump["decision_rules"]
    etf = current_dump["decision_rules"]["buy.index_etf_candidate"]
    assert etf["exclusions"] == ["leveraged_etf", "inverse_etf"]
    assert etf["tiers"][0]["conditions"]["idle_cash_allocation_rule"] is False
    assert etf["tiers"][0]["conditions"]["promoted_when_candidate_set_empty"] is False
    del current_dump["decision_rules"]["buy.index_etf_candidate"]

    assert "buy.held_majors_support_net" not in baseline_dump["decision_rules"]
    net = current_dump["decision_rules"]["buy.held_majors_support_net"]
    assert net["exclusions"] == [
        "new_coin_entry",
        "unheld_symbol",
        "losing_position_averaging_down",
        "market_order",
        "crash_day_new_batch",
    ]
    net_conditions = net["tiers"][0]["conditions"]
    assert net_conditions["max_notional_krw_per_coin"] == 300000
    assert net_conditions["max_notional_krw_per_tier"] == 900000
    assert net_conditions["review_date"] == "2026-09-19"
    del current_dump["decision_rules"]["buy.held_majors_support_net"]

    normalized_current_dump = deepcopy(current_dump)
    for keys, baseline_value, current_value in _S139_ALLOWED_POLICY_DELTAS:
        assert _policy_keys_get(baseline_dump, keys) == baseline_value
        assert _policy_keys_get(current_dump, keys) == current_value
        _policy_keys_set(normalized_current_dump, keys, baseline_value)
    for path, baseline_value, current_value in _ROB1292_ALLOWED_POLICY_DELTAS:
        assert _policy_path_get(baseline_dump, path) == baseline_value
        assert _policy_path_get(current_dump, path) == current_value
        _policy_path_set(normalized_current_dump, path, baseline_value)

    # §156차 scope addendum ④⑤ — the sector cap's value and keyset remain
    # stable, but its reserve-net admission role is explicitly relaxed to an
    # emitted/persisted advisory.  Normalize only those three truthful prose
    # deltas after pinning them, so the surrounding closed comparison still
    # catches every unrelated policy drift.
    current_authority = normalized_current_dump["authority"]
    baseline_authority = baseline_dump["authority"]
    assert (
        "observation-only sector-cluster concentration signal"
        in (current_authority["governs"])
    )
    current_authority["governs"] = baseline_authority["governs"]
    current_sector_cap = normalized_current_dump["thresholds"][
        "portfolio.sector_cluster_cap_pct"
    ]
    baseline_sector_cap = baseline_dump["thresholds"][
        "portfolio.sector_cluster_cap_pct"
    ]
    assert current_sector_cap["value"] == baseline_sector_cap["value"] == 10
    assert "advisory only" in current_sector_cap["semantics"]
    current_sector_cap["semantics"] = baseline_sector_cap["semantics"]
    current_theme_cap = normalized_current_dump["thresholds"][
        "portfolio.max_symbols_per_theme"
    ]
    baseline_theme_cap = baseline_dump["thresholds"]["portfolio.max_symbols_per_theme"]
    assert current_theme_cap["value"] == baseline_theme_cap["value"] == 2
    assert "sector concentration is surfaced" in current_theme_cap["semantics"]
    current_theme_cap["semantics"] = baseline_theme_cap["semantics"]

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
    # §142차 (2026-08-23) — the reserve-trim tier gains exactly the seven
    # enumerated additive condition keys and nothing else. Pin their values
    # here (a silent re-point of the band key or a back-dated stamp fails) and
    # strip them; every other key on that tier must still match the baseline.
    for dump in (normalized_current_dump, baseline_dump):
        s142_tier = dump["decision_rules"]["sell.trim_preplace"]["tiers"][1]
        assert s142_tier["id"] == "sell.breakeven_reserve_trim"
        conditions = s142_tier["conditions"]
        assert conditions["post_max_effective_anchor_operator"] == "max"
        assert conditions["post_max_effective_anchor_band_policy_key"] == (
            "order_proposals.auto_approve.breakeven_band_pct"
        )
        assert (
            conditions["post_max_effective_anchor_since_policy_version"]
            == "2026-08-23.1"
        )
        assert conditions["post_max_effective_anchor_retroactive"] is False
        for key in _S142_RESERVE_TRIM_ADDITIVE_CONDITION_KEYS:
            del conditions[key]

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
    # §139차 — the absolute ceiling is now a fat-finger guard only (BRK.A /
    # NVR class), not a risk cap; cash and the per-order auto-approve cap are.
    assert exception.absolute_ceiling_usd == 10000
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

    # §142차 — rung 1 only: clear the inclusive §40차 break-even band edge.
    assert conditions["tier1_effective_anchor_scope"] == "lowest_rung_only"
    rungs[0] = _band_clearing_effective_anchor(
        conditions,
        prefix="tier1_effective_anchor",
        market=market,
        average_cost=average_cost,
        tick_ceiled_raw_anchor=rungs[0],
        raw_operand="tick_ceil_average_cost_times_lowest_multiple",
    )
    return rungs


def _breakeven_extension_rung_one_before_s142(
    tier,
    *,
    market: str,
    average_cost: Decimal,
) -> Decimal:
    """The pre-§142차 rung 1: tick_ceil(average_cost × lowest multiple)."""

    multiple = Decimal(str(tier.conditions["anchor_average_cost_multiples"][0]))
    raw = average_cost * multiple
    tick = _market_tick(market, raw)
    return (raw / tick).to_integral_value(rounding=ROUND_CEILING) * tick


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
    # §142차 adds the enumerated effective-anchor keys to the reserve-trim
    # tier; strip exactly those and the tier is still byte-identical to the
    # ROB-1289 baseline (no threshold, operand, or gate key moved).
    reserve_trim = deepcopy(current_trim["tiers"][1])
    for key in _S142_RESERVE_TRIM_ADDITIVE_CONDITION_KEYS:
        del reserve_trim["conditions"][key]
    assert reserve_trim == baseline_trim["tiers"][1]


# ---------------------------------------------------------------------------
# §142차 (2026-08-23) — break-even band edge repair (versioned bugfix)
# ---------------------------------------------------------------------------

# The three boundary cases: an average cost whose × 1.01 lands EXACTLY on the
# market tick grid, so the ceil snap is a no-op and the rung sits on the
# inclusive §40차 band edge. The crypto row is the verifier's measured case
# (checkpoint #3 packet §4-1: avg 4,000,000 -> raw 4,040,000 -> ceil no-op).
_S142_BOUNDARY_CASES = (
    ("crypto", Decimal("4000000"), Decimal("4040000"), Decimal("4041000")),
    ("kr", Decimal("50000"), Decimal("50500"), Decimal("50600")),
    ("us", Decimal("100.00"), Decimal("101.00"), Decimal("101.01")),
)


def _expanded_limits(doc: TradingPolicyDocument) -> AutoApproveLimits:
    """§40차 classifier inputs, read from the shipped policy (not literals)."""

    auto_approve = _raw()["order_proposals"]["auto_approve"]
    return AutoApproveLimits(
        min_distance_pct=Decimal(str(auto_approve["min_distance_pct"])),
        per_order_cap=Decimal("1000000000"),
        daily_cap=Decimal("1000000000"),
        policy_version=doc.version,
        mode="expanded",
        breakeven_band_pct=Decimal(str(auto_approve["breakeven_band_pct"])),
        round_trip_cost_bps=Decimal("90"),
    )


@pytest.mark.parametrize(
    ("market", "average_cost", "pre_s142_rung_one", "expected_rung_one"),
    _S142_BOUNDARY_CASES,
    ids=[case[0] for case in _S142_BOUNDARY_CASES],
)
def test_s142_boundary_rung_one_is_lifted_off_the_inclusive_band_edge(
    market, average_cost, pre_s142_rung_one, expected_rung_one
):
    """AC — the defect reproduces, and the repair moves rung 1 one tick up."""

    tier = _breakeven_extension_ladder_tier()

    # The defect: before §142차 the ceil snap is a no-op on these averages, so
    # rung 1 IS the band edge.
    assert (
        _breakeven_extension_rung_one_before_s142(
            tier, market=market, average_cost=average_cost
        )
        == pre_s142_rung_one
    )
    band_pct = _breakeven_band_pct("order_proposals.auto_approve.breakeven_band_pct")
    assert pre_s142_rung_one == average_cost * (
        Decimal("1") + band_pct / Decimal("100")
    )

    # The repair: the effective rung 1 is the first valid tick strictly above.
    rungs = _breakeven_extension_rungs(tier, market=market, average_cost=average_cost)
    assert rungs[0] == expected_rung_one
    assert rungs[0] > pre_s142_rung_one
    # Lifted by exactly one tick -- the repair buys the minimum it needs.
    assert rungs[0] - pre_s142_rung_one == _market_tick(market, pre_s142_rung_one)
    # Rungs 2 and 3 are untouched and the ladder stays strictly ascending.
    assert rungs == sorted(set(rungs))
    assert (
        rungs[1]
        == _breakeven_extension_rungs(tier, market=market, average_cost=average_cost)[1]
    )


@pytest.mark.parametrize(
    ("market", "average_cost", "pre_s142_rung_one", "expected_rung_one"),
    _S142_BOUNDARY_CASES,
    ids=[case[0] for case in _S142_BOUNDARY_CASES],
)
def test_s142_boundary_rung_one_flips_breakeven_band_to_take_profit(
    market, average_cost, pre_s142_rung_one, expected_rung_one
):
    """The whole point: rung 1 can now satisfy its own submission_contract.

    Run against the real §40차 classifier, not a re-implementation.
    """

    doc = TradingPolicyDocument.model_validate(_raw())
    limits = _expanded_limits(doc)
    preview = {"avg_buy_price": str(average_cost)}

    before = classify_sell_profit(
        limit_price=pre_s142_rung_one,
        quantity=Decimal("100"),
        preview=preview,
        limits=limits,
    )
    assert before.verdict == "breakeven_band"

    after = classify_sell_profit(
        limit_price=expected_rung_one,
        quantity=Decimal("100"),
        preview=preview,
        limits=limits,
    )
    assert after.verdict == "take_profit"


def test_s142_leaves_the_inclusive_band_comparison_alone():
    """🔴 The global ``<=`` is NOT changed -- only the rung moved.

    A sell priced exactly on the band edge still classifies as breakeven_band
    for every other consumer of the classifier. If this flips, the repair has
    been widened past the §142차 authorization.
    """

    doc = TradingPolicyDocument.model_validate(_raw())
    limits = _expanded_limits(doc)
    average_cost = Decimal("4000000")
    band_edge = average_cost * (
        Decimal("1") + limits.breakeven_band_pct / Decimal("100")
    )

    for probe in (band_edge, average_cost - (band_edge - average_cost)):
        verdict = classify_sell_profit(
            limit_price=probe,
            quantity=Decimal("1"),
            preview={"avg_buy_price": str(average_cost)},
            limits=limits,
        )
        assert verdict.verdict == "breakeven_band"

    source = (
        Path(policy_schema.__file__).resolve().parents[2]
        / "app"
        / "services"
        / "order_proposals"
        / "auto_approve.py"
    ).read_text(encoding="utf-8")
    assert "if abs(distance_from_avg) <= band:" in source


@pytest.mark.parametrize(
    ("market", "average_cost", "expected"),
    [
        # §115차 AC1 (ETH) and the KR/US cases: raw × 1.01 is NOT on the grid,
        # so the ceil already cleared the band and §142차 changes nothing.
        ("crypto", Decimal("3168337"), Decimal("3201000")),
        ("kr", Decimal("71300"), Decimal("72100")),
        ("us", Decimal("187.42"), Decimal("189.30")),
    ],
    ids=["crypto_eth", "kr", "us"],
)
def test_s142_non_boundary_rung_one_is_unchanged(market, average_cost, expected):
    tier = _breakeven_extension_ladder_tier()
    before = _breakeven_extension_rung_one_before_s142(
        tier, market=market, average_cost=average_cost
    )
    after = _breakeven_extension_rungs(tier, market=market, average_cost=average_cost)[
        0
    ]

    assert before == after == expected
    band_pct = _breakeven_band_pct("order_proposals.auto_approve.breakeven_band_pct")
    assert after > average_cost * (Decimal("1") + band_pct / Decimal("100"))


def test_s142_reserve_trim_post_max_anchor_has_the_same_conflict_and_repair():
    """MEASURED, then repaired -- the §44차 tier shares the defect.

    Its post-max anchor is max(average_cost × loss_guard, d7 lowest price). When
    the guard operand wins, the anchor IS average_cost × 1.01, i.e. the band
    edge, and the ceil snap is a no-op on a grid-aligned average.
    """

    doc = TradingPolicyDocument.model_validate(_raw())
    tier = _breakeven_reserve_trim_tier()
    limits = _expanded_limits(doc)
    average_cost = Decimal("4000000")
    tick = _market_tick("crypto", average_cost * Decimal("1.01"))

    # Guard operand wins the max (D7 price is below it).
    anchor = _breakeven_reserve_trim_anchor(
        tier,
        average_cost=average_cost,
        d7_compliant_lowest_price=Decimal("4010000"),
    )
    snapped = _breakeven_reserve_trim_post_max_tick_snap(
        tier, anchor=anchor, tick_size=tick
    )
    assert snapped == Decimal("4040000")  # ceil is a no-op here
    assert (
        classify_sell_profit(
            limit_price=snapped,
            quantity=Decimal("1"),
            preview={"avg_buy_price": str(average_cost)},
            limits=limits,
        ).verdict
        == "breakeven_band"
    )

    effective = _band_clearing_effective_anchor(
        tier.conditions,
        prefix="post_max_effective_anchor",
        market="crypto",
        average_cost=average_cost,
        tick_ceiled_raw_anchor=snapped,
        raw_operand="tick_ceil_post_max_anchor",
    )
    assert effective == Decimal("4041000")
    assert effective - snapped == tick
    assert (
        classify_sell_profit(
            limit_price=effective,
            quantity=Decimal("1"),
            preview={"avg_buy_price": str(average_cost)},
            limits=limits,
        ).verdict
        == "take_profit"
    )


def test_s142_reserve_trim_band_operand_is_inert_when_the_d7_floor_wins():
    """No anchor is dragged upward when it already clears the band."""

    tier = _breakeven_reserve_trim_tier()
    average_cost = Decimal("4000000")
    tick = Decimal("1000")

    anchor = _breakeven_reserve_trim_anchor(
        tier,
        average_cost=average_cost,
        d7_compliant_lowest_price=Decimal("4200000"),
    )
    snapped = _breakeven_reserve_trim_post_max_tick_snap(
        tier, anchor=anchor, tick_size=tick
    )
    effective = _band_clearing_effective_anchor(
        tier.conditions,
        prefix="post_max_effective_anchor",
        market="crypto",
        average_cost=average_cost,
        tick_ceiled_raw_anchor=snapped,
        raw_operand="tick_ceil_post_max_anchor",
    )

    assert snapped == Decimal("4200000")
    assert effective == snapped


def test_s142_is_declared_versioned_and_not_retroactive():
    """The bugfix is stamped, and it never re-anchors an older placement."""

    doc = TradingPolicyDocument.model_validate(_raw())
    assert doc.version == "2026-08-26.3"
    assert "§142차 breakeven band boundary repair 2026-08-23" in doc.source
    assert "NOT retroactive" in doc.source

    ladder = _breakeven_extension_ladder_tier().conditions
    reserve = _breakeven_reserve_trim_tier().conditions
    assert ladder["tier1_effective_anchor_since_policy_version"] == "2026-08-23.1"
    assert ladder["tier1_effective_anchor_retroactive"] is False
    assert reserve["post_max_effective_anchor_since_policy_version"] == "2026-08-23.1"
    assert reserve["post_max_effective_anchor_retroactive"] is False


def test_s142_adds_no_tier_no_threshold_and_no_sizing_change():
    """The permitted class is contradiction resolution -- nothing else."""

    baseline = yaml.safe_load(_ROB1289_BASELINE.read_text(encoding="utf-8"))
    current = _raw()

    for key in (
        "sell.loss_guard_min_multiple",
        "sell.breakeven_near_pct",
        "sell.trim_min_expected_net_realized_gain_krw",
    ):
        assert current["thresholds"][key] == baseline["thresholds"][key]
    assert (
        current["order_proposals"]["auto_approve"]["breakeven_band_pct"]
        == baseline["order_proposals"]["auto_approve"]["breakeven_band_pct"]
    )

    current_trim = current["decision_rules"]["sell.trim_preplace"]
    assert [tier["id"] for tier in current_trim["tiers"]] == [
        "de_minimis_trim_watch",
        "sell.breakeven_reserve_trim",
        "single_share_full_exit_review",
        "momentum_spike_profit_ladder",
        "rsi_confirmed_resistance",
        "ultra_near_resistance",
        "watch_zone",
        "breakeven_extension_ladder",
    ]
    for tier in current_trim["tiers"]:
        if tier["id"] in ("sell.breakeven_reserve_trim", "breakeven_extension_ladder"):
            assert tier["sizing"] == "existing_trim_rule"
    assert current_trim["exclusions"] == ["no_resistance_reference", "composite_gates"]
    # The ladder's own multiples are untouched: the repair moves the effective
    # rung, not the declared multiple.
    ladder = current_trim["tiers"][-1]["conditions"]
    assert ladder["anchor_average_cost_multiples"] == [1.01, 1.05, 1.10]
    assert ladder["anchor_lowest_rung_policy_key"] == "sell.loss_guard_min_multiple"


def _s142_mutant(tier_index: int, key: str, value) -> dict:
    raw = _raw()
    raw["decision_rules"]["sell.trim_preplace"]["tiers"][tier_index]["conditions"][
        key
    ] = value
    return raw


def _s142_dropped(tier_index: int, key: str) -> dict:
    raw = _raw()
    del raw["decision_rules"]["sell.trim_preplace"]["tiers"][tier_index]["conditions"][
        key
    ]
    return raw


@pytest.mark.parametrize(
    "build",
    [
        lambda: _s142_dropped(-1, "tier1_effective_anchor_operands"),
        lambda: _s142_mutant(-1, "tier1_effective_anchor_operator", "min"),
        lambda: _s142_mutant(
            -1,
            "tier1_effective_anchor_operands",
            ["tick_ceil_average_cost_times_lowest_multiple"],
        ),
        lambda: _s142_mutant(
            -1, "tier1_effective_anchor_band_policy_key", "sell.breakeven_near_pct"
        ),
        lambda: _s142_mutant(-1, "tier1_effective_anchor_retroactive", True),
        lambda: _s142_mutant(
            -1, "tier1_effective_anchor_since_policy_version", "2026-08-22.1"
        ),
        lambda: _s142_mutant(
            -1, "tier1_effective_anchor_band_comparison_unchanged", False
        ),
        lambda: _s142_mutant(-1, "tier1_effective_anchor_scope", "all_rungs"),
        lambda: _s142_dropped(1, "post_max_effective_anchor_operator"),
        lambda: _s142_mutant(1, "post_max_effective_anchor_operator", "min"),
        lambda: _s142_mutant(
            1,
            "post_max_effective_anchor_operands",
            [
                "first_valid_tick_strictly_above_average_cost_"
                "times_one_plus_breakeven_band",
                "tick_ceil_post_max_anchor",
            ],
        ),
        lambda: _s142_mutant(1, "post_max_effective_anchor_retroactive", True),
    ],
    ids=[
        "ladder_operands_dropped",
        "ladder_operator_min",
        "ladder_band_operand_removed",
        "ladder_band_key_repointed",
        "ladder_claims_retroactive",
        "ladder_backdated_stamp",
        "ladder_claims_comparison_changed",
        "ladder_scope_widened",
        "reserve_operator_dropped",
        "reserve_operator_min",
        "reserve_operands_reordered",
        "reserve_claims_retroactive",
    ],
)
def test_s142_mutants_are_rejected(build):
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(build())


# ---------------------------------------------------------------------------
# §139차 (2026-08-22) — buy-side retrospective decisions ⓐ/ⓑ/ⓒ
# ---------------------------------------------------------------------------

_ETF_TIER_ID = "index_etf_candidate"
_HELD_MAJORS_TIER_ID = "held_majors_support_net"
_ETF_RULE_KEY = "buy.index_etf_candidate"
_HELD_MAJORS_RULE_KEY = "buy.held_majors_support_net"


def _rule_mutant(rule_key: str, mutate) -> dict:
    raw = _raw()
    mutate(raw["decision_rules"][rule_key])
    return raw


def test_s139_one_share_ceiling_is_raised_but_the_gate_is_still_armed():
    """ⓐ — the ceiling moves; the exception itself does not become unbounded.

    The operator's boundary is cash plus the per-order auto-approve cap, so
    the two keys that express that boundary must be unchanged by this edit.
    """

    doc = TradingPolicyDocument.model_validate(_raw())
    exception = doc.thresholds["buy.per_symbol_notional_usd_range"].one_share_exception

    assert exception is not None
    assert exception.enabled is True
    assert exception.absolute_ceiling_usd == 10000
    # Averaging-down exposure on an exception entry is NOT widened alongside it.
    assert exception.max_deep_rungs == 1
    # The real boundary: the USD per-order auto-approve cap is untouched, so a
    # single share above it still routes to manual approval rather than auto.
    assert doc.order_proposals.auto_approve.per_order_cap["us"] == 1500
    # And the standard tranche band is unchanged — this is an exception path,
    # not a new default size.
    assert doc.thresholds["buy.per_symbol_notional_usd_range"].value == [150, 450]


def test_s139_index_etf_candidate_parses_as_an_equal_candidate():
    """ⓑ — admission to the universe, not an allocation rule."""

    doc = TradingPolicyDocument.model_validate(_raw())
    rule = doc.decision_rules[_ETF_RULE_KEY]

    assert isinstance(rule, policy_schema.PolicyDecisionRule)
    assert rule.lanes == ["buy", "discovery"]
    assert rule.markets == ["kr", "us"]
    assert [tier.id for tier in rule.tiers] == [_ETF_TIER_ID]
    assert rule.exclusions == ["leveraged_etf", "inverse_etf"]

    conditions = rule.tiers[0].conditions
    # The rejected allocation form stays rejected.
    assert conditions["idle_cash_allocation_rule"] is False
    assert conditions["slot_reserved_for_etf"] is False
    assert conditions["promoted_when_candidate_set_empty"] is False
    assert conditions["etf_specific_sizing_multiplier"] is False
    assert conditions["ranked_against_equities_in_same_pool"] is True
    # Gates an ETF can satisfy stay on at the same thresholds.
    assert conditions["rsi_gate_applies"] is True
    assert conditions["support_strength_gate_applies"] is True
    assert conditions["support_distance_gate_applies"] is True
    # Gates it structurally cannot are not-applicable, never "waived".
    assert conditions["honest_upside_gate"] == "not_applicable_structurally_absent"
    assert conditions["analyst_gate"] == "not_applicable_structurally_absent"
    assert "EQUAL candidate" in rule.semantics
    assert "never an idle-cash fallback" in rule.semantics


def test_s139_index_etf_is_scoped_out_of_the_crypto_lane():
    from app.services.trading_policy_service import get_policy_for

    assert _ETF_RULE_KEY in get_policy_for("kr", "buy")["decision_rules"]
    assert _ETF_RULE_KEY in get_policy_for("us", "buy")["decision_rules"]
    assert _ETF_RULE_KEY in get_policy_for("kr", "discovery")["decision_rules"]
    assert _ETF_RULE_KEY not in get_policy_for("crypto", "buy")["decision_rules"]


def test_s139_held_majors_support_net_parses_as_a_bounded_time_boxed_tier():
    """ⓒ — the only LIVE tier here; every bound is asserted, not assumed."""

    doc = TradingPolicyDocument.model_validate(_raw())
    rule = doc.decision_rules[_HELD_MAJORS_RULE_KEY]

    assert isinstance(rule, policy_schema.PolicyDecisionRule)
    assert rule.lanes == ["buy"]
    assert rule.markets == ["crypto"]
    assert [tier.id for tier in rule.tiers] == [_HELD_MAJORS_TIER_ID]
    assert rule.exclusions == [
        "new_coin_entry",
        "unheld_symbol",
        "losing_position_averaging_down",
        "market_order",
        "crash_day_new_batch",
    ]

    conditions = rule.tiers[0].conditions
    # Scope: held + profitable only, so new-coin discovery is untouched.
    assert conditions["holding_required"] is True
    assert conditions["unrealized_pnl_pct_min_exclusive"] == 0
    assert conditions["new_coin_discovery_gate_unchanged"] is True
    # Anchor: moderate is the relaxation, >= 2 independent sources pay for it.
    assert conditions["support_strength_min"] == "moderate"
    assert conditions["independent_support_source_count_min"] == 2
    assert conditions["support_distance_from_current_pct_range"] == [-12, -3]
    # Execution: resting limit through the existing cap, GTC per Upbit.
    assert conditions["order_type"] == "limit"
    assert conditions["resting_only"] is True
    assert conditions["tif"] == "GTC"
    assert conditions["auto_approve_path"] == "existing_per_order_cap"
    assert conditions["per_order_cap_raised"] is False
    # Size: per-coin cap fits three placements inside the tier cap, and every
    # single order is far below the crypto per-order auto-approve cap.
    assert conditions["max_notional_krw_per_coin"] == 300000
    assert conditions["max_notional_krw_per_tier"] == 900000
    assert (
        conditions["max_placements_per_coin_per_support_level_per_policy_version"] == 1
    )
    assert (
        conditions["max_notional_krw_per_coin"]
        <= doc.order_proposals.auto_approve.per_order_cap["crypto"]
    )
    # Scoring: pre-registered, with the retirement bar fixed before the batches.
    assert conditions["forecast_save_required"] is True
    assert conditions["review_date"] == "2026-09-19"
    assert conditions["retire_unless_filled_cohort_d20_median_pct_min"] == 0
    assert (
        conditions["retire_unless_filled_cohort_d20_lower_quartile_pct_min_exclusive"]
        == -8
    )
    # Enforcement surface (B1): advisory, with the one real boundary named.
    assert conditions["enforcement_surface"] == "advisory_session_contract"
    assert (
        conditions["code_enforced_boundary"]
        == "crypto_per_order_auto_approve_cap_then_card"
    )
    assert conditions["major_classification"] == "session_judgment_no_machine_allowlist"
    # Crash regime: explicitly NOT the preplanned ladder's "keep".
    assert conditions["crash_day_new_batch_suspended"] is True
    assert conditions["crypto_crash_24h_drawdown_pct_max"] == -10
    ladder = doc.decision_rules["buy.preplanned_support_ladder"]
    assert isinstance(ladder, PreplannedSupportLadderPolicy)
    assert ladder.crash_day_behavior == "keep"


def test_s139_held_majors_semantics_records_the_counter_evidence():
    """A pre-registration that argues its counter-evidence away is not one."""

    doc = TradingPolicyDocument.model_validate(_raw())
    semantics = doc.decision_rules[_HELD_MAJORS_RULE_KEY].semantics

    yaml_text = _CONFIG.read_text(encoding="utf-8")
    start = yaml_text.index("# §139차 (2026-08-22) — crypto 보유 메이저")
    end = yaml_text.index("  sell.trim_preplace:", start)
    rule_block = yaml_text[start:end]

    # The measured evidence that argues AGAINST this tier is carried with it.
    assert "ROB-1031" in rule_block
    assert "60%" in rule_block
    assert "-8.36%" in rule_block
    assert "XRP" in rule_block
    # And the operator's own words, so the hypothesis is attributable.
    # (the quote wraps across two comment lines in the YAML)
    assert "상승장이라면" in rule_block
    assert "지지선에 매수 걸면 돈 벌 거잖아" in rule_block

    assert "2026-09-19" in semantics
    assert "RETIRED" in semantics


def test_s139_held_majors_semantics_does_not_overclaim_enforcement():
    """B1 — "LIVE" must not read as "this repo enforces it".

    Every tier in this file is advisory (``authority.scope:
    judgment_policy_only``). The caps, the once-per-level rule, the crash
    suspension, and the forecast obligation are a session contract carried by
    the operator prompt; the only boundary code actually enforces on a
    resulting order is the crypto per-order auto-approve cap.
    """

    doc = TradingPolicyDocument.model_validate(_raw())
    semantics = doc.decision_rules[_HELD_MAJORS_RULE_KEY].semantics

    assert "ENFORCEMENT SURFACE" in semantics
    assert "advisory" in semantics
    assert "judgment_policy_only" in doc.authority.scope
    assert "SESSION CONTRACT" in semantics
    assert "not a machine" in semantics
    assert '"LIVE" describes the funds, not an armed executor' in semantics
    # The operator-side counterpart is named, so the contract has an owner.
    assert "auto_trader-operator live/CLAUDE.md" in semantics
    # "Major" is not claimed to be machine-checked, because it is not.
    assert "no coin allowlist or classifier" in semantics
    # And the named boundary is the one that is really enforced in code.
    assert "5,000,000 KRW per-order cap" in semantics
    assert doc.order_proposals.auto_approve.per_order_cap["crypto"] == 5000000


def test_s139_held_majors_is_scoped_out_of_the_equity_lanes():
    from app.services.trading_policy_service import get_policy_for

    assert _HELD_MAJORS_RULE_KEY in get_policy_for("crypto", "buy")["decision_rules"]
    assert _HELD_MAJORS_RULE_KEY not in get_policy_for("kr", "buy")["decision_rules"]
    assert _HELD_MAJORS_RULE_KEY not in get_policy_for("us", "buy")["decision_rules"]


def test_s139_markets_scope_is_stripped_from_the_consumer_view():
    """``markets`` is scoping metadata, echoed no more than ``lanes`` is."""

    from app.services.trading_policy_service import get_policy_for

    for market, lane in (("kr", "buy"), ("us", "buy"), ("crypto", "buy")):
        for rule in get_policy_for(market, lane)["decision_rules"].values():
            assert "markets" not in rule
            assert "lanes" not in rule


def test_s139_pre_existing_rules_keep_the_all_markets_default():
    doc = TradingPolicyDocument.model_validate(_raw())

    for key in (
        "buy.support_reserve_net",
        "buy.preplanned_support_ladder",
        "buy.winner_pullback_add",
        "sell.trim_preplace",
    ):
        rule = doc.decision_rules[key]
        assert getattr(rule, "markets", None) is None


def test_s139_empty_markets_list_is_rejected():
    """``markets: []`` would silently disable a live tier in every market."""

    raw = _raw()
    raw["decision_rules"][_HELD_MAJORS_RULE_KEY]["markets"] = []
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def _mutate_etf_becomes_idle_cash_allocation(rule) -> None:
    rule["tiers"][0]["conditions"]["idle_cash_allocation_rule"] = True


def _mutate_etf_reserves_a_slot(rule) -> None:
    rule["tiers"][0]["conditions"]["slot_reserved_for_etf"] = True


def _mutate_etf_promoted_when_empty(rule) -> None:
    rule["tiers"][0]["conditions"]["promoted_when_candidate_set_empty"] = True


def _mutate_etf_waives_rsi(rule) -> None:
    rule["tiers"][0]["conditions"]["rsi_gate_applies"] = False


def _mutate_etf_waives_support_strength(rule) -> None:
    rule["tiers"][0]["conditions"]["support_strength_gate_applies"] = False


def _mutate_etf_upside_gate_waived_not_inapplicable(rule) -> None:
    rule["tiers"][0]["conditions"]["honest_upside_gate"] = "waived"


def _mutate_etf_drops_leverage_exclusion(rule) -> None:
    rule["exclusions"] = ["inverse_etf"]


def _mutate_etf_special_sizing(rule) -> None:
    rule["tiers"][0]["conditions"]["etf_specific_sizing_multiplier"] = True


def _mutate_etf_leaks_into_crypto(rule) -> None:
    rule["markets"] = ["kr", "us", "crypto"]


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_etf_becomes_idle_cash_allocation,
        _mutate_etf_reserves_a_slot,
        _mutate_etf_promoted_when_empty,
        _mutate_etf_waives_rsi,
        _mutate_etf_waives_support_strength,
        _mutate_etf_upside_gate_waived_not_inapplicable,
        _mutate_etf_drops_leverage_exclusion,
        _mutate_etf_special_sizing,
        _mutate_etf_leaks_into_crypto,
    ],
    ids=[
        "becomes_idle_cash_allocation",
        "reserves_a_slot",
        "promoted_when_candidate_set_empty",
        "waives_rsi_gate",
        "waives_support_strength_gate",
        "upside_gate_waived_instead_of_inapplicable",
        "drops_leveraged_etf_exclusion",
        "invents_etf_specific_sizing",
        "leaks_into_the_crypto_lane",
    ],
)
def test_s139_index_etf_candidate_mutants_are_rejected(mutate):
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(_rule_mutant(_ETF_RULE_KEY, mutate))


def _mutate_net_admits_unheld(rule) -> None:
    rule["tiers"][0]["conditions"]["holding_required"] = False


def _mutate_net_admits_losing_lot(rule) -> None:
    rule["tiers"][0]["conditions"]["unrealized_pnl_pct_min_exclusive"] = -5


def _mutate_net_touches_new_coin_gate(rule) -> None:
    rule["tiers"][0]["conditions"]["new_coin_discovery_gate_unchanged"] = False


def _mutate_net_drops_support_strength_to_weak(rule) -> None:
    rule["tiers"][0]["conditions"]["support_strength_min"] = "weak"


def _mutate_net_drops_to_single_source(rule) -> None:
    rule["tiers"][0]["conditions"]["independent_support_source_count_min"] = 1


def _mutate_net_widens_band(rule) -> None:
    rule["tiers"][0]["conditions"]["support_distance_from_current_pct_range"] = [-20, 0]


def _mutate_net_allows_market_order(rule) -> None:
    rule["tiers"][0]["conditions"]["order_type"] = "market"


def _mutate_net_raises_per_coin_cap(rule) -> None:
    rule["tiers"][0]["conditions"]["max_notional_krw_per_coin"] = 1000000


def _mutate_net_raises_tier_cap(rule) -> None:
    rule["tiers"][0]["conditions"]["max_notional_krw_per_tier"] = 3000000


def _mutate_net_allows_repeat_placements(rule) -> None:
    conditions = rule["tiers"][0]["conditions"]
    conditions["max_placements_per_coin_per_support_level_per_policy_version"] = 5


def _mutate_net_drops_forecast_obligation(rule) -> None:
    rule["tiers"][0]["conditions"]["forecast_save_required"] = False


def _mutate_net_softens_retirement_median(rule) -> None:
    rule["tiers"][0]["conditions"][
        "retire_unless_filled_cohort_d20_median_pct_min"
    ] = -5


def _mutate_net_softens_retirement_quartile(rule) -> None:
    conditions = rule["tiers"][0]["conditions"]
    conditions["retire_unless_filled_cohort_d20_lower_quartile_pct_min_exclusive"] = -30


def _mutate_net_drops_review_date(rule) -> None:
    del rule["tiers"][0]["conditions"]["review_date"]


def _mutate_net_keeps_batching_through_a_crash(rule) -> None:
    rule["tiers"][0]["conditions"]["crash_day_new_batch_suspended"] = False


def _mutate_net_loosens_crash_trigger(rule) -> None:
    rule["tiers"][0]["conditions"]["crypto_crash_24h_drawdown_pct_max"] = -30


def _mutate_net_raises_the_per_order_cap(rule) -> None:
    rule["tiers"][0]["conditions"]["per_order_cap_raised"] = True


def _mutate_net_drops_new_coin_exclusion(rule) -> None:
    rule["exclusions"] = [
        name for name in rule["exclusions"] if name != "new_coin_entry"
    ]


def _mutate_net_leaks_into_equity_lanes(rule) -> None:
    rule["markets"] = ["kr", "us", "crypto"]


def _mutate_net_claims_code_enforcement(rule) -> None:
    rule["tiers"][0]["conditions"]["enforcement_surface"] = "code_enforced"


def _mutate_net_drops_the_enforcement_surface(rule) -> None:
    del rule["tiers"][0]["conditions"]["enforcement_surface"]


def _mutate_net_claims_a_major_allowlist(rule) -> None:
    conditions = rule["tiers"][0]["conditions"]
    conditions["major_classification"] = "machine_allowlist_btc_eth_link"


def _mutate_net_renames_the_real_boundary(rule) -> None:
    rule["tiers"][0]["conditions"]["code_enforced_boundary"] = "tier_total_900000_krw"


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_net_admits_unheld,
        _mutate_net_admits_losing_lot,
        _mutate_net_touches_new_coin_gate,
        _mutate_net_drops_support_strength_to_weak,
        _mutate_net_drops_to_single_source,
        _mutate_net_widens_band,
        _mutate_net_allows_market_order,
        _mutate_net_raises_per_coin_cap,
        _mutate_net_raises_tier_cap,
        _mutate_net_allows_repeat_placements,
        _mutate_net_drops_forecast_obligation,
        _mutate_net_softens_retirement_median,
        _mutate_net_softens_retirement_quartile,
        _mutate_net_drops_review_date,
        _mutate_net_keeps_batching_through_a_crash,
        _mutate_net_loosens_crash_trigger,
        _mutate_net_raises_the_per_order_cap,
        _mutate_net_drops_new_coin_exclusion,
        _mutate_net_leaks_into_equity_lanes,
        _mutate_net_claims_code_enforcement,
        _mutate_net_drops_the_enforcement_surface,
        _mutate_net_claims_a_major_allowlist,
        _mutate_net_renames_the_real_boundary,
    ],
    ids=[
        "admits_an_unheld_coin",
        "admits_a_losing_lot",
        "claims_the_new_coin_gate_changed",
        "drops_support_strength_to_weak",
        "drops_to_a_single_support_source",
        "widens_the_anchor_band",
        "allows_a_market_order",
        "raises_the_per_coin_cap",
        "raises_the_tier_cap",
        "allows_repeat_placements",
        "drops_the_forecast_obligation",
        "softens_the_retirement_median",
        "softens_the_retirement_quartile",
        "drops_the_review_date",
        "keeps_batching_through_a_crash",
        "loosens_the_crash_trigger",
        "raises_the_per_order_cap",
        "drops_the_new_coin_exclusion",
        "leaks_into_the_equity_lanes",
        "claims_code_enforcement",
        "drops_the_enforcement_surface",
        "claims_a_machine_major_allowlist",
        "renames_the_real_code_boundary",
    ],
)
def test_s139_held_majors_support_net_mutants_are_rejected(mutate):
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(
            _rule_mutant(_HELD_MAJORS_RULE_KEY, mutate)
        )


@pytest.mark.parametrize("rule_key", [_ETF_RULE_KEY, _HELD_MAJORS_RULE_KEY])
def test_s139_renaming_a_tier_id_cannot_bypass_its_validators(rule_key):
    """B2 — the per-tier pins key off the tier id; the key binds it.

    Before this binding, renaming the tier while keeping the rule key made
    every §139차 validator return early, so a policy surface still named
    ``buy.held_majors_support_net`` could carry an unpinned tier.
    """

    raw = _raw()
    rule = raw["decision_rules"][rule_key]
    original_id = rule["tiers"][0]["id"]
    rule["tiers"][0]["id"] = f"{original_id}_v2"

    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_s139_new_rules_reject_typo_keys():
    for key in (_ETF_RULE_KEY, _HELD_MAJORS_RULE_KEY):
        raw = _raw()
        raw["decision_rules"][key]["bogus"] = True
        with pytest.raises(ValidationError):
            TradingPolicyDocument.model_validate(raw)


def test_s139_leaves_the_crypto_and_kr_approval_caps_untouched():
    """No cap is raised by §139차; the tier rides the existing approval lane."""

    baseline = yaml.safe_load(_ROB1289_BASELINE.read_text(encoding="utf-8"))
    current = _raw()

    current_auto = deepcopy(current["order_proposals"]["auto_approve"])
    baseline_auto = deepcopy(baseline["order_proposals"]["auto_approve"])
    for path, baseline_value, _current_value in _ROB1292_ALLOWED_POLICY_DELTAS:
        suffix = path.removeprefix("order_proposals.auto_approve.")
        _policy_path_set(current_auto, suffix, baseline_value)
    assert current_auto == baseline_auto

    # The new-coin discovery gates §139차 promises not to touch.
    for key in (
        "buy.deep_limit_pct_range",
        "buy.per_symbol_notional_krw_range",
        "sell.loss_guard_min_multiple",
    ):
        assert current["thresholds"][key] == baseline["thresholds"][key]
    assert (
        current["market_rules"]["crypto"]["no_chasing"]
        == baseline["market_rules"]["crypto"]["no_chasing"]
    )
    assert (
        current["market_rules"]["crypto"]["recovery_gate"]
        == baseline["market_rules"]["crypto"]["recovery_gate"]
    )


# ---------------------------------------------------------------------------
# §147차 (2026-08-24) — concurrent-new-entry slot limit ABOLISHED.
#
# This change removes a COUNT limit and nothing else. The tests below are the
# machine proof of that claim: they pin every quality and diversification
# device the operator ledger declared invariant, so a future edit that rides
# along on "§147차 removed a limit" and quietly relaxes a gate, widens a
# notional band, or lifts a concentration cap fails here instead of shipping.
# ---------------------------------------------------------------------------

# The four buy gates named as invariant in the §147차 ledger entry, with the
# values they held BEFORE the slot limit was abolished.
_S147_INVARIANT_BUY_GATES = {
    "screen.rsi_max": 45,  # RSI gate
    "screen.support_within_pct": 8,  # 지지 gate
    "screen.upside_min_pct": 40,  # upside gate
    "buy.deep_limit_pct_range": [-12, -3],  # 딥밴드 gate
}

# Sizing and concentration values declared invariant in the same entry.  §156
# later changes only the sector cap's reserve-net admission role, not its key
# or numeric value.
_S147_INVARIANT_SIZING_AND_CAPS = {
    "buy.per_symbol_notional_krw_range": [200000, 400000],  # KR 20~40만
    "buy.per_symbol_notional_usd_range": [150, 450],
    "portfolio.sector_cluster_cap_pct": 10,  # 섹터 클러스터 10% 캡
    "portfolio.max_symbols_per_theme": 2,  # 테마당 2종목 캡
}


def test_s147_buy_gates_are_unchanged():
    """Abolishing a count limit must not touch a single buy gate."""

    doc = TradingPolicyDocument.model_validate(_raw())
    for key, expected in _S147_INVARIANT_BUY_GATES.items():
        assert doc.thresholds[key].value == expected, key


def test_s147_notional_bands_and_concentration_values_are_unchanged():
    """The values survive even though §156 makes the sector cap advisory."""

    doc = TradingPolicyDocument.model_validate(_raw())
    for key, expected in _S147_INVARIANT_SIZING_AND_CAPS.items():
        assert doc.thresholds[key].value == expected, key


def test_s147_invariants_match_the_rob1289_baseline_exactly():
    """The strongest form: none of the eight invariants ever moved at all.

    The ROB-1289 baseline predates §127차, §129차 and §147차, so if every one
    of these keys still equals its baseline value then the whole slot-limit
    lineage — introduction, overflow, and abolition — provably never touched a
    gate, a notional band, or a concentration cap.
    """

    baseline = yaml.safe_load(_ROB1289_BASELINE.read_text(encoding="utf-8"))
    current = _raw()
    for key in {**_S147_INVARIANT_BUY_GATES, **_S147_INVARIANT_SIZING_AND_CAPS}:
        assert (
            current["thresholds"][key]["value"] == baseline["thresholds"][key]["value"]
        ), key

    # The only non-``value`` differences are the §139차 US one-share ceiling
    # and §156's explicitly recorded sector-cap semantics.  Both are pinned
    # rather than ignored, so §147차 cannot be used as cover for a new sibling
    # key drift.
    for key in {**_S147_INVARIANT_BUY_GATES, **_S147_INVARIANT_SIZING_AND_CAPS}:
        cur = deepcopy(current["thresholds"][key])
        base = deepcopy(baseline["thresholds"][key])
        if key == "buy.per_symbol_notional_usd_range":
            assert cur["one_share_exception"]["absolute_ceiling_usd"] == 10000
            assert base["one_share_exception"]["absolute_ceiling_usd"] == 700
            cur["one_share_exception"] = base["one_share_exception"]
        if key == "portfolio.sector_cluster_cap_pct":
            assert "advisory only" in cur["semantics"]
            cur["semantics"] = base["semantics"]
        if key == "portfolio.max_symbols_per_theme":
            assert "sector concentration is surfaced" in cur["semantics"]
            cur["semantics"] = base["semantics"]
        assert cur == base, key


def test_s147_new_entry_overflow_rule_is_deleted_from_the_document():
    """§129차's rule is gone — its only purpose was to relieve the slot count."""

    doc = TradingPolicyDocument.model_validate(_raw())
    assert "buy.new_entry_overflow" not in doc.decision_rules
    # and the tier id it carried is gone with it
    for rule in doc.decision_rules.values():
        for tier in getattr(rule, "tiers", None) or []:
            assert getattr(tier, "id", None) != "new_entry_overflow"


def test_s147_new_entry_overflow_is_absent_from_every_consumer_view():
    """No market/lane view may still echo the deleted rule."""

    from app.services.trading_policy_service import get_policy_for

    for market in ("kr", "us", "crypto"):
        for lane in ("buy", "sell"):
            view = get_policy_for(market, lane)
            assert "buy.new_entry_overflow" not in view["decision_rules"]


def test_s147_no_code_consumes_the_deleted_rule():
    """A policy key may only be deleted if nothing in the runtime reads it.

    Deleting a rule while leaving a consumer behind would break the runtime,
    so this pins the precondition that made the deletion safe.
    """

    repo_root = Path(__file__).resolve().parents[2]
    offenders = []
    for pkg in ("app", "scripts"):
        for path in (repo_root / pkg).rglob("*.py"):
            if "new_entry_overflow" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(repo_root)))
    assert offenders == []


def test_s147_stance_makes_cash_the_only_bound_and_keeps_the_other_guards():
    """The stance prose states the abolition and still names the guards."""

    doc = TradingPolicyDocument.model_validate(_raw())
    stance = doc.user_stances[0].implications[1]
    assert "동시 신규 종목 수 제한 없음(§147차 2026-08-24 철폐" in stance
    assert "상한은 주문가능 현금뿐" in stance
    # neither historical count survives anywhere in the stance block
    for implication in doc.user_stances[0].implications:
        assert "동시 신규 최대" not in implication
    # the non-count guards are still asserted in the same clause
    for fragment in ("notional 밴드", "섹터 클러스터 집중도", "테마당 종목 수 캡"):
        assert fragment in stance, fragment


def test_s147_source_records_the_abolition_and_the_q4_tension():
    """Provenance is append-only and carries the ledger's honest Q4 record."""

    doc = TradingPolicyDocument.model_validate(_raw())
    assert doc.version == "2026-08-26.3"
    assert "§147차 concurrent-new-entry slot limit ABOLISHED 2026-08-24" in doc.source
    assert "bounded by ORDERABLE CASH ALONE" in doc.source
    # the §129차 provenance is NOT rewritten out of history
    assert "§129차 buy.new_entry_overflow" in doc.source
    # the Q4 tension is recorded rather than glossed over
    assert "no relaxation before scoring" in doc.source
    assert "COUNT limit only" in doc.source
    assert "directly reverses the §147 assertion" in doc.source
