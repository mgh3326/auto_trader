"""7-day shadow gate and 60-day paper promotion eligibility for ROB-850.

Both gates use FULL CALENDAR DAY semantics:
- <required_days → blocked
- =required_days → passes (if other conditions met)
- >required_days → passes

No actual 7/60-day waiting is performed. Gates are evaluated against
observed evidence using deterministic boundary tests.
"""

from __future__ import annotations

from datetime import datetime

from app.services.paper_evaluation.contracts import (
    GateType,
    GateVerdict,
    MinimumEvidence,
    ViewMetrics,
    ViewName,
)
from app.services.paper_evaluation.epoch import compute_calendar_days

_SHADOW_SOAK_REQUIRED_DAYS = 7
_PAPER_PROMOTION_REQUIRED_DAYS = 60


def evaluate_shadow_gate(
    *,
    shadow_started_at: datetime,
    evaluated_at: datetime,
) -> GateVerdict:
    """Evaluate the 7-day shadow soak gate.

    Returns a :class:`GateVerdict` with ``gate_type=SHADOW_SOAK``.
    ``passed`` is True iff at least 7 full calendar days have elapsed.
    """
    calendar_days = compute_calendar_days(shadow_started_at, evaluated_at)
    if calendar_days < _SHADOW_SOAK_REQUIRED_DAYS:
        return GateVerdict(
            gate_type=GateType.SHADOW_SOAK,
            calendar_days_observed=calendar_days,
            required_days=_SHADOW_SOAK_REQUIRED_DAYS,
            passed=False,
            reason_code="shadow_soak_incomplete",
            reason_text=f"{calendar_days} < 7 full calendar days",
        )
    return GateVerdict(
        gate_type=GateType.SHADOW_SOAK,
        calendar_days_observed=calendar_days,
        required_days=_SHADOW_SOAK_REQUIRED_DAYS,
        passed=True,
        reason_code="shadow_soak_complete",
        reason_text=f"{calendar_days} >= 7 full calendar days",
    )


def evaluate_paper_gate(
    *,
    paper_started_at: datetime,
    evaluated_at: datetime,
    config_hash: str,
    current_config_hash: str,
) -> GateVerdict:
    """Evaluate the 60-day paper promotion gate.

    Returns a :class:`GateVerdict` with ``gate_type=PAPER_PROMOTION``.
    ``passed`` is True iff at least 60 full calendar days have elapsed AND
    ``config_hash`` matches ``current_config_hash``. A config change
    mid-cohort invalidates accumulated evidence (it cannot be spliced), so
    the effective observed days for the current config is zero.
    """
    calendar_days = compute_calendar_days(paper_started_at, evaluated_at)
    if calendar_days < _PAPER_PROMOTION_REQUIRED_DAYS:
        return GateVerdict(
            gate_type=GateType.PAPER_PROMOTION,
            calendar_days_observed=calendar_days,
            required_days=_PAPER_PROMOTION_REQUIRED_DAYS,
            passed=False,
            reason_code="paper_promotion_incomplete",
            reason_text=f"{calendar_days} < 60 full calendar days",
        )
    if config_hash != current_config_hash:
        return GateVerdict(
            gate_type=GateType.PAPER_PROMOTION,
            calendar_days_observed=0,
            required_days=_PAPER_PROMOTION_REQUIRED_DAYS,
            passed=False,
            reason_code="config_changed_mid_cohort",
            reason_text="evaluation config changed mid-cohort; evidence cannot be spliced",
        )
    return GateVerdict(
        gate_type=GateType.PAPER_PROMOTION,
        calendar_days_observed=calendar_days,
        required_days=_PAPER_PROMOTION_REQUIRED_DAYS,
        passed=True,
        reason_code="paper_promotion_complete",
        reason_text=f"{calendar_days} >= 60 full calendar days",
    )


def evaluate_insufficient_evidence(
    *,
    view_metrics: dict[ViewName, ViewMetrics],
    minimum_evidence: MinimumEvidence,
) -> tuple[bool, list[str]]:
    """Check whether each view meets the minimum evidence thresholds.

    Returns ``(is_sufficient, reasons)``. Under the V1 ``fail_close`` policy
    any missing observation is itself disqualifying.
    """
    reasons: list[str] = []
    for view_name, metrics in view_metrics.items():
        if metrics.fill_count < minimum_evidence.min_fills:
            reasons.append(
                f"{view_name}: fill_count {metrics.fill_count} "
                f"< {minimum_evidence.min_fills}"
            )
        observation_count = metrics.fill_count + metrics.missing_observation_count
        if observation_count < minimum_evidence.min_observations:
            reasons.append(
                f"{view_name}: observation_count {observation_count} "
                f"< {minimum_evidence.min_observations}"
            )
        if metrics.missing_observation_count > 0:
            reasons.append(
                f"{view_name}: {metrics.missing_observation_count} "
                "missing observations"
            )
    if reasons:
        return (False, reasons)
    return (True, [])


__all__ = [
    "evaluate_insufficient_evidence",
    "evaluate_paper_gate",
    "evaluate_shadow_gate",
]
