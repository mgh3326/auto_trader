"""Frozen pre-registration for ROB-1301.

Hypothesis, gates, windows, sizing, scoring formula, and the three forbidden
acts are pinned here *before* any shadow row is scored. Changing this dict
changes ``spec_sha256()`` and fails the pin test — that is the point.
Do not live-read ``trading_policy.yaml``; a later policy edit must not retcon
the experiment.

ROB-1331 adds a versioned Q6 activation-epoch addendum.  It does not rewrite
the original registration: the original canonical payload and hash remain
available as ``BASE_PRE_REGISTRATION`` / ``BASE_PRE_REGISTRATION_SHA256``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

EXPERIMENT_ID: Final = "rob-1301-buy-gate-ab-shadow"
BASE_PRE_REGISTRATION_SHA256: Final = (
    "a2814c87fcd54e3659bae0f6eb66fec145c1e02afe0507afde57cb768d86e672"
)
PREREGISTRATION_VERSION: Final = "rob-1301-buy-gate-ab-shadow.v2"
ACTIVATION_EPOCH_ADDENDUM_VERSION: Final = "rob-1331-q6-activation-epoch.v1"

# Issue ROB-1301 forbidden three — copied, not paraphrased.
FORBIDDEN: Final[tuple[str, str, str]] = (
    "shadow가 제안·주문·워치로 승격 금지(순수 기록)",
    "라이브 게이트 문언 무접촉",
    "채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)",
)

BASE_PRE_REGISTRATION: Final[dict[str, Any]] = {
    "experiment_id": EXPERIMENT_ID,
    "issue": "ROB-1301",
    "hypothesis": ("strong 지지 요구가 기대값 양(+)인 후보를 과도하게 기각한다"),
    "hypothesis_evidence_at_registration": {
        "kr_buy_rejects_on_2026-08-20": "4/6",
        "us_sessions_support_quality_majority": "9 sessions",
    },
    "markets": ["kr", "us"],
    "market_priority": ["kr", "us"],
    "variant_a": {
        "label": "A",
        "role": "live",
        "support_strength_min": "strong",
        "executes": True,
    },
    "variant_b": {
        "label": "B",
        "role": "shadow",
        "support_strength_min": "moderate",
        "executes": False,
        "register_as": "shadow_buy",
    },
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
        "winner_declaration": "forbidden",
        "intermediate_policy_change": "forbidden",
        "peeking": "forbidden",
        "score_before_collection_complete": "refuse",
        "collection_extension_after_peek": "forbidden",
        "promotion_automation_trigger": False,
    },
    "forbidden": list(FORBIDDEN),
    "forecast_tagging": {
        "session_label": EXPERIMENT_ID,
        "cohort": "shadow_buy",
        "promote": False,
        "calibration_eligibility": "calibration_exclude",
        "trade_performance_eligibility": "trade_performance_exclude",
        "probability_placeholder": "0.5",
        "kind": "price_target",
        "outcome_rule_version": "window-touch-v1-high-gte-low-lte",
        "direction": "at_or_above",
        "scoring_authority": "rob-1301-buy-gate-ab-shadow.scoring",
        "do_not_use_forecast_resolve_as_experiment_score": True,
    },
}

# Exact decision-rule projection being observed by Q6.  This is deliberately
# narrower than the full trading policy: only the A/B evaluator inputs and the
# one allowed fork belong to this experiment.  It is copied into the durable
# epoch marker by the ROB-1331 migration and never read from the live policy.
POLICY_PROJECTION: Final[dict[str, Any]] = {
    "schema": "rob-1301-buy-gate-policy-projection.v1",
    "experiment_id": EXPERIMENT_ID,
    "source": "app.services.buy_gate_ab_shadow.evaluate.evaluate_candidate",
    "markets": ["kr", "us"],
    "variant_a": {
        "label": "A",
        "role": "live",
        "support_strength_min": "strong",
        "executes": True,
    },
    "variant_b": {
        "label": "B",
        "role": "shadow",
        "support_strength_min": "moderate",
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

# Filled below from ``canonical_policy_projection_bytes``.  Keeping this as a
# literal (rather than computing it at import time) makes an unreviewed policy
# projection edit fail closed.
PINNED_POLICY_PROJECTION_SHA256: Final = (
    "c47ce8e132b7c88fa9e2554cdddc0f84663b467e115d45b79a07c618de9d857d"
)

ACTIVATION_EPOCH_ADDENDUM: Final[dict[str, Any]] = {
    "version": ACTIVATION_EPOCH_ADDENDUM_VERSION,
    "issue": "ROB-1331",
    "parent_issue": "ROB-1256",
    "verdict": "Q-d_COLLECTION_START=CORRECTION_REQUIRED",
    "registration_source_head": "f049cd922958ab0634928b9b4d5ae5359ba8b25b",
    "base_pre_registration_sha256": BASE_PRE_REGISTRATION_SHA256,
    "policy_projection": POLICY_PROJECTION,
    "policy_projection_sha256": PINNED_POLICY_PROJECTION_SHA256,
    "collection_epoch": {
        "epoch_id": "rob-1301-q6-collection-epoch.v1",
        "collection_armed_at": "2026-08-30T09:17:36+09:00",
        "collection_start": "2026-08-31",
        "collection_end_exclusive": "2026-09-28",
        "collection_calendar_days": 28,
        "collection_clock_timezone": "Asia/Seoul",
        "eligible_session_rule": (
            "next common full KR/US regular-session date strictly after "
            "collection_armed_at"
        ),
        "market_session_timezones": {
            "kr": "Asia/Seoul",
            "us": "America/New_York",
        },
        "first_valid_record_at": None,
        "first_valid_record_role": "nullable_observation_only_not_a_boundary",
        "zero_event_close": {
            "status": "INSUFFICIENT_SAMPLE",
            "outcome": "NO_FIRING",
        },
        "zero_events_all_events_matured": True,
        "scoring_ready_rule": ("collection_window_closed AND all_events_matured"),
    },
    "independent_review": "required_external_verifier",
}

PRE_REGISTRATION: Final[dict[str, Any]] = {
    **BASE_PRE_REGISTRATION,
    "spec_version": PREREGISTRATION_VERSION,
    "addenda": [ACTIVATION_EPOCH_ADDENDUM],
}

# Recomputed by tests/services/buy_gate_ab_shadow/test_spec_freeze.py.
# Bump only together with an explicit pre-registration amendment.
PINNED_SPEC_SHA256: Final = (
    "c07fb69001f5e48759718a4d725a327d5b6b1fb5d4aea442f3aeb7b170ffcd5b"
)


def canonical_spec_bytes(payload: dict[str, Any] | None = None) -> bytes:
    body = PRE_REGISTRATION if payload is None else payload
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def spec_sha256(payload: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_spec_bytes(payload)).hexdigest()


def canonical_policy_projection_bytes(
    payload: dict[str, Any] | None = None,
) -> bytes:
    body = POLICY_PROJECTION if payload is None else payload
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def policy_projection_sha256(payload: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_policy_projection_bytes(payload)).hexdigest()
