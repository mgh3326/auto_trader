"""Kiwoom B0-X ORDERING lease (diagnostic flock) + J3A coordination adapter.

This module intentionally does not reimplement J3A PostgreSQL advisory SQL, key
math, reservation, cancellation shielding, or reason enums.  Those live in
:mod:`app.services.mock_integration.coordination` and are imported by exact
symbol.  The host-local ``flock`` remains a diagnostic writer marker and
**cannot authorize a send**.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

from app.schemas.execution_contracts import LaneStatus
from app.services.brokers.client_order_ids import BrokerClientIdTarget
from app.services.brokers.kiwoom import constants as kiwoom_constants
from app.services.brokers.kiwoom.client import KiwoomMockClient
from app.services.mock_integration.coordination import (
    AccountUncertaintyGatePort,
    ClaimFollowupRequest,
    CoordinatedMutationResult,
    CoordinationReasonCode,
    CoordinationScope,
    DispatchEvidence,
    DispatchEvidencePort,
    DurableSendClaimAdapter,
    MutationCallbackResult,
    MutationCertainty,
    TerminalClaimEvidence,
    coordinate_mock_order_mutation,
    describe_claim_followup,
)
from app.services.mock_integration.lineage import (
    DecisionIntentDraft,
    ExecutionPlanDraft,
    LineageEnvelope,
    LineagePersistencePort,
    MockLineageFactory,
    OrderAttemptDraft,
)
from app.services.mock_lane_registry import (
    LaneGuardError,
    LaneRegistryEntry,
    RegistrySource,
)
from app.services.order_send_intent_service import DuplicateOrderIntent

ORDERING_EVENT_JOURNAL_NAME: Final[str] = "ordering-events.jsonl"


class AccountWriterLeaseContended(RuntimeError):
    """Another local process holds this account's ORDERING writer lease."""


class AccountWriterLeaseLost(RuntimeError):
    """A caller reached a mutation boundary without its lease still held."""


class OrderingJournalUnreadable(RuntimeError):
    """A lifecycle journal exists but cannot be parsed as append-only evidence."""


class WriterLease(Protocol):
    """Injection seam used by tests; production uses :class:`AccountWriterLease`."""

    def acquire(self) -> None: ...

    def assert_held(self) -> None: ...

    def release(self) -> None: ...

    def canonical(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class AccountWriterLease:
    """A non-blocking local lease keyed by the redacted account fingerprint.

    The lock lives beneath the B0-X artifact root rather than an operator repo,
    so normal runtime activity cannot dirty a PR-only checkout.  The raw
    account identifier never reaches this class: the public fingerprint from
    ``account_identity_summary`` is already a one-way digest.
    """

    root: Path
    lane: str
    account_fingerprint: str
    _handle: int | None = None
    _token: str | None = None

    @property
    def lock_path(self) -> Path:
        digest = hashlib.sha256(self.account_fingerprint.encode()).hexdigest()[:16]
        return Path(self.root).expanduser() / self.lane / f".{digest}.ordering.lock"

    @property
    def acquired(self) -> bool:
        return self._handle is not None and self._token is not None

    def acquire(self) -> None:
        if self.acquired:
            raise RuntimeError("account writer lease is already acquired")
        path = self.lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(handle)
            raise AccountWriterLeaseContended(
                "kiwoom_mock ORDERING account writer lease is held by another "
                f"local process ({path}); refusing all mutations"
            ) from exc

        token = uuid.uuid4().hex
        try:
            os.ftruncate(handle, 0)
            os.write(
                handle,
                (
                    f"pid={os.getpid()}\n"
                    f"lane={self.lane}\n"
                    f"account_fingerprint={self.account_fingerprint}\n"
                    f"lease_token_sha256={hashlib.sha256(token.encode()).hexdigest()[:16]}\n"
                ).encode(),
            )
        except BaseException:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)
            raise
        self._handle = handle
        self._token = token

    def assert_held(self) -> None:
        if not self.acquired:
            raise AccountWriterLeaseLost(
                "kiwoom_mock ORDERING account writer lease is no longer held; "
                "the mutation boundary is closed"
            )

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        self._token = None
        if handle is None:
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)

    def canonical(self) -> dict[str, Any]:
        return {
            "acquired": self.acquired,
            "authority": "host_local_fcntl_account_keyed",
            "authorizes_send": False,
            "lock_path": str(self.lock_path),
            "account_fingerprint": self.account_fingerprint,
            "checked_before_each_mutation": True,
        }


@dataclass(frozen=True, slots=True)
class OrderingEventJournal:
    """Append-only per-event evidence for the ORDERING lifecycle."""

    path: Path

    @classmethod
    def for_lane(cls, *, root: Path, lane: str) -> OrderingEventJournal:
        return cls(path=Path(root).expanduser() / lane / ORDERING_EVENT_JOURNAL_NAME)

    def append(self, event: dict[str, Any]) -> None:
        if not isinstance(event.get("at"), str) or not event["at"].strip():
            raise ValueError("ordering lifecycle event requires a non-empty timestamp")
        if not isinstance(event.get("event"), str) or not event["event"].strip():
            raise ValueError("ordering lifecycle event requires a non-empty event name")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_all(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        events: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise TypeError("ordering lifecycle row is not an object")
                if not isinstance(payload.get("at"), str) or not payload["at"].strip():
                    raise ValueError("ordering lifecycle row has no timestamp")
                if (
                    not isinstance(payload.get("event"), str)
                    or not payload["event"].strip()
                ):
                    raise ValueError("ordering lifecycle row has no event name")
                events.append(payload)
        except Exception as exc:  # noqa: BLE001 — corrupt evidence is never empty
            raise OrderingJournalUnreadable(
                f"ordering lifecycle journal at {self.path} is unreadable "
                f"({type(exc).__name__})"
            ) from exc
        return tuple(events)


# ---------------------------------------------------------------------------
# ROB-1264 — thin Kiwoom adapter around merged J3A / J2A / J2B public ports
# ---------------------------------------------------------------------------

KIWOOM_CANONICAL_LANE_ID: Final[str] = "kr.kiwoom.mock"
KIWOOM_LIFECYCLE_STATUS: Final[str] = LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE.value
UNKNOWN_PENDING_RECONCILE: Final[str] = "unknown_pending_reconcile"
CALLER_DERIVED_IDENTITY_REJECTED: Final[str] = "caller_derived_identity_rejected"
ROOT_PATH_IDENTITY_REJECTED: Final[str] = "root_path_identity_rejected"
ACCOUNT_SUMMARY_FINGERPRINT_IDENTITY_REJECTED: Final[str] = (
    "account_summary_fingerprint_identity_rejected"
)
LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND: Final[str] = "local_flock_cannot_authorize_send"
PROXIMITY_ATTRIBUTION_REJECTED: Final[str] = "proximity_attribution_rejected"
KT00009_CANNOT_REPLACE_KT00007: Final[str] = "kt00009_cannot_replace_kt00007"
JSONL_ABSENCE_NOT_EMPTY_OWNERSHIP: Final[str] = "jsonl_absence_not_empty_ownership"
TRANSPORT_GATE_REJECTED: Final[str] = "transport_gate_rejected"

KIWOOM_RECOVERY_OWNER: Final[str] = (
    "scripts.b0x.kr.kiwoom_ordering.KiwoomCoordinationAdapter"
)
KIWOOM_RESTART_TRIGGER: Final[str] = (
    "process_restart_rediscovers_durable_j2b_claims_for_physical_account"
)
KIWOOM_READBACK_OPERATION: Final[str] = "kt00007"
KIWOOM_RELEASE_IF_MATCHES: Final[str] = (
    "DurableSendClaimAdapter.release_with_terminal_evidence("
    "TerminalClaimEvidence(lane_native_terminal_evidence=True, "
    "account_position_reconciled=True, remainder_known=True) "
    "or authoritative_absence_proven=True after pre-send NOT_CREATED)"
)

_KIWOOM_ALLOWED_HOST: Final[str] = "mockapi.kiwoom.com"


class KiwoomIdentityRejected(RuntimeError):
    """A caller-derived identity was offered in place of J2A physical_account_id."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class KiwoomTransportGateRejected(RuntimeError):
    """A send was refused before transport because a K-5 check failed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class KiwoomAttributionRejected(RuntimeError):
    """A heuristic or non-kt00007 source was offered as ownership/fill truth."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class KiwoomSendNotAuthorized(RuntimeError):
    """No J3A grant is present; a diagnostic flock cannot authorize transport."""

    def __init__(self, reason: str = LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND) -> None:
        self.reason = reason
        super().__init__(reason)


def require_j2a_physical_account_id(
    entry: LaneRegistryEntry,
    *,
    caller_physical_account_id: str | None = None,
    root_path: str | Path | None = None,
    account_summary_fingerprint: str | None = None,
) -> str:
    """Return the canonical J2A identity. Caller/root/summary mutants are rejected."""

    if caller_physical_account_id is not None:
        raise KiwoomIdentityRejected(CALLER_DERIVED_IDENTITY_REJECTED)
    if root_path is not None:
        raise KiwoomIdentityRejected(ROOT_PATH_IDENTITY_REJECTED)
    if account_summary_fingerprint is not None:
        # Diagnostic fingerprints may be *recorded*, never used as the identity
        # argument. Passing one here is the mutant this function exists to kill.
        raise KiwoomIdentityRejected(ACCOUNT_SUMMARY_FINGERPRINT_IDENTITY_REJECTED)
    physical_account_id = entry.physical_account_id
    if (
        not isinstance(physical_account_id, str)
        or not physical_account_id.strip()
        or entry.identity_status == "UNKNOWN"
    ):
        raise LaneGuardError("physical_account_identity_unknown", lane_id=entry.lane_id)
    return physical_account_id


def actual_kiwoom_client(account: object) -> object | None:
    """The object that would actually speak HTTP, if the account wraps one."""

    return getattr(account, "_client", None)


def assert_kiwoom_transport_ready(
    *,
    account: object,
    entry: LaneRegistryEntry,
    physical_account_id: str,
    grant_owned: bool,
    diagnostic_fingerprint: str | None = None,
) -> None:
    """K-5: refuse transport unless every actual-client check holds."""

    if grant_owned is not True:
        raise KiwoomTransportGateRejected(LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND)
    client = actual_kiwoom_client(account)
    if type(client) is not KiwoomMockClient:
        raise KiwoomTransportGateRejected(TRANSPORT_GATE_REJECTED)
    if getattr(client, "_base_url", None) != kiwoom_constants.MOCK_BASE_URL:
        raise KiwoomTransportGateRejected(TRANSPORT_GATE_REJECTED)
    if entry.lane_id != KIWOOM_CANONICAL_LANE_ID:
        raise KiwoomTransportGateRejected(TRANSPORT_GATE_REJECTED)
    if entry.account_profile != "mock" or entry.account_mode.value != "mock":
        raise KiwoomTransportGateRejected(TRANSPORT_GATE_REJECTED)
    if _KIWOOM_ALLOWED_HOST not in entry.allowed_hosts:
        raise KiwoomTransportGateRejected(TRANSPORT_GATE_REJECTED)
    if entry.physical_account_id != physical_account_id:
        raise KiwoomTransportGateRejected(TRANSPORT_GATE_REJECTED)
    if (
        diagnostic_fingerprint is not None
        and entry.fingerprint_evidence_ref is not None
        and diagnostic_fingerprint != entry.fingerprint_evidence_ref
    ):
        raise KiwoomTransportGateRejected(TRANSPORT_GATE_REJECTED)


def assert_broker_client_id_contract(envelope: LineageEnvelope) -> None:
    """Kiwoom CID pair is both None; J2B internal idempotency is non-blank."""

    attempt = envelope.order_attempt
    if attempt is None:
        raise KiwoomSendNotAuthorized("order_attempt_missing")
    if envelope.broker_client_id_target is not None:
        raise KiwoomSendNotAuthorized("broker_client_id_target_must_be_none")
    if attempt.broker_client_order_id is not None:
        raise KiwoomSendNotAuthorized("broker_client_order_id_must_be_none")
    if type(attempt.idempotency_key) is not str or not attempt.idempotency_key.strip():
        raise KiwoomSendNotAuthorized("internal_idempotency_required")
    if set(BrokerClientIdTarget) != {
        BrokerClientIdTarget.TOSS,
        BrokerClientIdTarget.BINANCE_SPOT_DEMO,
        BrokerClientIdTarget.ALPACA_PAPER,
    }:
        raise KiwoomSendNotAuthorized("broker_client_id_target_enum_changed")


def normalize_kt00007_state(status: str) -> str:
    lowered = status.strip().lower()
    mapping = {
        "open": "open",
        "accepted": "open",
        "new": "open",
        "partial": "partial",
        "partially_filled": "partial",
        "filled": "filled",
        "complete": "filled",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "rejected": "rejected",
        "expired": "expired",
    }
    return mapping.get(lowered, "unknown")


@dataclass(frozen=True, slots=True)
class NativeBrokerRow:
    """Exact kt00007 row keyed by a known native broker_order_id."""

    broker_order_id: str
    normalized_state: str
    filled_quantity: int | None
    remaining_quantity: int | None
    raw_row: Mapping[str, Any]


def native_row_from_kt00007(
    rows: Sequence[Mapping[str, Any]], *, broker_order_id: str
) -> NativeBrokerRow | None:
    """Return the exact native row, or None. Never invents from proximity."""

    if type(broker_order_id) is not str or not broker_order_id.strip():
        raise KiwoomAttributionRejected(PROXIMITY_ATTRIBUTION_REJECTED)
    matches = [
        row for row in rows if str(row.get("order_id") or "").strip() == broker_order_id
    ]
    if len(matches) != 1:
        return None
    row = matches[0]
    remaining = row.get("remaining_quantity")
    filled = row.get("filled_quantity")
    return NativeBrokerRow(
        broker_order_id=broker_order_id,
        normalized_state=normalize_kt00007_state(str(row.get("status") or "")),
        filled_quantity=None if filled is None else int(filled),
        remaining_quantity=None if remaining is None else int(remaining),
        raw_row=dict(row),
    )


def reject_proximity_attribution() -> None:
    raise KiwoomAttributionRejected(PROXIMITY_ATTRIBUTION_REJECTED)


def reject_kt00009_as_truth() -> None:
    raise KiwoomAttributionRejected(KT00009_CANNOT_REPLACE_KT00007)


def reject_jsonl_as_empty_ownership() -> None:
    raise KiwoomAttributionRejected(JSONL_ABSENCE_NOT_EMPTY_OWNERSHIP)


@dataclass(frozen=True, slots=True)
class RestartDisposition:
    status: str
    block_physical_account: bool
    allow_repost: bool
    native: NativeBrokerRow | None
    reason: str


def restart_disposition(
    *,
    durable_broker_order_id: str | None,
    kt00007_readable: bool,
    kt00007_rows: Sequence[Mapping[str, Any]] = (),
    pre_send_not_created: bool = False,
    jsonl_missing: bool = False,
    jsonl_corrupt: bool = False,
) -> RestartDisposition:
    """Restart rules from artifact K-3 / K-4. JSONL never authorizes release."""

    if pre_send_not_created and durable_broker_order_id is None:
        return RestartDisposition(
            status="not_created",
            block_physical_account=False,
            allow_repost=False,
            native=None,
            reason="authoritative_pre_send_not_created",
        )
    if jsonl_missing or jsonl_corrupt:
        if durable_broker_order_id is None:
            reject_jsonl_as_empty_ownership()
    if durable_broker_order_id is None:
        return RestartDisposition(
            status=UNKNOWN_PENDING_RECONCILE,
            block_physical_account=True,
            allow_repost=False,
            native=None,
            reason="unresolved_pre_send_claim_without_durable_broker_order_id",
        )
    if not kt00007_readable:
        return RestartDisposition(
            status=UNKNOWN_PENDING_RECONCILE,
            block_physical_account=True,
            allow_repost=False,
            native=None,
            reason="kt00007_read_unavailable",
        )
    native = native_row_from_kt00007(
        kt00007_rows, broker_order_id=durable_broker_order_id
    )
    if native is None:
        return RestartDisposition(
            status=UNKNOWN_PENDING_RECONCILE,
            block_physical_account=True,
            allow_repost=False,
            native=None,
            reason="exact_native_row_absent",
        )
    return RestartDisposition(
        status="recovered_from_j2b_and_kt00007",
        block_physical_account=False,
        allow_repost=False,
        native=native,
        reason="exact_broker_order_id_and_kt00007",
    )


def wall_clock_cannot_release() -> TerminalClaimEvidence:
    """A default evidence instance authorizes nothing — including DAY expiry."""

    return TerminalClaimEvidence()


def followup_precheck(
    *,
    operation: str,
    native_order_id: str | None,
    known_remainder: Decimal | None,
    fresh_guards_passed: bool,
) -> bool:
    """True only when cancel/reduce preconditions other than the live grant hold."""

    capability = describe_claim_followup(
        ClaimFollowupRequest(
            operation=operation,
            lane_capability_supports_operation=True,
            attributed_native_order_id=native_order_id,
            known_remainder=known_remainder,
            fresh_guards_passed=fresh_guards_passed,
            lease_ownership_verified=True,
        )
    )
    return capability.capability_present


@dataclass
class InMemoryLineagePersistence:
    """Lane-owned lineage store for offline tests and the adapter's own memory."""

    envelopes: list[LineageEnvelope] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    async def persist(self, envelope: LineageEnvelope, /) -> None:
        self.envelopes.append(envelope)
        attempt = envelope.order_attempt
        if attempt is not None and attempt.broker_order_id is not None:
            self.events.append("j2b_ack_persisted")
        else:
            self.events.append("j2b_attempt_persisted")


@dataclass
class InMemoryDispatchEvidence:
    records: list[DispatchEvidence] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    async def persist_dispatch_evidence(self, evidence: DispatchEvidence, /) -> None:
        self.records.append(evidence)
        self.events.append(f"dispatch_{evidence.kind.value}")


@dataclass
class InMemoryUncertaintyGate:
    """Account-wide unresolved-outcome gate. Uncorrelated claims block the account."""

    unresolved_scopes: set[str] = field(default_factory=set)
    events: list[str] = field(default_factory=list)

    async def has_unresolved_account_uncertainty(
        self, *, claim_account_scope: str
    ) -> bool:
        self.events.append(f"uncertainty:{claim_account_scope}")
        return claim_account_scope in self.unresolved_scopes

    def mark_unresolved(self, claim_account_scope: str) -> None:
        self.unresolved_scopes.add(claim_account_scope)

    def clear(self, claim_account_scope: str) -> None:
        self.unresolved_scopes.discard(claim_account_scope)


@dataclass
class InMemoryReservationPort:
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)
    _next_id: int = 1

    async def reserve(
        self,
        *,
        account_scope: str,
        idempotency_key: str,
        symbol: str | None = None,
        side: str | None = None,
        conflicting_key_sides: tuple[tuple[str, str], ...] = (),
    ) -> int:
        del conflicting_key_sides
        for row in self.rows.values():
            if (
                row["account_scope"] == account_scope
                and row["idempotency_key"] == idempotency_key
            ):
                raise DuplicateOrderIntent("duplicate reservation")
        row_id = self._next_id
        self._next_id += 1
        self.rows[row_id] = {
            "account_scope": account_scope,
            "idempotency_key": idempotency_key,
            "symbol": symbol,
            "side": side,
        }
        return row_id

    async def list_reservations(self, *, account_scope: str) -> Sequence[Any]:
        return [
            row for row in self.rows.values() if row["account_scope"] == account_scope
        ]

    async def release_if_matches(
        self,
        *,
        account_scope: str,
        row_id: int,
        idempotency_key: str,
        side: str | None,
    ) -> int:
        row = self.rows.get(row_id)
        if (
            row is None
            or row["account_scope"] != account_scope
            or row["idempotency_key"] != idempotency_key
            or row["side"] != side
        ):
            return 0
        del self.rows[row_id]
        return 1


@dataclass
class KiwoomCoordinationPorts:
    persistence: LineagePersistencePort
    dispatch_evidence: DispatchEvidencePort
    uncertainty_gate: AccountUncertaintyGatePort
    claims: DurableSendClaimAdapter
    connection_factory: Callable[[], Awaitable[Any]]
    registry: RegistrySource
    lineage_factory: MockLineageFactory
    entry: LaneRegistryEntry
    diagnostic_fingerprint: str | None = None


class KiwoomCoordinationAdapter:
    """Thin Kiwoom consumer of ``coordinate_mock_order_mutation``.

    Recovery owner (exactly one): this adapter.  It does not copy J3A lock
    SQL, key derivation, reservation, or reason enums.
    """

    recovery_owner: Final[str] = KIWOOM_RECOVERY_OWNER
    restart_trigger: Final[str] = KIWOOM_RESTART_TRIGGER
    readback_operation: Final[str] = KIWOOM_READBACK_OPERATION
    release_if_matches_condition: Final[str] = KIWOOM_RELEASE_IF_MATCHES
    blocked_state: Final[str] = KIWOOM_LIFECYCLE_STATUS

    def __init__(self, ports: KiwoomCoordinationPorts) -> None:
        self.ports = ports
        self.physical_account_id = require_j2a_physical_account_id(ports.entry)
        self.fence_rechecks: list[str] = []
        self.transport_calls: list[str] = []
        self.ordered_events: list[str] = []
        self.jsonl_appends: list[dict[str, Any]] = []
        self.last_result: CoordinatedMutationResult | None = None

    @property
    def policy_binding(self) -> Any:
        binding = self.ports.entry.policy_binding
        if binding is None:
            raise KiwoomSendNotAuthorized("policy_binding_missing")
        return binding

    def record_lane_evidence(self, kind: str, **payload: Any) -> dict[str, Any]:
        record = {"kind": kind, "at": datetime.now(UTC).isoformat(), **payload}
        self.ordered_events.append(f"lane_evidence:{kind}")
        return record

    async def reassert_before_mutation(
        self, scope: CoordinationScope, *, action: str
    ) -> None:
        await scope.assert_owned()
        self.fence_rechecks.append(action)

    async def submit_planned(
        self,
        account: Any,
        *,
        planned: Any,
        record_order_no: Callable[..., None] | None = None,
        policy_version: str,
        policy_version_hash: str,
        now: datetime,
        mutation: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> CoordinatedMutationResult:
        """One POST behind J3A. JSONL is appended only after J2B ACK persist."""

        envelope = self._attempt_envelope(
            planned,
            policy_version=policy_version,
            policy_version_hash=policy_version_hash,
        )
        assert_broker_client_id_contract(envelope)
        self.ordered_events.append("j2b_attempt_built")

        async def _callback(scope: CoordinationScope) -> MutationCallbackResult:
            await self.reassert_before_mutation(
                scope, action=f"post:{planned.order_key}"
            )
            assert_kiwoom_transport_ready(
                account=account,
                entry=self.ports.entry,
                physical_account_id=self.physical_account_id,
                grant_owned=True,
                diagnostic_fingerprint=self.ports.diagnostic_fingerprint,
            )
            submit = mutation
            if submit is None:
                if planned.side == "buy":
                    submit = account.place_limit_buy
                elif planned.side == "sell":
                    submit = account.place_limit_sell
                else:
                    raise ValueError(f"unsupported side: {planned.side}")
            self.transport_calls.append(f"post:{planned.order_key}")
            payload = await submit(
                symbol=planned.symbol,
                quantity=planned.quantity,
                price=planned.price,
            )
            order_no = str(payload.get("ord_no") or payload.get("order_no") or "")
            if not order_no.strip():
                return MutationCallbackResult(certainty=MutationCertainty.UNCERTAIN)
            self.record_lane_evidence("ack", broker_order_id=order_no)
            return MutationCallbackResult(
                certainty=MutationCertainty.DEFINITIVE, broker_order_id=order_no
            )

        result = await coordinate_mock_order_mutation(
            envelope=envelope,
            persistence=self.ports.persistence,
            dispatch_evidence=self.ports.dispatch_evidence,
            uncertainty_gate=self.ports.uncertainty_gate,
            claims=self.ports.claims,
            connection_factory=self.ports.connection_factory,
            mutation=_callback,
            registry=self.ports.registry,
            lineage_factory=self.ports.lineage_factory,
        )
        self.last_result = result
        self.ordered_events.append("j2b_ack_persisted")
        broker_order_id = result.evidence.broker_order_id
        if broker_order_id is not None and record_order_no is not None:
            record_order_no(order_no=broker_order_id, planned=planned, at=now)
            self.jsonl_appends.append(
                {"order_no": broker_order_id, "order_key": planned.order_key}
            )
            self.ordered_events.append("jsonl_appended")
            self.record_lane_evidence(
                "fidelity_journal", broker_order_id=broker_order_id
            )
        return result

    async def cancel_attributed(
        self,
        account: Any,
        *,
        planned: Any,
        native_order_id: str,
        known_remainder: Decimal,
        policy_version: str,
        policy_version_hash: str,
        mutation: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> CoordinatedMutationResult:
        if not followup_precheck(
            operation="cancel",
            native_order_id=native_order_id,
            known_remainder=known_remainder,
            fresh_guards_passed=True,
        ):
            raise KiwoomSendNotAuthorized(
                CoordinationReasonCode.CLAIM_FOLLOWUP_NOT_AUTHORIZED.value
            )
        envelope = self._attempt_envelope(
            planned,
            policy_version=policy_version,
            policy_version_hash=policy_version_hash,
            cycle_suffix=":cancel",
        )

        async def _callback(scope: CoordinationScope) -> MutationCallbackResult:
            await self.reassert_before_mutation(
                scope, action=f"cancel:{native_order_id}"
            )
            assert_kiwoom_transport_ready(
                account=account,
                entry=self.ports.entry,
                physical_account_id=self.physical_account_id,
                grant_owned=True,
                diagnostic_fingerprint=self.ports.diagnostic_fingerprint,
            )
            cancel = mutation or account.cancel
            self.transport_calls.append(f"cancel:{native_order_id}")
            payload = await cancel(
                original_order_no=native_order_id,
                symbol=planned.symbol,
                cancel_quantity=int(known_remainder),
            )
            cancel_no = str(payload.get("ord_no") or native_order_id)
            self.record_lane_evidence("cancel", broker_order_id=cancel_no)
            return MutationCallbackResult(
                certainty=MutationCertainty.DEFINITIVE, broker_order_id=cancel_no
            )

        result = await coordinate_mock_order_mutation(
            envelope=envelope,
            persistence=self.ports.persistence,
            dispatch_evidence=self.ports.dispatch_evidence,
            uncertainty_gate=self.ports.uncertainty_gate,
            claims=self.ports.claims,
            connection_factory=self.ports.connection_factory,
            mutation=_callback,
            registry=self.ports.registry,
            lineage_factory=self.ports.lineage_factory,
        )
        self.last_result = result
        return result

    def _attempt_envelope(
        self,
        planned: Any,
        *,
        policy_version: str,
        policy_version_hash: str,
        cycle_suffix: str = "",
    ) -> LineageEnvelope:
        factory = self.ports.lineage_factory
        decided_at = datetime.now(UTC)
        intent = factory.create_decision_intent(
            DecisionIntentDraft(
                policy_version=policy_version,
                policy_version_hash=policy_version_hash,
                decision_timestamp=decided_at,
                market_data_cutoff=decided_at - timedelta(seconds=1),
                symbol=str(planned.symbol),
                side=str(planned.side),
                target_notional=Decimal(int(planned.price) * int(planned.quantity)),
                target_notional_currency="KRW",
                limit_policy={"order_type": "limit", "price": int(planned.price)},
                expiry_policy={"kind": "day"},
                rationale=f"kiwoom-j3c:{planned.order_key}",
            )
        )
        plan_envelope = factory.create_plan_envelope(
            intent,
            ExecutionPlanDraft(
                lane_id=KIWOOM_CANONICAL_LANE_ID,
                broker="kiwoom",
                account_profile="mock",
                account_mode="mock",
                normalized_symbol=str(planned.symbol),
                quantity=Decimal(int(planned.quantity)),
                limit_price=Decimal(int(planned.price)),
                quote_currency="KRW",
                tick_rounding={"mode": "down" if planned.side == "buy" else "up"},
                session="regular",
                time_in_force="day",
                min_order_validation={"min_qty": "1"},
                risk_caps={"max_notional": "10000000"},
            ),
        )
        return factory.create_attempt_envelope(
            plan_envelope,
            OrderAttemptDraft(
                cycle_id=f"{planned.cycle_id}{cycle_suffix}",
                attempt_seq=1,
                lane_prefix=None,
                broker_client_id_target=None,
            ),
        )


__all__ = [
    "ACCOUNT_SUMMARY_FINGERPRINT_IDENTITY_REJECTED",
    "CALLER_DERIVED_IDENTITY_REJECTED",
    "JSONL_ABSENCE_NOT_EMPTY_OWNERSHIP",
    "KIWOOM_CANONICAL_LANE_ID",
    "KIWOOM_LIFECYCLE_STATUS",
    "KIWOOM_READBACK_OPERATION",
    "KIWOOM_RECOVERY_OWNER",
    "KIWOOM_RELEASE_IF_MATCHES",
    "KIWOOM_RESTART_TRIGGER",
    "KT00009_CANNOT_REPLACE_KT00007",
    "LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND",
    "ORDERING_EVENT_JOURNAL_NAME",
    "PROXIMITY_ATTRIBUTION_REJECTED",
    "ROOT_PATH_IDENTITY_REJECTED",
    "TRANSPORT_GATE_REJECTED",
    "UNKNOWN_PENDING_RECONCILE",
    "AccountWriterLease",
    "AccountWriterLeaseContended",
    "AccountWriterLeaseLost",
    "InMemoryDispatchEvidence",
    "InMemoryLineagePersistence",
    "InMemoryReservationPort",
    "InMemoryUncertaintyGate",
    "KiwoomAttributionRejected",
    "KiwoomCoordinationAdapter",
    "KiwoomCoordinationPorts",
    "KiwoomIdentityRejected",
    "KiwoomSendNotAuthorized",
    "KiwoomTransportGateRejected",
    "NativeBrokerRow",
    "OrderingEventJournal",
    "OrderingJournalUnreadable",
    "RestartDisposition",
    "WriterLease",
    "actual_kiwoom_client",
    "assert_broker_client_id_contract",
    "assert_kiwoom_transport_ready",
    "followup_precheck",
    "native_row_from_kt00007",
    "normalize_kt00007_state",
    "reject_jsonl_as_empty_ownership",
    "reject_kt00009_as_truth",
    "reject_proximity_attribution",
    "require_j2a_physical_account_id",
    "restart_disposition",
    "wall_clock_cannot_release",
]
