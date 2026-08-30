from __future__ import annotations

from copy import deepcopy

from app.services.buy_gate_ab_shadow.evaluate import (
    A_SUPPORT_MIN,
    B_SUPPORT_MIN,
    OTHER_GATE_KEYS,
    RSI_MAX,
    SUPPORT_WITHIN_PCT,
    UPSIDE_MIN_PCT,
)
from app.services.buy_gate_ab_shadow.spec import (
    ACTIVATION_EPOCH_ADDENDUM,
    ACTIVATION_EPOCH_ADDENDUM_VERSION,
    BASE_PRE_REGISTRATION,
    BASE_PRE_REGISTRATION_SHA256,
    EXPERIMENT_ID,
    FORBIDDEN,
    PINNED_POLICY_PROJECTION_SHA256,
    PINNED_SPEC_SHA256,
    POLICY_PROJECTION,
    PRE_REGISTRATION,
    policy_projection_sha256,
    spec_sha256,
)


def test_pinned_spec_hash_matches_canonical_payload() -> None:
    assert spec_sha256() == PINNED_SPEC_SHA256
    assert len(PINNED_SPEC_SHA256) == 64
    assert spec_sha256(BASE_PRE_REGISTRATION) == BASE_PRE_REGISTRATION_SHA256


def test_policy_projection_has_its_own_exact_seal() -> None:
    assert policy_projection_sha256() == PINNED_POLICY_PROJECTION_SHA256
    assert (
        ACTIVATION_EPOCH_ADDENDUM["policy_projection_sha256"]
        == PINNED_POLICY_PROJECTION_SHA256
    )
    mutated = deepcopy(POLICY_PROJECTION)
    mutated["variant_b"]["support_strength_min"] = "weak"
    assert policy_projection_sha256(mutated) != PINNED_POLICY_PROJECTION_SHA256
    gates = POLICY_PROJECTION["shared_gates"]
    assert gates["rsi"] == {
        "operator": "lt",
        "threshold": "45",
        "missing": "reject",
    }
    assert gates["support_distance_pct"] == {
        "operator": "closed_interval",
        "minimum": "0",
        "maximum": "8",
        "missing": "reject",
    }
    assert gates["honest_upside_pct"] == {
        "operator": "gte",
        "threshold": "40",
        "missing": "reject",
    }
    assert gates["other_gate_bits"]["required_value"] is True
    assert gates["other_gate_bits"]["missing_value"] is False
    assert gates["other_gate_bits"]["non_boolean"] == "reject"
    assert POLICY_PROJECTION["variant_a"]["support_strength_min"] == A_SUPPORT_MIN
    assert POLICY_PROJECTION["variant_b"]["support_strength_min"] == B_SUPPORT_MIN
    assert gates["rsi"]["threshold"] == str(RSI_MAX)
    assert gates["support_distance_pct"]["maximum"] == str(SUPPORT_WITHIN_PCT)
    assert gates["honest_upside_pct"]["threshold"] == str(UPSIDE_MIN_PCT)
    assert tuple(gates["other_gate_bits"]["keys"]) == OTHER_GATE_KEYS


def test_q6_activation_epoch_is_a_versioned_addendum() -> None:
    assert PRE_REGISTRATION["addenda"] == [ACTIVATION_EPOCH_ADDENDUM]
    assert ACTIVATION_EPOCH_ADDENDUM["version"] == ACTIVATION_EPOCH_ADDENDUM_VERSION
    epoch = ACTIVATION_EPOCH_ADDENDUM["collection_epoch"]
    assert epoch["collection_armed_at"] == "2026-08-30T09:17:36+09:00"
    assert epoch["collection_start"] == "2026-08-31"
    assert epoch["collection_end_exclusive"] == "2026-09-28"
    assert epoch["first_valid_record_at"] is None
    assert epoch["zero_event_close"] == {
        "status": "INSUFFICIENT_SAMPLE",
        "outcome": "NO_FIRING",
    }
    assert (
        epoch["scoring_ready_rule"] == "collection_window_closed AND all_events_matured"
    )
    assert ACTIVATION_EPOCH_ADDENDUM["independent_review"] == (
        "required_external_verifier"
    )


def test_mutating_pre_registration_changes_hash() -> None:
    mutated = deepcopy(PRE_REGISTRATION)
    mutated["shared_gates"]["rsi_max"] = 50
    assert spec_sha256(mutated) != PINNED_SPEC_SHA256


def test_forbidden_three_are_issue_canonical() -> None:
    assert FORBIDDEN == (
        "shadow가 제안·주문·워치로 승격 금지(순수 기록)",
        "라이브 게이트 문언 무접촉",
        "채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)",
    )
    assert PRE_REGISTRATION["forbidden"] == list(FORBIDDEN)


def test_only_difference_is_support_strength_min() -> None:
    assert PRE_REGISTRATION["experiment_id"] == EXPERIMENT_ID
    assert PRE_REGISTRATION["only_difference"] == "support_strength_min"
    assert PRE_REGISTRATION["variant_a"]["support_strength_min"] == "strong"
    assert PRE_REGISTRATION["variant_b"]["support_strength_min"] == "moderate"
    assert PRE_REGISTRATION["variant_a"]["executes"] is True
    assert PRE_REGISTRATION["variant_b"]["executes"] is False
    assert PRE_REGISTRATION["variant_b"]["register_as"] == "shadow_buy"
    assert PRE_REGISTRATION["windows_trading_days"] == [5, 20]
    assert PRE_REGISTRATION["collection_calendar_days"] == 28
    assert PRE_REGISTRATION["scoring"]["same_formula_both_variants"] is True
    assert PRE_REGISTRATION["scoring"]["single_scoring_as_of"] is True
    assert PRE_REGISTRATION["scoring"]["score_before_collection_complete"] == "refuse"
    assert PRE_REGISTRATION["scoring"]["winner_declaration"] == "forbidden"
    assert PRE_REGISTRATION["scoring"]["promotion_automation_trigger"] is False
    assert PRE_REGISTRATION["forecast_tagging"]["promote"] is False
