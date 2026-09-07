"""ROB-1351 v2 pre-arming A/B buy-gate evaluator.

The tool evaluates a reviewed candidate set and returns ready-to-save witness
kwargs.  It never calls ``forecast_save`` itself and cannot arm an epoch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.buy_gate_ab_shadow.epoch_v2 import (
    CollectionEpochV2Error,
    assert_v2_seal,
)
from app.services.buy_gate_ab_shadow.evaluate_v2 import (
    EvaluationError,
    evaluate_candidates,
)
from app.services.buy_gate_ab_shadow.forecast_tag_v2 import (
    build_v2_witness_forecasts,
)
from app.services.buy_gate_ab_shadow.spec_v2 import (
    EXPERIMENT_ID_V2,
    FORBIDDEN_V2,
    PINNED_POLICY_PROJECTION_SHA256_V2,
    PINNED_SPEC_SHA256_V2,
    policy_projection_sha256_v2,
    spec_sha256_v2,
)


def _parse_as_of(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise EvaluationError("evaluation_as_of is required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EvaluationError("evaluation_as_of must be timezone-aware")
    return parsed


def evaluate_buy_gate_ab_shadow_v2_impl(
    candidates: list[dict[str, Any]] | None,
    evaluation_as_of: str,
    created_by: str,
) -> dict[str, Any]:
    """Evaluate v2 review evidence and return, but never save, witness rows."""

    author = (created_by or "").strip()
    if not author:
        return {
            "success": False,
            "error": "created_by is required",
            "promote": False,
            "live_gate_impact": False,
        }
    if not isinstance(candidates, list) or not candidates:
        return {
            "success": False,
            "error": "candidates must be a non-empty list",
            "promote": False,
            "live_gate_impact": False,
        }
    try:
        assert_v2_seal()
        as_of = _parse_as_of(evaluation_as_of)
        rows = evaluate_candidates(candidates, evaluation_as_of=as_of)
        witness_forecasts: list[dict[str, Any]] = []
        for row in rows:
            witness_forecasts.extend(build_v2_witness_forecasts(row, created_by=author))
    except (CollectionEpochV2Error, EvaluationError, ValueError) as exc:
        return {
            "success": False,
            "error": str(exc),
            "promote": False,
            "live_gate_impact": False,
        }

    return {
        "success": True,
        "experiment_id": EXPERIMENT_ID_V2,
        "spec_sha256": spec_sha256_v2(),
        "pinned_spec_sha256": PINNED_SPEC_SHA256_V2,
        "policy_projection_sha256": policy_projection_sha256_v2(),
        "pinned_policy_projection_sha256": PINNED_POLICY_PROJECTION_SHA256_V2,
        "collection_epoch_id": None,
        "pre_arming_witness": True,
        "evaluation_as_of": as_of.isoformat(),
        "promote": False,
        "live_gate_impact": False,
        "forbidden": list(FORBIDDEN_V2),
        "do_not_use_for_policy_change": True,
        "candidates": [row.as_dict() for row in rows],
        "shadow_buy_forecasts": witness_forecasts,
        "counts": {
            "n": len(rows),
            "a_and_b": sum(row.cohort == "a_and_b" for row in rows),
            "b_only": sum(row.cohort == "b_only" for row in rows),
            "neither": sum(row.cohort == "neither" for row in rows),
            "experiment_sample_candidates": sum(
                row.shared_gate_bits_supplied for row in rows
            ),
        },
    }


__all__ = ["evaluate_buy_gate_ab_shadow_v2_impl"]
