"""ROB-1301 — buy-gate A/B shadow experiment (record only).

Variant A is the live screening gate (strong support). Variant B is a
shadow-only moderate+ support counterfactual. This package never proposes,
orders, watches, or retunes the live gate.
"""

from app.services.buy_gate_ab_shadow.epoch import (
    COLLECTION_EPOCH,
    CollectionEpochMarker,
    CollectionReadiness,
    assess_collection_readiness,
)
from app.services.buy_gate_ab_shadow.evaluate import (
    CandidateEvidence,
    VariantVerdict,
    evaluate_candidate,
    evaluate_candidates,
)
from app.services.buy_gate_ab_shadow.forecast_tag import (
    build_shadow_buy_forecasts,
)
from app.services.buy_gate_ab_shadow.scoring import (
    DailyBar,
    WindowScore,
    compare_cohorts,
    score_window,
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

__all__ = [
    "ACTIVATION_EPOCH_ADDENDUM",
    "ACTIVATION_EPOCH_ADDENDUM_VERSION",
    "BASE_PRE_REGISTRATION",
    "BASE_PRE_REGISTRATION_SHA256",
    "CandidateEvidence",
    "COLLECTION_EPOCH",
    "CollectionEpochMarker",
    "CollectionReadiness",
    "DailyBar",
    "EXPERIMENT_ID",
    "FORBIDDEN",
    "PINNED_POLICY_PROJECTION_SHA256",
    "PINNED_SPEC_SHA256",
    "POLICY_PROJECTION",
    "PRE_REGISTRATION",
    "VariantVerdict",
    "WindowScore",
    "assess_collection_readiness",
    "build_shadow_buy_forecasts",
    "compare_cohorts",
    "evaluate_candidate",
    "evaluate_candidates",
    "policy_projection_sha256",
    "score_window",
    "spec_sha256",
]
