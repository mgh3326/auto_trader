"""Terminal, append-only closure record for the sealed ROB-1301 experiment.

This module intentionally does not amend the predecessor pre-registration or
invoke its scorer.  Operator cessation is represented as a separate frozen
record, so the original spec and policy-projection seals remain independently
verifiable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.services.buy_gate_ab_shadow.spec import (
    EXPERIMENT_ID,
    PINNED_POLICY_PROJECTION_SHA256,
    PINNED_SPEC_SHA256,
    policy_projection_sha256,
    spec_sha256,
)

TerminationReason = Literal["STOPPED_BY_OPERATOR_DECISION"]
Carryover = Literal["forbidden"]

_ROB_1301_EPOCH_ID = "rob-1301-q6-collection-epoch.v1"
_TERMINATION_REASON: TerminationReason = "STOPPED_BY_OPERATOR_DECISION"
_CARRYOVER: Carryover = "forbidden"
_TERMINAL_STATUS = "INSUFFICIENT_SAMPLE"
_TERMINAL_OUTCOME = "NO_FIRING"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ExperimentTerminationError(ValueError):
    """The durable terminal record is malformed or no longer seals v1."""


@dataclass(frozen=True, slots=True)
class ExperimentTermination:
    """An immutable, fail-closed terminal record for one pre-registration."""

    experiment_id: str
    epoch_id: str
    terminated_at: datetime
    reason: TerminationReason
    decided_by: str
    carryover: Carryover
    terminal_status: str
    terminal_outcome: str
    preregistration_spec_sha256: str
    policy_projection_sha256: str

    def __post_init__(self) -> None:
        for field, value in (
            ("experiment_id", self.experiment_id),
            ("epoch_id", self.epoch_id),
            ("decided_by", self.decided_by),
            ("terminal_status", self.terminal_status),
            ("terminal_outcome", self.terminal_outcome),
        ):
            if type(value) is not str or not value:
                raise ExperimentTerminationError(
                    f"{field} must be a non-empty exact str"
                )
        if type(self.terminated_at) is not datetime:
            raise ExperimentTerminationError("terminated_at must be an exact datetime")
        if self.terminated_at.tzinfo is None or self.terminated_at.utcoffset() is None:
            raise ExperimentTerminationError("terminated_at must be timezone-aware")
        if self.experiment_id != EXPERIMENT_ID:
            raise ExperimentTerminationError("experiment_id must identify ROB-1301")
        if self.epoch_id != _ROB_1301_EPOCH_ID:
            raise ExperimentTerminationError(
                "epoch_id must identify the ROB-1301 epoch"
            )
        if self.reason != _TERMINATION_REASON:
            raise ExperimentTerminationError("termination reason is not permitted")
        if self.decided_by != "operator":
            raise ExperimentTerminationError("decided_by must be operator")
        if self.carryover != _CARRYOVER:
            raise ExperimentTerminationError("carryover must be forbidden")
        if self.terminal_status != _TERMINAL_STATUS:
            raise ExperimentTerminationError(
                "terminal_status must be INSUFFICIENT_SAMPLE"
            )
        if self.terminal_outcome != _TERMINAL_OUTCOME:
            raise ExperimentTerminationError("terminal_outcome must be NO_FIRING")
        for field, value in (
            ("preregistration_spec_sha256", self.preregistration_spec_sha256),
            ("policy_projection_sha256", self.policy_projection_sha256),
        ):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ExperimentTerminationError(f"{field} must be lowercase SHA-256")
        if self.preregistration_spec_sha256 != PINNED_SPEC_SHA256:
            raise ExperimentTerminationError(
                "pre-registration hash differs from ROB-1301 pin"
            )
        if self.policy_projection_sha256 != PINNED_POLICY_PROJECTION_SHA256:
            raise ExperimentTerminationError(
                "policy projection hash differs from ROB-1301 pin"
            )


# `date -Iseconds` emitted this exact value on the operator-decision record.
ROB_1301_TERMINATION = ExperimentTermination(
    experiment_id=EXPERIMENT_ID,
    epoch_id=_ROB_1301_EPOCH_ID,
    terminated_at=datetime.fromisoformat("2026-09-07T09:43:42+09:00"),
    reason=_TERMINATION_REASON,
    decided_by="operator",
    carryover=_CARRYOVER,
    terminal_status=_TERMINAL_STATUS,
    terminal_outcome=_TERMINAL_OUTCOME,
    preregistration_spec_sha256=PINNED_SPEC_SHA256,
    policy_projection_sha256=PINNED_POLICY_PROJECTION_SHA256,
)


def assert_predecessor_seal_intact() -> None:
    """Prove that closing ROB-1301 did not rewrite either sealed payload."""

    if spec_sha256() != PINNED_SPEC_SHA256:
        raise ExperimentTerminationError("ROB-1301 pre-registration seal is broken")
    if policy_projection_sha256() != PINNED_POLICY_PROJECTION_SHA256:
        raise ExperimentTerminationError("ROB-1301 policy-projection seal is broken")


def terminal_report(
    termination: ExperimentTermination = ROB_1301_TERMINATION,
) -> dict[str, Any]:
    """Return the only honest terminal envelope: no score was computed."""

    assert_predecessor_seal_intact()
    return {
        "experiment_id": termination.experiment_id,
        "epoch_id": termination.epoch_id,
        "status": termination.terminal_status,
        "outcome": termination.terminal_outcome,
        "score_computation": "not_applicable_stopped_by_operator_decision",
        "carryover": termination.carryover,
        "winner_declaration": "forbidden",
        "policy_implication": "none",
        "preregistration_spec_sha256": termination.preregistration_spec_sha256,
        "policy_projection_sha256": termination.policy_projection_sha256,
        "terminated_at": termination.terminated_at.isoformat(),
        "decided_by": termination.decided_by,
    }


__all__ = [
    "Carryover",
    "ExperimentTermination",
    "ExperimentTerminationError",
    "ROB_1301_TERMINATION",
    "TerminationReason",
    "assert_predecessor_seal_intact",
    "terminal_report",
]
