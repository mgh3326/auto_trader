"""Build pre-arming witness ``forecast_save`` kwargs for ROB-1351 v2.

The v2 collection epoch is intentionally unarmed.  These payloads are
evidence of plumbing or of a reviewed sample, never an activation marker and
never a promotion instruction.  This module does not import v1 forecast or
epoch code.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.services.buy_gate_ab_shadow.epoch_v2 import assert_v2_seal
from app.services.buy_gate_ab_shadow.evaluate_v2 import CandidateEvaluation
from app.services.buy_gate_ab_shadow.policy_alignment_v2 import (
    assert_v2_policy_alignment,
)
from app.services.buy_gate_ab_shadow.spec_v2 import (
    EXPERIMENT_ID_V2,
    PINNED_POLICY_PROJECTION_SHA256_V2,
    PINNED_SPEC_SHA256_V2,
    PRE_REGISTRATION_V2,
)

_TAG = PRE_REGISTRATION_V2["forecast_tagging"]
_SIZING = PRE_REGISTRATION_V2["assumed_sizing"]
_WINDOWS: tuple[int, ...] = tuple(PRE_REGISTRATION_V2["windows_trading_days"])
_CALENDAR_OFFSET_BY_WINDOW: dict[int, int] = {5: 7, 20: 28}
_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us"}
_CAP = {"kr": Decimal(str(_SIZING["cap_krw"])), "us": Decimal(str(_SIZING["cap_usd"]))}
_MULTIPLIER = Decimal(str(_SIZING["multiplier"]))

FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "order_proposal",
        "proposal_id",
        "place_order",
        "max_action",
        "watch_condition",
        "approval_hash",
        "confirm",
        "account_mode",
    }
)


def assumed_notional(market: str) -> Decimal:
    return _CAP[market] * _MULTIPLIER


def _review_date(decision: date, window_trading_days: int) -> str:
    offset = _CALENDAR_OFFSET_BY_WINDOW[window_trading_days]
    return (decision + timedelta(days=offset)).isoformat()


def _sample_fields(evaluation: CandidateEvaluation) -> tuple[bool, str]:
    """Derive the sample flag solely from actual v2 shared-bit provenance."""

    if evaluation.shared_gate_bits_supplied:
        return True, "supplied"
    return False, "unavailable_at_this_call_site"


def build_shadow_buy_forecasts(
    evaluation: CandidateEvaluation,
    *,
    created_by: str,
    experiment_sample: bool | None = None,
) -> list[dict[str, Any]]:
    """Return v2 5d+20d witness kwargs for every evaluated candidate.

    ``experiment_sample`` is deliberately not a caller-controlled override.
    It remains in the signature so a caller cannot smuggle a true value into a
    fanout witness: the value stamped below always comes from the exact
    boolean-bit provenance captured by ``CandidateEvidence``.
    """

    del experiment_sample
    author = (created_by or "").strip()
    if not author:
        raise ValueError("created_by is required")
    assert_v2_seal()
    assert_v2_policy_alignment()

    decision = evaluation.evaluation_as_of.date()
    notional = assumed_notional(evaluation.market)
    is_sample, shared_gate_bits = _sample_fields(evaluation)
    payloads: list[dict[str, Any]] = []
    for window in _WINDOWS:
        forecast_target = {
            "kind": _TAG["kind"],
            "direction": _TAG["direction"],
            "target_price": float(evaluation.entry_price),
            "outcome_rule_version": _TAG["outcome_rule_version"],
            "experiment_id": EXPERIMENT_ID_V2,
            # ``variant``/``cohort`` identify the sealed v2 stream.  The
            # evaluated facts below describe this particular candidate.
            "variant": "B",
            "cohort": _TAG["cohort"],
            "evaluated_cohort": evaluation.cohort,
            "variant_a_passed": evaluation.variant_a.passed,
            "variant_b_passed": evaluation.variant_b.passed,
            "shadow_buy": evaluation.shadow_buy,
            "promote": False,
            "live_gate_impact": False,
            "spec_sha256": PINNED_SPEC_SHA256_V2,
            "policy_projection_sha256": PINNED_POLICY_PROJECTION_SHA256_V2,
            # v2 is unarmed.  A witness cannot manufacture an epoch marker.
            "collection_epoch_id": None,
            "pre_arming_witness": True,
            "experiment_sample": is_sample,
            "shared_gate_bits": shared_gate_bits,
            "evaluation_as_of": evaluation.evaluation_as_of.isoformat(),
            "session_date": decision.isoformat(),
            "entry_price": str(evaluation.entry_price),
            "input_snapshot": dict(evaluation.input_snapshot),
            "input_snapshot_sha256": evaluation.input_snapshot_sha256,
            "assumed_notional": str(notional),
            "window_trading_days": window,
            "support_strength": evaluation.support_strength,
            "variant_a_cohort_label": evaluation.variant_a_cohort_label,
            "calibration_eligibility": _TAG["calibration_eligibility"],
            "trade_performance_eligibility": _TAG["trade_performance_eligibility"],
            "scoring_authority": _TAG["scoring_authority"],
            "do_not_use_forecast_resolve_as_experiment_score": _TAG[
                "do_not_use_forecast_resolve_as_experiment_score"
            ],
        }
        leaked = FORBIDDEN_PAYLOAD_KEYS.intersection(forecast_target)
        if leaked:
            raise RuntimeError(f"promotion keys leaked into forecast_target: {leaked}")
        payload = {
            "created_by": author,
            "symbol": evaluation.symbol,
            "instrument_type": _INSTRUMENT[evaluation.market],
            "forecast_target": forecast_target,
            "probability": float(_TAG["probability_placeholder"]),
            "review_date": _review_date(decision, window),
            "horizon": f"{window}d",
            "session_label": EXPERIMENT_ID_V2,
            "correlation_id": (
                f"{EXPERIMENT_ID_V2}:{evaluation.market}:{evaluation.symbol}:"
                f"{decision.isoformat()}:{window}d"
            ),
            "contrary_evidence": (
                "ROB-1351 v2 pre-arming witness; shadow_buy; do not promote "
                "to proposal, order, or watch"
            ),
        }
        leaked_top = FORBIDDEN_PAYLOAD_KEYS.intersection(payload)
        if leaked_top:
            raise RuntimeError(
                f"promotion keys leaked into forecast payload: {leaked_top}"
            )
        payloads.append(payload)
    return payloads


def build_v2_witness_forecasts(
    evaluation: CandidateEvaluation,
    *,
    created_by: str,
    experiment_sample: bool | None = None,
) -> list[dict[str, Any]]:
    """Explicit alias for callers that record all v2 witness cohorts."""

    return build_shadow_buy_forecasts(
        evaluation,
        created_by=created_by,
        experiment_sample=experiment_sample,
    )


__all__ = [
    "FORBIDDEN_PAYLOAD_KEYS",
    "assumed_notional",
    "build_shadow_buy_forecasts",
    "build_v2_witness_forecasts",
]
