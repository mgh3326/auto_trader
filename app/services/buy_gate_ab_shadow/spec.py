"""Frozen pre-registration for ROB-1301.

Hypothesis, gates, windows, sizing, scoring formula, and the three forbidden
acts are pinned here *before* any shadow row is scored. Changing this dict
changes ``spec_sha256()`` and fails the pin test — that is the point.
Do not live-read ``trading_policy.yaml``; a later policy edit must not retcon
the experiment.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

EXPERIMENT_ID: Final = "rob-1301-buy-gate-ab-shadow"

# Issue ROB-1301 forbidden three — copied, not paraphrased.
FORBIDDEN: Final[tuple[str, str, str]] = (
    "shadow가 제안·주문·워치로 승격 금지(순수 기록)",
    "라이브 게이트 문언 무접촉",
    "채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)",
)

PRE_REGISTRATION: Final[dict[str, Any]] = {
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

# Recomputed by tests/services/buy_gate_ab_shadow/test_spec_freeze.py.
# Bump only together with an explicit pre-registration amendment.
PINNED_SPEC_SHA256: Final = (
    "a2814c87fcd54e3659bae0f6eb66fec145c1e02afe0507afde57cb768d86e672"
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
