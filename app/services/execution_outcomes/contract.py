"""MCP-independent command and outcome algebra for broker mutations.

This package is a descriptive service-layer contract.  It does not execute a
broker call, persist a ledger row, register an MCP tool, or authorize a retry.
Callers may adopt it in later migrations without changing today's public
mutation surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class MutationOperation(StrEnum):
    """Broker mutation requested by an application command."""

    PLACE = "place"
    MODIFY = "modify"
    CANCEL = "cancel"


class BrokerAcceptance(StrEnum):
    """What broker evidence proves about mutation acceptance."""

    NOT_SENT = "not_sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class TrackingState(StrEnum):
    """Whether an accepted mutation has a stable broker tracking identity."""

    NOT_APPLICABLE = "not_applicable"
    TRACKED = "tracked"
    UNTRACKED = "untracked"
    UNKNOWN = "unknown"


class TerminalEvidence(StrEnum):
    """Execution evidence observed after mutation dispatch.

    ``PARTIAL_FILL`` is intentionally represented here because the migration
    AC names this field ``terminal_evidence``.  It is *not* terminal: the
    :attr:`MutationOutcome.evidence_finality` property classifies it as
    ``NON_TERMINAL`` and the constructor requires continued reconciliation.
    """

    NONE = "none"
    PARTIAL_FILL = "partial_fill"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class EvidenceFinality(StrEnum):
    """Whether the observed execution evidence closes the lifecycle."""

    NON_TERMINAL = "non_terminal"
    TERMINAL = "terminal"


class OutcomeNextAction(StrEnum):
    """Safe next step described by the outcome.

    There is deliberately no resend variant.  A retry is a separately
    authorized new command outside this leaf contract, never an automatic
    consequence of a transport or broker response.
    """

    NONE = "none"
    TRACK = "track"
    RECONCILE = "reconcile"
    REVIEW_NEW_COMMAND = "review_new_command"


_TERMINAL_EVIDENCE = frozenset(
    {
        TerminalEvidence.FILLED,
        TerminalEvidence.CANCELLED,
        TerminalEvidence.EXPIRED,
    }
)


def _required_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must be non-empty")
    return cleaned


@dataclass(frozen=True, slots=True)
class MutationCommand:
    """Neutral application command passed to a broker mutation port.

    ``designated_writer_ref`` is an opaque reference to the writer binding that
    was already validated under the ROB-1189 one-writer ownership contract.
    This algebra consumes that invariant as a precondition; it does not infer a
    physical account, enumerate writers, or reimplement cardinality checks.
    """

    command_id: str
    designated_writer_ref: str
    account_mode: str
    operation: MutationOperation
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "command_id", _required_text(self.command_id, "command_id")
        )
        object.__setattr__(
            self,
            "designated_writer_ref",
            _required_text(self.designated_writer_ref, "designated_writer_ref"),
        )
        object.__setattr__(
            self, "account_mode", _required_text(self.account_mode, "account_mode")
        )
        if not isinstance(self.operation, MutationOperation):
            raise TypeError("operation must be a MutationOperation")
        if self.idempotency_key is not None:
            object.__setattr__(
                self,
                "idempotency_key",
                _required_text(self.idempotency_key, "idempotency_key"),
            )


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    """Orthogonal facts about one broker mutation attempt.

    The type intentionally has no ``success`` field.  Request handling,
    dispatch, broker acceptance, tracking, local recording, reconciliation,
    and execution evidence remain separate facts.  In particular,
    ``ACCEPTED`` never implies ``FILLED``.
    """

    request_validated: bool
    mutation_sent: bool
    broker_acceptance: BrokerAcceptance
    tracking: TrackingState
    local_recorded: bool
    reconcile_required: bool
    terminal_evidence: TerminalEvidence

    def __post_init__(self) -> None:
        self._validate_types()
        self._validate_dispatch_and_acceptance()
        self._validate_tracking()
        self._validate_evidence()

    def _validate_types(self) -> None:
        for field_name in (
            "request_validated",
            "mutation_sent",
            "local_recorded",
            "reconcile_required",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be bool")
        if not isinstance(self.broker_acceptance, BrokerAcceptance):
            raise TypeError("broker_acceptance must be a BrokerAcceptance")
        if not isinstance(self.tracking, TrackingState):
            raise TypeError("tracking must be a TrackingState")
        if not isinstance(self.terminal_evidence, TerminalEvidence):
            raise TypeError("terminal_evidence must be a TerminalEvidence")

    def _validate_dispatch_and_acceptance(self) -> None:
        if self.mutation_sent and not self.request_validated:
            raise ValueError("an unvalidated request cannot be sent")
        if self.mutation_sent:
            if self.broker_acceptance is BrokerAcceptance.NOT_SENT:
                raise ValueError("sent mutation cannot have acceptance=not_sent")
        elif self.broker_acceptance is not BrokerAcceptance.NOT_SENT:
            raise ValueError("unsent mutation must have acceptance=not_sent")

        if (
            self.broker_acceptance
            in {
                BrokerAcceptance.NOT_SENT,
                BrokerAcceptance.REJECTED,
            }
            and self.reconcile_required
        ):
            raise ValueError("definite not-sent/rejected outcomes do not reconcile")

        if self.broker_acceptance is BrokerAcceptance.UNKNOWN:
            if not self.reconcile_required:
                raise ValueError("unknown acceptance requires reconciliation")
            if self.terminal_evidence is not TerminalEvidence.NONE:
                raise ValueError("unknown acceptance cannot claim execution evidence")

    def _validate_tracking(self) -> None:
        acceptance = self.broker_acceptance
        if acceptance is BrokerAcceptance.ACCEPTED:
            if self.tracking not in {TrackingState.TRACKED, TrackingState.UNTRACKED}:
                raise ValueError("accepted mutation must be tracked or untracked")
        elif acceptance is BrokerAcceptance.UNKNOWN:
            if self.tracking is not TrackingState.UNKNOWN:
                raise ValueError("unknown acceptance requires tracking=unknown")
        elif self.tracking is not TrackingState.NOT_APPLICABLE:
            raise ValueError("not-sent/rejected mutation has no tracking identity")

        if self.tracking is TrackingState.UNTRACKED and not self.reconcile_required:
            raise ValueError("accepted-untracked requires reconciliation")

    def _validate_evidence(self) -> None:
        if self.terminal_evidence is not TerminalEvidence.NONE:
            if self.broker_acceptance is not BrokerAcceptance.ACCEPTED:
                raise ValueError("execution evidence requires broker acceptance")
            if not self.mutation_sent:
                raise ValueError("execution evidence requires a sent mutation")

        if self.terminal_evidence is TerminalEvidence.PARTIAL_FILL:
            if not self.reconcile_required:
                raise ValueError("partial fill is non-terminal and must reconcile")

        if (
            self.terminal_evidence in _TERMINAL_EVIDENCE
            and not self.local_recorded
            and not self.reconcile_required
        ):
            raise ValueError(
                "terminal broker evidence not recorded locally must reconcile"
            )

    @property
    def evidence_finality(self) -> EvidenceFinality:
        if self.terminal_evidence in _TERMINAL_EVIDENCE:
            return EvidenceFinality.TERMINAL
        return EvidenceFinality.NON_TERMINAL

    @property
    def next_action(self) -> OutcomeNextAction:
        if self.reconcile_required:
            return OutcomeNextAction.RECONCILE
        if self.evidence_finality is EvidenceFinality.TERMINAL:
            return OutcomeNextAction.NONE
        if self.broker_acceptance is BrokerAcceptance.ACCEPTED:
            return OutcomeNextAction.TRACK
        return OutcomeNextAction.REVIEW_NEW_COMMAND

    @property
    def is_filled(self) -> bool:
        return self.terminal_evidence is TerminalEvidence.FILLED

    @property
    def automatic_resend_allowed(self) -> Literal[False]:
        """This descriptive leaf contract never authorizes automatic resend."""

        return False

    def to_dict(self) -> dict[str, bool | str]:
        """Return a JSON-ready representation used by contract fixtures."""

        return {
            "request_validated": self.request_validated,
            "mutation_sent": self.mutation_sent,
            "broker_acceptance": self.broker_acceptance.value,
            "tracking": self.tracking.value,
            "local_recorded": self.local_recorded,
            "reconcile_required": self.reconcile_required,
            "terminal_evidence": self.terminal_evidence.value,
            "evidence_finality": self.evidence_finality.value,
            "next_action": self.next_action.value,
            "is_filled": self.is_filled,
            "automatic_resend_allowed": self.automatic_resend_allowed,
        }


__all__ = [
    "BrokerAcceptance",
    "EvidenceFinality",
    "MutationCommand",
    "MutationOperation",
    "MutationOutcome",
    "OutcomeNextAction",
    "TerminalEvidence",
    "TrackingState",
]
