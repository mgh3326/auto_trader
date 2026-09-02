"""ROB-1340 durable evidence that one send authority actually ceased.

The positive verdict in this module is intentionally hard to obtain.  Empty
``pg_locks`` output, a closed pool handle, process exit, or a last-successful
release are never evidence by themselves.  The current cycle's committed start
journal is the enumeration source and every attempt that may have acquired a
key must be covered by one exact, committed cessation receipt.

Only the authority owner receives :class:`AuthorityCessationEvidencePort`.
Cycle/reporting code may read the resulting assessment but has no receipt
producer API.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

AUTHORITY_CESSATION_CONTRACT_VERSION: Final[str] = "rob1340.v1"
KIWOOM_AUTHORITY_LANE_ID: Final[str] = "kr.kiwoom.mock"


class AuthorityAttemptTerminalState(StrEnum):
    """Exactly one terminal state is required for every committed start row."""

    NO_KEY_ACQUIRED_PROVEN = "NO_KEY_ACQUIRED_PROVEN"
    CESSATION_RECEIPT_COMMITTED = "CESSATION_RECEIPT_COMMITTED"
    UNRESOLVED_HOLD = "UNRESOLVED_HOLD"


class AuthorityCessationKind(StrEnum):
    """Terminal evidence kinds; only the middle two are qualifying receipts."""

    NO_KEY_ACQUIRED_PROVEN = "no_key_acquired_proven"
    ADVISORY_UNLOCK = "advisory_unlock"
    BACKEND_TERMINATION = "backend_termination"
    UNRESOLVED_HOLD = "unresolved_hold"


class AuthorityReleaseStatus(StrEnum):
    RELEASE_VERIFIED = "RELEASE_VERIFIED"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"


@dataclass(frozen=True, slots=True)
class AuthorityAttemptContextV1:
    """Lineage supplied additively to the generic lease acquisition API."""

    lane_id: str
    cycle_id: str
    order_attempt_id: str


@dataclass(frozen=True, slots=True)
class AuthorityAttemptStartedV1:
    """Committed before the first ``pg_try_advisory_lock`` dispatch."""

    authority_attempt_id: str
    lane_id: str
    cycle_id: str
    order_attempt_id: str
    owner_binding_digest: str
    keyset_digest: str
    key_count: int
    baseline_matching_rows: int
    contract_version: str = AUTHORITY_CESSATION_CONTRACT_VERSION


@dataclass(frozen=True, slots=True)
class AuthorityAttemptTerminalV1:
    """One append-only terminal row for an authority attempt.

    ``claim_row_id`` is nullable only for acquisition rollback, which necessarily
    precedes claim reservation.  The lineage fields and attempt binding remain
    mandatory in that case.
    """

    authority_attempt_id: str
    lane_id: str
    cycle_id: str
    order_attempt_id: str
    claim_row_id: int | None
    owner_binding_digest: str
    keyset_digest: str
    key_count: int
    terminal_state: AuthorityAttemptTerminalState
    kind: AuthorityCessationKind
    lock_statement_dispatched: bool
    lock_definite_false: bool
    acquired_key_count: int
    in_flight_unknown: bool
    unlock_true_count: int
    post_release_matching_rows: int | None
    termination_returned_exact_true: bool | None
    observer_pid_absent: bool | None
    receipt_digest: str
    receipt_id: int | None = None
    committed: bool = False
    contract_version: str = AUTHORITY_CESSATION_CONTRACT_VERSION

    @property
    def is_qualifying_receipt(self) -> bool:
        return self.kind in {
            AuthorityCessationKind.ADVISORY_UNLOCK,
            AuthorityCessationKind.BACKEND_TERMINATION,
        }


@dataclass(frozen=True, slots=True)
class AuthorityReleaseAssessment:
    """Capability-free cycle verdict exposed to the observation layer."""

    cycle_id: str
    status: AuthorityReleaseStatus
    enumeration_complete: bool
    expected_attempt_ids: tuple[str, ...]
    committed_receipt_attempt_ids: tuple[str, ...]
    committed_receipt_refs: tuple[tuple[int, str], ...]
    active_hold_attempt_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def release_verified(self) -> bool:
        return self.status is AuthorityReleaseStatus.RELEASE_VERIFIED

    def canonical(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status.value,
            "enumeration_complete": self.enumeration_complete,
            "expected_attempt_ids": list(self.expected_attempt_ids),
            "committed_receipt_attempt_ids": list(self.committed_receipt_attempt_ids),
            "committed_receipts": [
                {"receipt_id": receipt_id, "receipt_digest": digest}
                for receipt_id, digest in self.committed_receipt_refs
            ],
            "active_hold_attempt_ids": list(self.active_hold_attempt_ids),
            "reasons": list(self.reasons),
        }


@runtime_checkable
class AuthorityCessationEvidencePort(Protocol):
    """Append-only producer port held exclusively by coordination internals."""

    async def assert_ready(self) -> None: ...

    async def record_started(
        self, started: AuthorityAttemptStartedV1, /
    ) -> AuthorityAttemptStartedV1: ...

    async def record_terminal(
        self, terminal: AuthorityAttemptTerminalV1, /
    ) -> AuthorityAttemptTerminalV1: ...

    async def release_assessment_for_cycle(
        self, *, cycle_id: str
    ) -> AuthorityReleaseAssessment: ...


def canonical_digest(payload: Mapping[str, object], *, domain: str) -> str:
    """SHA-256 over a domain-separated canonical JSON projection."""

    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + body).hexdigest()


def owner_binding_digest(*, backend_pid: int, database_oid: int, token: str) -> str:
    """Persist a binding, never the termination-capable raw PID/token pair."""

    return canonical_digest(
        {
            "backend_pid": backend_pid,
            "database_oid": database_oid,
            "connection_token": token,
        },
        domain="rob1340-owner-binding-v1",
    )


def keyset_digest(keys: Sequence[int]) -> str:
    return canonical_digest({"ordered_keys": list(keys)}, domain="rob1340-keyset-v1")


def terminal_receipt_digest(
    terminal: AuthorityAttemptTerminalV1,
) -> str:
    """Digest every immutable terminal field except DB id/commit projection."""

    return canonical_digest(
        {
            "contract_version": terminal.contract_version,
            "authority_attempt_id": terminal.authority_attempt_id,
            "lane_id": terminal.lane_id,
            "cycle_id": terminal.cycle_id,
            "order_attempt_id": terminal.order_attempt_id,
            "claim_row_id": terminal.claim_row_id,
            "owner_binding_digest": terminal.owner_binding_digest,
            "keyset_digest": terminal.keyset_digest,
            "key_count": terminal.key_count,
            "terminal_state": terminal.terminal_state.value,
            "kind": terminal.kind.value,
            "lock_statement_dispatched": terminal.lock_statement_dispatched,
            "lock_definite_false": terminal.lock_definite_false,
            "acquired_key_count": terminal.acquired_key_count,
            "in_flight_unknown": terminal.in_flight_unknown,
            "unlock_true_count": terminal.unlock_true_count,
            "post_release_matching_rows": terminal.post_release_matching_rows,
            "termination_returned_exact_true": (
                terminal.termination_returned_exact_true
            ),
            "observer_pid_absent": terminal.observer_pid_absent,
        },
        domain="rob1340-authority-terminal-v1",
    )


def _binding_matches(
    started: AuthorityAttemptStartedV1,
    terminal: AuthorityAttemptTerminalV1,
) -> bool:
    return (
        terminal.contract_version == started.contract_version
        and terminal.lane_id == started.lane_id
        and terminal.cycle_id == started.cycle_id
        and terminal.order_attempt_id == started.order_attempt_id
        and terminal.owner_binding_digest == started.owner_binding_digest
        and terminal.keyset_digest == started.keyset_digest
        and terminal.key_count == started.key_count
        and terminal.receipt_digest == terminal_receipt_digest(terminal)
    )


def _qualifies(
    started: AuthorityAttemptStartedV1,
    terminal: AuthorityAttemptTerminalV1,
) -> bool:
    if (
        terminal.committed is not True
        or type(terminal.receipt_id) is not int
        or terminal.receipt_id <= 0
        or not _binding_matches(started, terminal)
        or type(started.baseline_matching_rows) is not int
        or started.baseline_matching_rows != 0
        or terminal.terminal_state
        is not AuthorityAttemptTerminalState.CESSATION_RECEIPT_COMMITTED
        or terminal.lock_statement_dispatched is not True
        or terminal.lock_definite_false is not False
        or type(terminal.acquired_key_count) is not int
        or type(terminal.unlock_true_count) is not int
        or type(terminal.in_flight_unknown) is not bool
        or not (0 <= terminal.acquired_key_count <= terminal.key_count)
        or not (0 <= terminal.unlock_true_count <= terminal.acquired_key_count)
        or not (terminal.acquired_key_count > 0 or terminal.in_flight_unknown)
    ):
        return False
    if terminal.kind is AuthorityCessationKind.ADVISORY_UNLOCK:
        return (
            terminal.in_flight_unknown is False
            and terminal.acquired_key_count > 0
            and terminal.unlock_true_count == terminal.acquired_key_count
            and type(terminal.post_release_matching_rows) is int
            and terminal.post_release_matching_rows == 0
            and terminal.termination_returned_exact_true is None
            and terminal.observer_pid_absent is None
        )
    if terminal.kind is AuthorityCessationKind.BACKEND_TERMINATION:
        return (
            terminal.post_release_matching_rows is None
            and terminal.termination_returned_exact_true is True
            and terminal.observer_pid_absent is True
            and terminal.unlock_true_count <= terminal.acquired_key_count
        )
    return False


def verify_authority_cessation(
    *,
    cycle_id: str,
    attempts: Sequence[AuthorityAttemptStartedV1],
    terminals: Sequence[AuthorityAttemptTerminalV1],
    enumeration_complete: bool,
    history_hold_attempt_ids: Sequence[str] = (),
    active_hold_attempt_ids: Sequence[str] = (),
) -> AuthorityReleaseAssessment:
    """Verify complete current-cycle coverage; every negative fact is only a veto."""

    reasons: list[str] = []
    starts = [row for row in attempts if row.cycle_id == cycle_id]
    foreign_starts = [row for row in attempts if row.cycle_id != cycle_id]
    if foreign_starts:
        reasons.append("attempt_scope_mismatch")
        enumeration_complete = False

    start_by_id = {row.authority_attempt_id: row for row in starts}
    if len(start_by_id) != len(starts):
        reasons.append("duplicate_attempt_start")
        enumeration_complete = False
    if any(
        row.contract_version != AUTHORITY_CESSATION_CONTRACT_VERSION
        or row.lane_id != KIWOOM_AUTHORITY_LANE_ID
        or row.key_count <= 0
        for row in starts
    ):
        reasons.append("attempt_contract_mismatch")
        enumeration_complete = False

    terminal_by_attempt: dict[str, list[AuthorityAttemptTerminalV1]] = {}
    for row in terminals:
        terminal_by_attempt.setdefault(row.authority_attempt_id, []).append(row)
        if row.cycle_id != cycle_id or row.authority_attempt_id not in start_by_id:
            reasons.append("foreign_or_unbound_terminal")
            enumeration_complete = False

    if any(len(rows) != 1 for rows in terminal_by_attempt.values()):
        reasons.append("terminal_cardinality_not_one")
        enumeration_complete = False
    if set(terminal_by_attempt) != set(start_by_id):
        reasons.append("started_attempt_without_exact_terminal")
        enumeration_complete = False

    history_ids = tuple(sorted(set(history_hold_attempt_ids)))
    if any(attempt_id not in start_by_id for attempt_id in history_ids):
        reasons.append("process_hold_history_not_joined")
        enumeration_complete = False

    expected: set[str] = set()
    committed_receipts: set[str] = set()
    receipt_refs: list[tuple[int, str]] = []
    for attempt_id, started in start_by_id.items():
        rows = terminal_by_attempt.get(attempt_id)
        if rows is None or len(rows) != 1:
            # Missing terminal means acquisition outcome is unknown, so it must
            # never be excluded from E merely because local progress was lost.
            expected.add(attempt_id)
            continue
        terminal = rows[0]
        potentially_acquired = (
            terminal.acquired_key_count > 0 or terminal.in_flight_unknown
        )
        if potentially_acquired:
            expected.add(attempt_id)

        if terminal.kind is AuthorityCessationKind.NO_KEY_ACQUIRED_PROVEN:
            no_key_exact = (
                terminal.committed is True
                and type(terminal.receipt_id) is int
                and terminal.receipt_id > 0
                and _binding_matches(started, terminal)
                and terminal.terminal_state
                is AuthorityAttemptTerminalState.NO_KEY_ACQUIRED_PROVEN
                and type(terminal.acquired_key_count) is int
                and terminal.acquired_key_count == 0
                and terminal.in_flight_unknown is False
                and (
                    (
                        terminal.lock_statement_dispatched is False
                        and terminal.lock_definite_false is False
                    )
                    or (
                        terminal.lock_statement_dispatched is True
                        and terminal.lock_definite_false is True
                    )
                )
                and type(terminal.unlock_true_count) is int
                and terminal.unlock_true_count == 0
                and type(terminal.post_release_matching_rows) is int
                and terminal.post_release_matching_rows == 0
                and terminal.termination_returned_exact_true is None
                and terminal.observer_pid_absent is None
            )
            if not no_key_exact:
                reasons.append("no_key_terminal_not_proven")
                enumeration_complete = False
            continue

        if terminal.kind is AuthorityCessationKind.UNRESOLVED_HOLD:
            reasons.append("unresolved_hold_terminal")
            continue

        if _qualifies(started, terminal):
            committed_receipts.add(attempt_id)
            assert terminal.receipt_id is not None
            receipt_refs.append((terminal.receipt_id, terminal.receipt_digest))
        else:
            reasons.append("cessation_receipt_not_exact")

    active_ids = tuple(sorted(set(active_hold_attempt_ids)))
    if active_ids:
        reasons.append("unreleased_authority_hold_veto")
    if not expected:
        reasons.append("no_potentially_acquired_attempt")
    if committed_receipts != expected:
        reasons.append("committed_receipt_coverage_mismatch")
    if not enumeration_complete:
        reasons.append("enumeration_incomplete")

    verified = bool(
        expected
        and enumeration_complete
        and committed_receipts == expected
        and not active_ids
    )
    return AuthorityReleaseAssessment(
        cycle_id=cycle_id,
        status=(
            AuthorityReleaseStatus.RELEASE_VERIFIED
            if verified
            else AuthorityReleaseStatus.INCOMPLETE_EVIDENCE
        ),
        enumeration_complete=enumeration_complete,
        expected_attempt_ids=tuple(sorted(expected)),
        committed_receipt_attempt_ids=tuple(sorted(committed_receipts)),
        committed_receipt_refs=tuple(sorted(receipt_refs)),
        active_hold_attempt_ids=active_ids,
        reasons=tuple(dict.fromkeys(reasons)),
    )


__all__ = [
    "AUTHORITY_CESSATION_CONTRACT_VERSION",
    "KIWOOM_AUTHORITY_LANE_ID",
    "AuthorityAttemptContextV1",
    "AuthorityAttemptStartedV1",
    "AuthorityAttemptTerminalState",
    "AuthorityAttemptTerminalV1",
    "AuthorityCessationEvidencePort",
    "AuthorityCessationKind",
    "AuthorityReleaseAssessment",
    "AuthorityReleaseStatus",
    "canonical_digest",
    "keyset_digest",
    "owner_binding_digest",
    "terminal_receipt_digest",
    "verify_authority_cessation",
]
