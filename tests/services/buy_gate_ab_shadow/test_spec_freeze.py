from __future__ import annotations

from copy import deepcopy

from app.services.buy_gate_ab_shadow.spec import (
    EXPERIMENT_ID,
    FORBIDDEN,
    PINNED_SPEC_SHA256,
    PRE_REGISTRATION,
    spec_sha256,
)


def test_pinned_spec_hash_matches_canonical_payload() -> None:
    assert spec_sha256() == PINNED_SPEC_SHA256
    assert len(PINNED_SPEC_SHA256) == 64


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
