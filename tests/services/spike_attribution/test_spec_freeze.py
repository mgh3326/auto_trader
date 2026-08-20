from __future__ import annotations

from copy import deepcopy

from app.services.spike_attribution.spec import (
    ATTRIBUTION_TYPES,
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
    mutated["spike_detection"]["abs_change_pct_min"] = 3.0
    assert spec_sha256(mutated) != PINNED_SPEC_SHA256


def test_widening_a_scoring_window_after_the_fact_changes_hash() -> None:
    mutated = deepcopy(PRE_REGISTRATION)
    mutated["follow_through"]["windows_trading_days"] = [3, 5, 10, 20]
    assert spec_sha256(mutated) != PINNED_SPEC_SHA256


def test_lowering_the_sample_floor_changes_hash() -> None:
    mutated = deepcopy(PRE_REGISTRATION)
    mutated["follow_through"]["min_events_per_type_for_comparison"] = 2
    assert spec_sha256(mutated) != PINNED_SPEC_SHA256


def test_windows_cover_the_issue_body_d_plus_5() -> None:
    # ROB-1303's body specifies D+5; [3,5,10] covers it and the earlier [3,10].
    windows = PRE_REGISTRATION["follow_through"]["windows_trading_days"]
    assert windows == [3, 5, 10]
    amendment = PRE_REGISTRATION["follow_through"]["windows_amendment"]
    assert amendment["from"] == [3, 10]
    assert amendment["to"] == windows
    # The amendment is legitimate only because nothing had been scored yet.
    assert "zero scored events" in amendment["amended_when"]
    offsets = PRE_REGISTRATION["forecast_tagging"][
        "review_date_calendar_offset_days_by_window"
    ]
    assert set(offsets) == {str(w) for w in windows}


def test_operator_enumerated_types_are_all_present() -> None:
    # 실적·공시·수급·섹터·unattributed, plus the documented ``news`` addition.
    for expected in ("earnings", "disclosure", "flow", "sector", "unattributed"):
        assert expected in ATTRIBUTION_TYPES
    assert "news" in ATTRIBUTION_TYPES


def test_forbidden_acts_are_pinned_verbatim() -> None:
    assert FORBIDDEN == (
        "원인을 발명하지 마라 — 재료로 설명되지 않으면 unattributed",
        "unattributed 를 기타·시장 전반 같은 말로 분칠하지 마라",
        "후보가 여럿이면 여럿으로 남겨라 — 하나로 단정하지 마라",
        "귀속 레코드가 제안·주문·워치로 승격되는 경로 0",
        "채점 완료 전 중간값으로 정책·임계값 변경 논거 삼지 않기",
    )


def test_experiment_declares_no_scheduler_or_broker_surface() -> None:
    assert PRE_REGISTRATION["scheduler_registration"] is False
    assert PRE_REGISTRATION["broker_or_order_surface"] is False
    assert PRE_REGISTRATION["new_credential_surface"] is False
    assert PRE_REGISTRATION["materials"]["forbidden_new_sources"] is True
    assert EXPERIMENT_ID == "rob-1303-spike-attribution"


def test_unreachable_types_are_declared_not_silently_absorbed() -> None:
    attribution = PRE_REGISTRATION["attribution"]
    assert attribution["types_unreachable_in_v1"] == ["flow", "sector"]
    assert "unattributed" not in attribution["types_unreachable_in_v1"]
    for material in ("flow", "sector"):
        block = PRE_REGISTRATION["materials"][material]
        assert block["eligible_as_cause_in_v1"] is False
        assert block["ineligibility_reason"]
