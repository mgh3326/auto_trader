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
from app.services.buy_gate_ab_shadow.epoch_v2 import (
    CollectionEpochV2Error,
    assert_v2_seal,
    build_marker,
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
from app.services.buy_gate_ab_shadow.spec_v2 import (
    EXPERIMENT_ID_V2,
    FORBIDDEN_V2,
    PINNED_POLICY_PROJECTION_SHA256_V2,
    PINNED_SPEC_SHA256_V2,
    POLICY_PROJECTION_V2,
    PRE_REGISTRATION_V2,
    PREREGISTRATION_VERSION_V2,
    canonical_policy_projection_bytes_v2,
    canonical_spec_bytes_v2,
    policy_projection_sha256_v2,
    spec_sha256_v2,
)
from app.services.buy_gate_ab_shadow.termination import (
    ROB_1301_TERMINATION,
    Carryover,
    ExperimentTermination,
    ExperimentTerminationError,
    TerminationReason,
    assert_predecessor_seal_intact,
    terminal_report,
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
    "CollectionEpochV2Error",
    "Carryover",
    "DailyBar",
    "EXPERIMENT_ID",
    "EXPERIMENT_ID_V2",
    "FORBIDDEN",
    "FORBIDDEN_V2",
    "ExperimentTermination",
    "ExperimentTerminationError",
    "PINNED_POLICY_PROJECTION_SHA256",
    "PINNED_POLICY_PROJECTION_SHA256_V2",
    "PINNED_SPEC_SHA256",
    "PINNED_SPEC_SHA256_V2",
    "POLICY_PROJECTION",
    "POLICY_PROJECTION_V2",
    "PRE_REGISTRATION",
    "PRE_REGISTRATION_V2",
    "PREREGISTRATION_VERSION_V2",
    "ROB_1301_TERMINATION",
    "TerminationReason",
    "VariantVerdict",
    "WindowScore",
    "assess_collection_readiness",
    "assert_predecessor_seal_intact",
    "assert_v2_seal",
    "build_marker",
    "build_shadow_buy_forecasts",
    "canonical_policy_projection_bytes_v2",
    "canonical_spec_bytes_v2",
    "compare_cohorts",
    "evaluate_candidate",
    "evaluate_candidates",
    "policy_projection_sha256",
    "policy_projection_sha256_v2",
    "score_window",
    "spec_sha256",
    "spec_sha256_v2",
    "terminal_report",
]
