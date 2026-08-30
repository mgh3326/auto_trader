"""ROB-1301 observation-only A/B buy-gate evaluator.

Does not write forecasts, proposals, orders, or watches. A B-only candidate
returns ready-to-save forecast_save kwargs; the caller decides whether to
record them. Intermediate scores are not a policy argument.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.buy_gate_ab_shadow.epoch import (
    COLLECTION_EPOCH,
    CollectionEpochError,
    assert_epoch_seal,
)
from app.services.buy_gate_ab_shadow.evaluate import (
    EvaluationError,
    evaluate_candidates,
)
from app.services.buy_gate_ab_shadow.forecast_tag import build_shadow_buy_forecasts
from app.services.buy_gate_ab_shadow.spec import (
    EXPERIMENT_ID,
    FORBIDDEN,
    PINNED_SPEC_SHA256,
    spec_sha256,
)


def _parse_as_of(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise EvaluationError("evaluation_as_of is required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EvaluationError("evaluation_as_of must be timezone-aware")
    return parsed


def evaluate_buy_gate_ab_shadow_impl(
    candidates: list[dict[str, Any]] | None,
    evaluation_as_of: str,
    created_by: str,
) -> dict[str, Any]:
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
        assert_epoch_seal()
        as_of = _parse_as_of(evaluation_as_of)
        rows = evaluate_candidates(candidates, evaluation_as_of=as_of)
        shadow_forecasts: list[dict[str, Any]] = []
        for row in rows:
            shadow_forecasts.extend(build_shadow_buy_forecasts(row, created_by=author))
    except (CollectionEpochError, EvaluationError, ValueError) as exc:
        return {
            "success": False,
            "error": str(exc),
            "promote": False,
            "live_gate_impact": False,
        }

    return {
        "success": True,
        "experiment_id": EXPERIMENT_ID,
        "spec_sha256": spec_sha256(),
        "pinned_spec_sha256": PINNED_SPEC_SHA256,
        "collection_epoch": COLLECTION_EPOCH.as_dict(),
        "evaluation_as_of": as_of.isoformat(),
        "promote": False,
        "live_gate_impact": False,
        "forbidden": list(FORBIDDEN),
        "do_not_use_for_policy_change": True,
        "candidates": [row.as_dict() for row in rows],
        "shadow_buy_forecasts": shadow_forecasts,
        "counts": {
            "n": len(rows),
            "a_and_b": sum(row.cohort == "a_and_b" for row in rows),
            "b_only": sum(row.cohort == "b_only" for row in rows),
            "neither": sum(row.cohort == "neither" for row in rows),
        },
    }


__all__ = ["evaluate_buy_gate_ab_shadow_impl"]
