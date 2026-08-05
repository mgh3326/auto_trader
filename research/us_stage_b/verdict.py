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

__all__ = ["FalsificationVerdict", "evaluate_falsification"]


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "labels": list(self.labels),
            "state": self.state,
            "falsification_literal": self.falsification_literal,
            "gate_results": dict(self.gate_results),
            "invalid_reasons": list(self.invalid_reasons),
            "promotion": "FORBIDDEN",
        }


def evaluate_falsification(
    *,
    candidate: CandidateBinding,
    outcomes: Sequence[TradeOutcome],
    cohorts: Sequence[CohortComparison],
    run_invalid: bool,
    invalid_reasons: Sequence[str],
) -> FalsificationVerdict:
    """Apply only the frozen candidate's own falsification literal and gates."""

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
    if run_invalid:
        return _verdict(
            candidate,
            "RUN_INVALID",
            {
                **common,
                "run_invalid": True,
                "maturity_close_missing_policy": "RUN_INVALID",
            },
            invalid_reasons,
        )

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

    if candidate.strategy_id == "US-TS-MOM-CONT-Z126-H20-v1":
        return _evaluate_mom(candidate, common, coverage, resolved_validation)
    if candidate.strategy_id == "US-TS-REV-SHORT-Z3-T126-H3-v1":
        return _evaluate_rev(candidate, common, coverage, resolved_validation)
    if candidate.strategy_id == "US-TS-VOLBREAK-C55-V2-H10-v1":
        return _evaluate_volbreak(candidate, common, coverage, resolved_validation)
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


def _evaluate_rev(
    candidate: CandidateBinding,
    common: Mapping[str, Any],
    coverage: Mapping[str, int],
    validation: Sequence[CohortComparison],
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
    base_excess = metrics["base_entry_session_equal_weighted_excess"]
    sensitivity_excess = metrics["sensitivity_entry_session_equal_weighted_excess"]
    cost_sensitive = (
        base_excess is not None
        and sensitivity_excess is not None
        and base_excess <= 0.0
        and sensitivity_excess > 0.0
    )
    gates = {
        **common,
        **coverage,
        **metrics,
        "frequency_gate": {
            "completed_trade_min": 1_000,
            "unique_entry_session_min": 200,
            "validation_completed_trade_min": 200,
            "validation_unique_entry_session_min": 50,
            "validation_each_entry_year_min": 80,
            "passed": frequency_pass,
        },
        "base_10bp_per_side_entry_session_equal_weighted_excess_gt_zero": (
            base_excess is not None and base_excess > 0.0
        ),
        "sensitivity_5bp_per_side_entry_session_equal_weighted_excess_gt_zero": (
            sensitivity_excess is not None and sensitivity_excess > 0.0
        ),
        "cost_sensitive_failure": cost_sensitive,
    }
    if not frequency_pass:
        return _verdict(candidate, "FALSIFIED_FREQUENCY", gates)
    if coverage["validation_cohort_unavailable_count"]:
        return _verdict(candidate, "UNIDENTIFIABLE_COHORT", gates)
    if base_excess is None or sensitivity_excess is None:
        return _verdict(candidate, "UNIDENTIFIABLE_COHORT", gates)
    if base_excess <= 0.0:
        return _verdict(
            candidate,
            "FALSIFIED_COST_SENSITIVE"
            if cost_sensitive
            else "FALSIFIED_VALIDATION_COHORT_EXCESS",
            gates,
        )
    return _verdict(candidate, "NOT_FALSIFIED_EXPLORATORY_ONLY", gates)


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
) -> FalsificationVerdict:
    return FalsificationVerdict(
        strategy_id=candidate.strategy_id,
        contract_hash=candidate.contract_hash,
        labels=candidate.labels,
        state=state,
        falsification_literal=candidate.falsification,
        gate_results=dict(gates),
        invalid_reasons=tuple(invalid_reasons),
    )
