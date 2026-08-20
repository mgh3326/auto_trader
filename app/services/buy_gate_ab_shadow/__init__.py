"""ROB-1301 — buy-gate A/B shadow experiment (record only).

Variant A is the live screening gate (strong support). Variant B is a
shadow-only moderate+ support counterfactual. This package never proposes,
orders, watches, or retunes the live gate.
"""

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
    EXPERIMENT_ID,
    FORBIDDEN,
    PINNED_SPEC_SHA256,
    PRE_REGISTRATION,
    spec_sha256,
)

__all__ = [
    "CandidateEvidence",
    "DailyBar",
    "EXPERIMENT_ID",
    "FORBIDDEN",
    "PINNED_SPEC_SHA256",
    "PRE_REGISTRATION",
    "VariantVerdict",
    "WindowScore",
    "build_shadow_buy_forecasts",
    "compare_cohorts",
    "evaluate_candidate",
    "evaluate_candidates",
    "score_window",
    "spec_sha256",
]
