"""Candidate-specific US falsification gates for Stage-B evidence.

The output can only say that a frozen candidate was or was not falsified by
its stated exploratory gates.  It never produces a promotion, execution, or
live-trading decision.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import TYPE_CHECKING, Any, Literal

from .registry import CandidateBinding

if TYPE_CHECKING:
    from .engine import CohortComparison, TradeOutcome

__all__ = [
    "FalsificationEvaluation",
    "FalsificationVerdict",
    "RevCostProfileVerdicts",
    "evaluate_falsification",
    "evaluate_falsification_evidence",
]


CostProfile = Literal["base_10bp_per_side", "sensitivity_5bp_per_side"]


VerdictState = Literal[
    "RUN_INVALID",
    "FALSIFIED_FREQUENCY",
    "FALSIFIED_VALIDATION_COHORT_EXCESS",
    "FALSIFIED_COST_SENSITIVE",
    "FALSIFIED_EXTREME_DEPENDENCE",
    "UNIDENTIFIABLE_COHORT",
    "UNIDENTIFIABLE_EXTREME_ANALYSIS",
    "NOT_FALSIFIED_EXPLORATORY_ONLY",
]


@dataclass(frozen=True)
class FalsificationVerdict:
    """A provenance-stamped candidate verdict with explicit gate measurements."""

    strategy_id: str
    contract_hash: str
    labels: tuple[str, ...]
    state: VerdictState
    falsification_literal: str
    gate_results: Mapping[str, Any]
    invalid_reasons: tuple[str, ...]
    cost_profile: CostProfile | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "labels": list(self.labels),
            "state": self.state,
            "falsification_literal": self.falsification_literal,
            "gate_results": dict(self.gate_results),
            "invalid_reasons": list(self.invalid_reasons),
            "cost_profile": self.cost_profile,
            "promotion": "FORBIDDEN",
        }


@dataclass(frozen=True)
class RevCostProfileVerdicts:
    """Independent REV verdicts for the two frozen execution-cost profiles."""

    base_10bp_per_side: FalsificationVerdict
    sensitivity_5bp_per_side: FalsificationVerdict

    def __post_init__(self) -> None:
        base = self.base_10bp_per_side
        sensitivity = self.sensitivity_5bp_per_side
        if base is sensitivity:
            raise ValueError("REV cost profiles must retain distinct verdict objects")
        if base.cost_profile != "base_10bp_per_side":
            raise ValueError("REV base verdict lost its 10bp/side identity")
        if sensitivity.cost_profile != "sensitivity_5bp_per_side":
            raise ValueError("REV sensitivity verdict lost its 5bp/side identity")
        if (
            base.strategy_id != sensitivity.strategy_id
            or base.contract_hash != sensitivity.contract_hash
            or base.labels != sensitivity.labels
        ):
            raise ValueError("REV cost-profile verdict provenance mismatch")

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {
            "base_10bp_per_side": self.base_10bp_per_side.to_dict(),
            "sensitivity_5bp_per_side": self.sensitivity_5bp_per_side.to_dict(),
        }


@dataclass(frozen=True)
class FalsificationEvaluation:
    """Aggregate falsification result plus any required cost-profile evidence."""

    verdict: FalsificationVerdict
    cost_profile_verdicts: RevCostProfileVerdicts | None

    def __post_init__(self) -> None:
        is_rev = self.verdict.strategy_id == "US-TS-REV-SHORT-Z3-T126-H3-v1"
        if is_rev != (self.cost_profile_verdicts is not None):
            raise ValueError("REV alone requires the frozen dual cost-profile verdicts")
        if self.cost_profile_verdicts is not None:
            base = self.cost_profile_verdicts.base_10bp_per_side
            if (
                self.verdict.contract_hash != base.contract_hash
                or self.verdict.labels != base.labels
            ):
                raise ValueError("aggregate REV verdict provenance mismatch")


def evaluate_falsification(
    *,
    candidate: CandidateBinding,
    outcomes: Sequence[TradeOutcome],
    cohorts: Sequence[CohortComparison],
    run_invalid: bool,
    invalid_reasons: Sequence[str],
) -> FalsificationVerdict:
    """Return the aggregate frozen-candidate verdict for compatibility callers."""

    return evaluate_falsification_evidence(
        candidate=candidate,
        outcomes=outcomes,
        cohorts=cohorts,
        run_invalid=run_invalid,
        invalid_reasons=invalid_reasons,
    ).verdict


def evaluate_falsification_evidence(
    *,
    candidate: CandidateBinding,
    outcomes: Sequence[TradeOutcome],
    cohorts: Sequence[CohortComparison],
    run_invalid: bool,
    invalid_reasons: Sequence[str],
) -> FalsificationEvaluation:
    """Produce aggregate evidence and the mandatory independent REV pair."""

    completed = tuple(outcome for outcome in outcomes if outcome.status == "completed")
    validation_completed = tuple(
        outcome
        for outcome in completed
        if outcome.entry_session is not None
        and outcome.entry_session.year in {2023, 2024}
    )
    common = {
        "completed_trade_count": len(completed),
        "unique_entry_session_count": len(
            {
                outcome.entry_session
                for outcome in completed
                if outcome.entry_session is not None
            }
        ),
        "validation_completed_trade_count": len(validation_completed),
        "validation_unique_entry_session_count": len(
            {
                outcome.entry_session
                for outcome in validation_completed
                if outcome.entry_session is not None
            }
        ),
        "validation_completed_by_entry_year": {
            str(year): sum(
                outcome.entry_session is not None and outcome.entry_session.year == year
                for outcome in validation_completed
            )
            for year in (2023, 2024)
        },
    }
    resolved_validation = tuple(
        cohort
        for cohort in cohorts
        if cohort.status == "completed" and cohort.entry_session.year in {2023, 2024}
    )
    completed_identities = {
        _identity(outcome.entry_session, outcome.symbol)
        for outcome in validation_completed
        if outcome.entry_session is not None
    }
    resolved_identities = {
        _identity(cohort.entry_session, cohort.symbol) for cohort in resolved_validation
    }
    coverage = {
        "validation_completed_with_resolved_cohort_count": len(resolved_validation),
        "validation_cohort_unavailable_count": len(
            completed_identities - resolved_identities
        ),
    }

    if candidate.strategy_id == "US-TS-REV-SHORT-Z3-T126-H3-v1":
        profiles = _evaluate_rev_cost_profile_verdicts(
            candidate=candidate,
            common=common,
            coverage=coverage,
            validation=resolved_validation,
            run_invalid=run_invalid,
            invalid_reasons=invalid_reasons,
        )
        return FalsificationEvaluation(
            verdict=_compose_rev_dual_falsification(candidate, profiles),
            cost_profile_verdicts=profiles,
        )
    if run_invalid:
        return FalsificationEvaluation(
            verdict=_verdict(
                candidate,
                "RUN_INVALID",
                {
                    **common,
                    "run_invalid": True,
                    "maturity_close_missing_policy": "RUN_INVALID",
                },
                invalid_reasons,
            ),
            cost_profile_verdicts=None,
        )
    if candidate.strategy_id == "US-TS-MOM-CONT-Z126-H20-v1":
        return FalsificationEvaluation(
            verdict=_evaluate_mom(candidate, common, coverage, resolved_validation),
            cost_profile_verdicts=None,
        )
    if candidate.strategy_id == "US-TS-VOLBREAK-C55-V2-H10-v1":
        return FalsificationEvaluation(
            verdict=_evaluate_volbreak(
                candidate, common, coverage, resolved_validation
            ),
            cost_profile_verdicts=None,
        )
    raise ValueError(f"unsupported US candidate {candidate.strategy_id!r}")


def _evaluate_mom(
    candidate: CandidateBinding,
    common: Mapping[str, Any],
    coverage: Mapping[str, int],
    validation: Sequence[CohortComparison],
) -> FalsificationVerdict:
    frequency_pass = (
        common["completed_trade_count"] >= 400
        and common["unique_entry_session_count"] >= 120
        and common["validation_completed_trade_count"] >= 60
        and common["validation_unique_entry_session_count"] >= 24
    )
    metrics = _validation_metrics(validation)
    gates = {
        **common,
        **coverage,
        **metrics,
        "frequency_gate": {
            "completed_trade_min": 400,
            "unique_entry_session_min": 120,
            "validation_completed_trade_min": 60,
            "validation_unique_entry_session_min": 24,
            "passed": frequency_pass,
        },
        "validation_entry_session_equal_weighted_base_excess_gt_zero": (
            metrics["base_entry_session_equal_weighted_excess"] is not None
            and metrics["base_entry_session_equal_weighted_excess"] > 0.0
        ),
    }
    if not frequency_pass:
        return _verdict(candidate, "FALSIFIED_FREQUENCY", gates)
    if coverage["validation_cohort_unavailable_count"]:
        return _verdict(candidate, "UNIDENTIFIABLE_COHORT", gates)
    if metrics["base_entry_session_equal_weighted_excess"] is None:
        return _verdict(candidate, "UNIDENTIFIABLE_COHORT", gates)
    if metrics["base_entry_session_equal_weighted_excess"] <= 0.0:
        return _verdict(candidate, "FALSIFIED_VALIDATION_COHORT_EXCESS", gates)
    return _verdict(candidate, "NOT_FALSIFIED_EXPLORATORY_ONLY", gates)


def _evaluate_rev_cost_profile_verdicts(
    *,
    candidate: CandidateBinding,
    common: Mapping[str, Any],
    coverage: Mapping[str, int],
    validation: Sequence[CohortComparison],
    run_invalid: bool,
    invalid_reasons: Sequence[str],
) -> RevCostProfileVerdicts:
    return RevCostProfileVerdicts(
        base_10bp_per_side=_evaluate_rev_at_cost_profile(
            candidate=candidate,
            common=common,
            coverage=coverage,
            validation=validation,
            cost_profile="base_10bp_per_side",
            run_invalid=run_invalid,
            invalid_reasons=invalid_reasons,
        ),
        sensitivity_5bp_per_side=_evaluate_rev_at_cost_profile(
            candidate=candidate,
            common=common,
            coverage=coverage,
            validation=validation,
            cost_profile="sensitivity_5bp_per_side",
            run_invalid=run_invalid,
            invalid_reasons=invalid_reasons,
        ),
    )


def _evaluate_rev_at_cost_profile(
    *,
    candidate: CandidateBinding,
    common: Mapping[str, Any],
    coverage: Mapping[str, int],
    validation: Sequence[CohortComparison],
    cost_profile: CostProfile,
    run_invalid: bool,
    invalid_reasons: Sequence[str],
) -> FalsificationVerdict:
    by_year = common["validation_completed_by_entry_year"]
    frequency_pass = (
        common["completed_trade_count"] >= 1_000
        and common["unique_entry_session_count"] >= 200
        and common["validation_completed_trade_count"] >= 200
        and common["validation_unique_entry_session_count"] >= 50
        and by_year["2023"] >= 80
        and by_year["2024"] >= 80
    )
    metrics = _validation_metrics(validation)
    metric_name = {
        "base_10bp_per_side": "base_entry_session_equal_weighted_excess",
        "sensitivity_5bp_per_side": "sensitivity_entry_session_equal_weighted_excess",
    }[cost_profile]
    excess = metrics[metric_name]
    bp_per_side = 10 if cost_profile == "base_10bp_per_side" else 5
    gates = {
        **common,
        **coverage,
        **metrics,
        "cost_profile": cost_profile,
        "cost_bp_per_side": bp_per_side,
        "profile_entry_session_equal_weighted_excess": excess,
        "profile_entry_session_equal_weighted_excess_gt_zero": (
            excess is not None and excess > 0.0
        ),
        "frequency_gate": {
            "completed_trade_min": 1_000,
            "unique_entry_session_min": 200,
            "validation_completed_trade_min": 200,
            "validation_unique_entry_session_min": 50,
            "validation_each_entry_year_min": 80,
            "passed": frequency_pass,
        },
    }
    if run_invalid:
        return _verdict(
            candidate,
            "RUN_INVALID",
            {
                **gates,
                "run_invalid": True,
                "maturity_close_missing_policy": "RUN_INVALID",
            },
            invalid_reasons,
            cost_profile=cost_profile,
        )
    if not frequency_pass:
        return _verdict(
            candidate, "FALSIFIED_FREQUENCY", gates, cost_profile=cost_profile
        )
    if coverage["validation_cohort_unavailable_count"]:
        return _verdict(
            candidate, "UNIDENTIFIABLE_COHORT", gates, cost_profile=cost_profile
        )
    if excess is None:
        return _verdict(
            candidate, "UNIDENTIFIABLE_COHORT", gates, cost_profile=cost_profile
        )
    if excess <= 0.0:
        return _verdict(
            candidate,
            "FALSIFIED_VALIDATION_COHORT_EXCESS",
            gates,
            cost_profile=cost_profile,
        )
    return _verdict(
        candidate,
        "NOT_FALSIFIED_EXPLORATORY_ONLY",
        gates,
        cost_profile=cost_profile,
    )


def _compose_rev_dual_falsification(
    candidate: CandidateBinding,
    profiles: RevCostProfileVerdicts,
) -> FalsificationVerdict:
    """Compose the frozen REV classification from independently evaluated costs."""

    base = profiles.base_10bp_per_side
    sensitivity = profiles.sensitivity_5bp_per_side
    cost_sensitive = (
        base.state == "FALSIFIED_VALIDATION_COHORT_EXCESS"
        and sensitivity.state == "NOT_FALSIFIED_EXPLORATORY_ONLY"
    )
    gates = {
        "dual_verdicts_are_distinct_objects": base is not sensitivity,
        "base_10bp_per_side_verdict_state": base.state,
        "sensitivity_5bp_per_side_verdict_state": sensitivity.state,
        "base_10bp_per_side_verdict": base.to_dict(),
        "sensitivity_5bp_per_side_verdict": sensitivity.to_dict(),
        "cost_sensitive_failure": cost_sensitive,
    }
    if base.state == sensitivity.state:
        state = base.state
    elif cost_sensitive:
        state = "FALSIFIED_COST_SENSITIVE"
    else:
        # The 10bp/side gate is the frozen decisive gate.  The independent
        # 5bp/side result remains serialized above rather than being folded
        # into a single predicate.
        state = base.state
    invalid_reasons = tuple(
        dict.fromkeys((*base.invalid_reasons, *sensitivity.invalid_reasons))
    )
    return _verdict(candidate, state, gates, invalid_reasons)


def _evaluate_volbreak(
    candidate: CandidateBinding,
    common: Mapping[str, Any],
    coverage: Mapping[str, int],
    validation: Sequence[CohortComparison],
) -> FalsificationVerdict:
    by_year = common["validation_completed_by_entry_year"]
    frequency_pass = (
        common["completed_trade_count"] >= 500
        and common["unique_entry_session_count"] >= 150
        and common["validation_completed_trade_count"] >= 100
        and common["validation_unique_entry_session_count"] >= 30
        and by_year["2023"] >= 40
        and by_year["2024"] >= 40
    )
    metrics = _validation_metrics(validation)
    base_excess = metrics["base_entry_session_equal_weighted_excess"]
    gates: dict[str, Any] = {
        **common,
        **coverage,
        **metrics,
        "frequency_gate": {
            "completed_trade_min": 500,
            "unique_entry_session_min": 150,
            "validation_completed_trade_min": 100,
            "validation_unique_entry_session_min": 30,
            "validation_each_entry_year_min": 40,
            "passed": frequency_pass,
        },
        "base_10bp_per_side_entry_session_equal_weighted_excess_gt_zero": (
            base_excess is not None and base_excess > 0.0
        ),
    }
    if not frequency_pass:
        return _verdict(candidate, "FALSIFIED_FREQUENCY", gates)
    if coverage["validation_cohort_unavailable_count"]:
        return _verdict(candidate, "UNIDENTIFIABLE_COHORT", gates)
    if base_excess is None:
        return _verdict(candidate, "UNIDENTIFIABLE_COHORT", gates)
    if base_excess <= 0.0:
        return _verdict(candidate, "FALSIFIED_VALIDATION_COHORT_EXCESS", gates)

    extreme = _volbreak_extreme_metrics(validation)
    gates.update(extreme)
    if extreme["top_1_percent_exclusion_entry_session_equal_weighted_excess"] is None:
        return _verdict(candidate, "UNIDENTIFIABLE_EXTREME_ANALYSIS", gates)
    if extreme["top_1_percent_excess_contribution_ratio"] is None:
        return _verdict(candidate, "UNIDENTIFIABLE_EXTREME_ANALYSIS", gates)
    if (
        extreme["top_1_percent_exclusion_entry_session_equal_weighted_excess"] <= 0.0
        or extreme["top_1_percent_excess_contribution_ratio"] > 0.5
    ):
        return _verdict(candidate, "FALSIFIED_EXTREME_DEPENDENCE", gates)
    return _verdict(candidate, "NOT_FALSIFIED_EXPLORATORY_ONLY", gates)


def _validation_metrics(
    validation: Sequence[CohortComparison],
) -> dict[str, float | None]:
    base = [item.base_excess_return for item in validation]
    sensitivity = [item.sensitivity_excess_return for item in validation]
    if any(value is None for value in base) or any(
        value is None for value in sensitivity
    ):
        return {
            "base_trade_weighted_excess": None,
            "base_entry_session_equal_weighted_excess": None,
            "sensitivity_trade_weighted_excess": None,
            "sensitivity_entry_session_equal_weighted_excess": None,
        }
    base_values = [float(value) for value in base if value is not None]
    sensitivity_values = [float(value) for value in sensitivity if value is not None]
    return {
        "base_trade_weighted_excess": _mean_or_none(base_values),
        "base_entry_session_equal_weighted_excess": _entry_session_mean(
            validation, field="base_excess_return"
        ),
        "sensitivity_trade_weighted_excess": _mean_or_none(sensitivity_values),
        "sensitivity_entry_session_equal_weighted_excess": _entry_session_mean(
            validation, field="sensitivity_excess_return"
        ),
    }


def _volbreak_extreme_metrics(
    validation: Sequence[CohortComparison],
) -> dict[str, Any]:
    if not validation or any(item.volume_ratio20 is None for item in validation):
        return {
            "top_1_percent_count": 0,
            "top_1_percent_selector": "ceil(validation_count * 0.01), volume_ratio20 desc then SHA bytes",
            "top_1_percent_exclusion_entry_session_equal_weighted_excess": None,
            "top_1_percent_excess_contribution_ratio": None,
        }
    ordered = sorted(
        validation,
        key=lambda item: (
            -float(item.volume_ratio20),
            bytes.fromhex(item.tie_break_sha256),
        ),
    )
    top_count = max(1, math.ceil(len(ordered) * 0.01))
    top = tuple(ordered[:top_count])
    top_identities = {_identity(item.entry_session, item.symbol) for item in top}
    remaining = tuple(
        item
        for item in validation
        if _identity(item.entry_session, item.symbol) not in top_identities
    )
    all_excess = [
        float(item.base_excess_return)
        for item in validation
        if item.base_excess_return is not None
    ]
    top_excess = [
        float(item.base_excess_return)
        for item in top
        if item.base_excess_return is not None
    ]
    total = sum(all_excess)
    contribution = sum(top_excess) / total if total > 0.0 else None
    return {
        "top_1_percent_count": top_count,
        "top_1_percent_selector": "ceil(validation_count * 0.01), volume_ratio20 desc then SHA bytes",
        "top_1_percent_exclusion_entry_session_equal_weighted_excess": _entry_session_mean(
            remaining, field="base_excess_return"
        ),
        "top_1_percent_excess_contribution_ratio": contribution,
    }


def _entry_session_mean(
    records: Sequence[CohortComparison],
    *,
    field: Literal["base_excess_return", "sensitivity_excess_return"],
) -> float | None:
    grouped: dict[date, list[float]] = defaultdict(list)
    for record in records:
        value = getattr(record, field)
        if value is None:
            return None
        grouped[record.entry_session].append(float(value))
    if not grouped:
        return None
    return mean(mean(values) for _, values in sorted(grouped.items()))


def _mean_or_none(values: Sequence[float]) -> float | None:
    return mean(values) if values else None


def _identity(entry_session: date | None, symbol: str) -> tuple[date | None, str]:
    return entry_session, symbol


def _verdict(
    candidate: CandidateBinding,
    state: VerdictState,
    gates: Mapping[str, Any],
    invalid_reasons: Sequence[str] = (),
    *,
    cost_profile: CostProfile | None = None,
) -> FalsificationVerdict:
    return FalsificationVerdict(
        strategy_id=candidate.strategy_id,
        contract_hash=candidate.contract_hash,
        labels=candidate.labels,
        state=state,
        falsification_literal=candidate.falsification,
        gate_results=dict(gates),
        invalid_reasons=tuple(invalid_reasons),
        cost_profile=cost_profile,
    )
