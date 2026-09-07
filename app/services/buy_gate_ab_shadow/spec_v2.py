"""Independent ROB-1351 v2 pre-registration for the moderate live gate.

This registration is deliberately separate from the sealed ROB-1301 payload.
It defines a new population after the operator's policy decision; it neither
amends v1 nor carries a v1 sample into a later comparison.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

EXPERIMENT_ID_V2: Final = "rob-1351-buy-gate-moderate-live"
PREREGISTRATION_VERSION_V2: Final = "rob-1351-buy-gate-moderate-live.v1"

# The first and third prohibitions are copied verbatim from ROB-1301.  The
# second is deliberately reworded rather than paraphrased: the policy decision
# predates v2 and defines its population, rather than being an experiment act.
FORBIDDEN_V2: Final[tuple[str, str, str]] = (
    "shadow가 제안·주문·워치로 승격 금지(순수 기록)",
    "라이브 게이트 문언은 이 실험이 바꾸지 않는다 — 문언 변경은 이 실험에 선행하는 운영자 결정이며 v2 의 모집단을 정의한다",
    "채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)",
)

PRE_REGISTRATION_V2: Final[dict[str, Any]] = {
    "experiment_id": EXPERIMENT_ID_V2,
    "spec_version": PREREGISTRATION_VERSION_V2,
    "issue": "ROB-1351",
    "predecessor": {
        "experiment_id": "rob-1301-buy-gate-ab-shadow",
        "terminal_status": "INSUFFICIENT_SAMPLE",
        "termination_reason": "STOPPED_BY_OPERATOR_DECISION",
        "carried_over_samples": "forbidden",
    },
    "hypothesis": (
        "정규 발굴 지지 요건을 moderate 로 낮춰 새로 편입된 후보(moderate_only)가 "
        "기존 strong 후보 대비 기대값을 훼손하지 않는다; 채점 후 수정 금지"
    ),
    "markets": ["kr", "us"],
    "market_priority": ["kr", "us"],
    "variant_a": {
        "label": "A",
        "role": "live",
        "support_strength_min": "moderate",
        "executes": True,
        "cohort_labels": ["strong", "moderate_only"],
    },
    "variant_b": {
        "label": "B",
        "role": "shadow",
        "support_strength_min": "weak",
        "executes": False,
        "register_as": "shadow_buy",
    },
    "support_strength_order": ["weak", "moderate", "strong"],
    "shared_gates": {
        "rsi_max": 45,
        "support_within_pct": 8,
        "upside_min_pct": 40,
        "other_gate_bit_keys": [
            "liquid_midcap",
            "concentration",
            "overhang",
        ],
    },
    "only_difference": "support_strength_min",
    "entry": "decision_time_current_price_frozen",
    "assumed_sizing": {
        "cap_krw": 400000,
        "cap_usd": 450,
        "multiplier": "0.5",
    },
    "windows_trading_days": [5, 20],
    "collection_calendar_days": 28,
    "scoring": {
        "primary_metrics": [
            "simple_return_to_close",
            "max_drawdown_from_entry_close_peak",
        ],
        "sensitivity_metrics": [
            "simple_return_to_window_high",
            "simple_return_to_window_low",
        ],
        "single_scoring_as_of": True,
        "same_formula_both_variants": True,
        "bars_after_scoring_as_of_ignored": True,
        "do_not_impute_missing_bars": True,
        "a_primary_entry": "frozen_decision_price_not_fill",
        "actual_fill_return_is_sensitivity_only": True,
        "combine_with": "ROB-1283",
        "cohort_split_is_observational_not_randomized": True,
        "cohort_split_confounding": (
            "support strength is a candidate property, not an assignment"
        ),
        "winner_declaration": "forbidden",
        "intermediate_policy_change": "forbidden",
        "peeking": "forbidden",
        "score_before_collection_complete": "refuse",
        "collection_extension_after_peek": "forbidden",
        "promotion_automation_trigger": False,
    },
    "forbidden": list(FORBIDDEN_V2),
    "forecast_tagging": {
        "session_label": EXPERIMENT_ID_V2,
        "cohort": "shadow_buy",
        "promote": False,
        "calibration_eligibility": "calibration_exclude",
        "trade_performance_eligibility": "trade_performance_exclude",
        "probability_placeholder": "0.5",
        "kind": "price_target",
        "outcome_rule_version": "window-touch-v1-high-gte-low-lte",
        "direction": "at_or_above",
        "scoring_authority": "rob-1351-buy-gate-moderate-live.scoring",
        "do_not_use_forecast_resolve_as_experiment_score": True,
    },
}

POLICY_PROJECTION_V2: Final[dict[str, Any]] = {
    "schema": "rob-1351-buy-gate-policy-projection.v1",
    "experiment_id": EXPERIMENT_ID_V2,
    "source": "app.services.buy_gate_ab_shadow.evaluate.evaluate_candidate",
    "markets": ["kr", "us"],
    "variant_a": {
        "label": "A",
        "role": "live",
        "support_strength_min": "moderate",
        "executes": True,
    },
    "variant_b": {
        "label": "B",
        "role": "shadow",
        "support_strength_min": "weak",
        "executes": False,
        "register_as": "shadow_buy",
    },
    "support_strength_order": ["weak", "moderate", "strong"],
    "shared_gates": {
        "rsi": {
            "operator": "lt",
            "threshold": "45",
            "missing": "reject",
        },
        "support_distance_pct": {
            "operator": "closed_interval",
            "minimum": "0",
            "maximum": "8",
            "missing": "reject",
        },
        "honest_upside_pct": {
            "operator": "gte",
            "threshold": "40",
            "missing": "reject",
        },
        "other_gate_bits": {
            "keys": [
                "liquid_midcap",
                "concentration",
                "overhang",
            ],
            "required_value": True,
            "missing_value": False,
            "non_boolean": "reject",
        },
    },
    "only_difference": "support_strength_min",
}

# These are reviewed literals, deliberately not import-time calculations.  A
# payload edit must make the pin tests fail closed until a new registration is
# explicitly reviewed.
PINNED_SPEC_SHA256_V2: Final = (
    "c156fdb3c3fcd5e122bf71d37e64bc3b8feac087dee7f93f8ac1a012b2cca14f"
)
PINNED_POLICY_PROJECTION_SHA256_V2: Final = (
    "33488817d1191b7ad54800da1b397682e1097627ce8fb454a36faf5394018507"
)


def canonical_spec_bytes_v2(payload: dict[str, Any] | None = None) -> bytes:
    body = PRE_REGISTRATION_V2 if payload is None else payload
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def spec_sha256_v2(payload: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_spec_bytes_v2(payload)).hexdigest()


def canonical_policy_projection_bytes_v2(
    payload: dict[str, Any] | None = None,
) -> bytes:
    body = POLICY_PROJECTION_V2 if payload is None else payload
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def policy_projection_sha256_v2(payload: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_policy_projection_bytes_v2(payload)).hexdigest()


__all__ = [
    "EXPERIMENT_ID_V2",
    "FORBIDDEN_V2",
    "PINNED_POLICY_PROJECTION_SHA256_V2",
    "PINNED_SPEC_SHA256_V2",
    "POLICY_PROJECTION_V2",
    "PRE_REGISTRATION_V2",
    "PREREGISTRATION_VERSION_V2",
    "canonical_policy_projection_bytes_v2",
    "canonical_spec_bytes_v2",
    "policy_projection_sha256_v2",
    "spec_sha256_v2",
]
