"""ROB-1262 J3A — broker-neutral mock/paper/demo coordination port.

One physical mock/paper/demo account is coordinated by four separable pieces: an
authoritative PostgreSQL **session** advisory lease, a durable **binary** send
reservation, lane-owned **durable evidence** ports, and an **injected** mutation
callback.  This module owns none of the broker transport those pieces protect.

NOT BROKER-ENFORCED FENCING — statement 1 of 3 (see also
:class:`PostgresAdvisoryKeysetLease` and the lane matrix in
``docs/contracts/rob-1262-coordination-port.md``).  The lease coordinates
processes *inside this repository only*.  Every lane in
:data:`LANE_FENCING_MATRIX` maps to :data:`FENCING_NOT_BROKER_ENFORCED` because
no broker on that list accepts a fencing token: an out-of-repo process, a stale
deployment, or a human at a broker console reaches the same account without ever
contending for this lease.  Reading "the lease is held, therefore the broker will
reject everyone else" is the most expensive available misreading of this module.

Three distinctions this module refuses to blur:

* **A pool return is not a backend-session termination.**  Returning or closing a
  pooled connection leaves the backend — and every advisory lock whose unlock
  could not be *proven* — alive.  :class:`LockAuthorityConnection` therefore
  requires a real ``terminate_backend_session`` that raises unless severance is
  positively demonstrated, and an authority without one is rejected before any
  lock is taken.
* **A lease release is not a claim release.**  The lease is ephemeral process
  coordination; the durable claim is the account block.  Only
  :meth:`DurableSendClaimAdapter.release_with_terminal_evidence` removes a claim.
* **Cleanup is not permitted while the outcome is unknown.**  If the durable
  evidence of what happened to a dispatch cannot be written, this process keeps
  the writer authority and records an auditable
  :class:`UnreleasedAuthorityHold` rather than handing the account to a
  successor that would mutate it blind.

Deliberate absences, each covered by a static test:

* no clock — no TTL, no heartbeat, no local-clock expiry, no automatic takeover;
* no file lock — a DB authority failure is fail-closed, never a file fallback
  (the existing KIS PID file stays a later J3B *diagnostic* seam, not authority);
* no broker transport, socket, signing implementation, or credential-value load;
* no lifecycle/hold/retry/queue state — ``review.order_send_intents`` stays a
  binary reservation, and lifecycle evidence belongs to lane-native services.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.mock_integration.lineage import (
    LineageEnvelope,
    LineagePersistencePort,
    LineagePersistenceUnavailable,
    LineageReasonCode,
    MockLineageFactory,
    require_lineage_persistence_port,
)
from app.services.mock_lane_registry import (
    CANONICAL_LANE_IDS,
    LaneGuardError,
    LaneRegistryEntry,
    RegistrySource,
    assert_entry_execution_ready,
    assert_lineage_registry_binding,
)
from app.services.order_send_intent_service import DuplicateOrderIntent

# --------------------------------------------------------------------------
# Reason codes
# --------------------------------------------------------------------------


class CoordinationReasonCode(StrEnum):
    """The complete J3A reason-code vocabulary; no free-form alternatives.

    ``LINEAGE_PERSISTENCE_UNAVAILABLE`` reuses J2B's exact literal instead of
    redefining it.  J2B's :class:`~app.services.mock_integration.lineage.LineageReasonCode`
    is never modified or overloaded from here.
    """

    LOCK_AUTHORITY_UNAVAILABLE = "lock_authority_unavailable"
    LEASE_CONTENDED = "lease_contended"
    LEASE_LOST = "lease_lost"
    LEASE_EVENT_LOOP_MISMATCH = "lease_event_loop_mismatch"
    DURABLE_CLAIM_CONFLICT = "durable_claim_conflict"
    LINEAGE_PERSISTENCE_UNAVAILABLE = (
        LineageReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE.value
    )
    TERMINAL_EVIDENCE_REQUIRED = "terminal_evidence_required"
    CLAIM_FOLLOWUP_NOT_AUTHORIZED = "claim_followup_not_authorized"


COORDINATION_REASON_CODES: Final[frozenset[str]] = frozenset(
    reason.value for reason in CoordinationReasonCode
)


class CoordinationError(RuntimeError):
    """Fail-closed coordination rejection carrying one stable machine code.

    The message never contains a physical account identifier; only the derived
    opaque scope or the lane id may accompany it.
    """

    def __init__(
        self,
        reason_code: CoordinationReasonCode,
        *,
        lane_id: str | None = None,
        hold_id: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.lane_id = lane_id
        # Only the opaque hold id is ever exposed: never the account identity,
        # the advisory key, or the backend PID.
        self.hold_id = hold_id
        parts = [reason_code.value]
        if lane_id is not None:
            parts.append(f"lane={lane_id}")
        if hold_id is not None:
            parts.append(f"hold={hold_id}")
        super().__init__(": ".join(parts))


class BackendSessionTerminationUnproven(RuntimeError):
    """A backend session could not be *proven* to have ended.

    This is not a reason code and never becomes one: it is the internal signal
    that a lock may still be held by a surviving backend, which the caller turns
    into an :class:`UnreleasedAuthorityHold`.
    """


# --------------------------------------------------------------------------
# Not-broker-enforced fencing (statement 2 of 3 lives on the lease class)
# --------------------------------------------------------------------------

FENCING_NOT_BROKER_ENFORCED: Final[str] = "not_broker_enforced"

NOT_BROKER_ENFORCED_FENCING_STATEMENT: Final[str] = (
    "The J3A physical-account lease is process coordination only. It is NOT "
    "broker-enforced fencing: no lane broker rejects a request because another "
    "process holds this lease."
)

# One row per canonical J2A lane. There is intentionally no per-lane exception:
# a lane that later gains real broker-side fencing must add its own evidence
# rather than silently reinterpreting this matrix.
LANE_FENCING_MATRIX: Final[Mapping[str, str]] = MappingProxyType(
    dict.fromkeys(CANONICAL_LANE_IDS, FENCING_NOT_BROKER_ENFORCED)
)

# There is no TTL, no heartbeat, and no janitor. A durable claim is removed only
# by an evidence-gated ``release_if_matches``; nothing in this process deletes a
# claim because time passed.
AUTOMATIC_CLAIM_RELEASE_AVAILABLE: Final[bool] = False
LEASE_TTL_SECONDS: Final[None] = None


# --------------------------------------------------------------------------
# B-1 — opaque physical-account scope derived from canonical J2A identity
# --------------------------------------------------------------------------

_PHYSICAL_ACCOUNT_SCOPE_V1_DOMAIN: Final[bytes] = b"mock-physical-account-v1\0"
_PHYSICAL_ACCOUNT_SCOPE_V1_PREFIX: Final[str] = "mockpa:v1:"


@dataclass(frozen=True, slots=True)
class PhysicalAccountScope:
    """Opaque, non-reversible coordination identity for one physical account.

    Both fields are SHA-256 derivatives.  The raw ``physical_account_id`` is
    never stored here, so this record is safe to repr, log, and serialize.
    """

    claim_account_scope: str
    advisory_key: int


def _derive_physical_account_scope(physical_account_id: str) -> PhysicalAccountScope:
    """Apply the pinned derivation to already-canonical J2A identity bytes.

    No local alias, case, whitespace, broker, market, or profile normalization is
    applied: the caller must already hold canonical J2A identity bytes.
    """

    digest = hashlib.sha256(
        _PHYSICAL_ACCOUNT_SCOPE_V1_DOMAIN + physical_account_id.encode("utf-8")
    ).digest()
    return PhysicalAccountScope(
        claim_account_scope=_PHYSICAL_ACCOUNT_SCOPE_V1_PREFIX + digest.hex(),
        advisory_key=int.from_bytes(digest[:8], byteorder="big", signed=True),
    )


def physical_account_scope_for_entry(entry: LaneRegistryEntry) -> PhysicalAccountScope:
    """Derive the coordination scope from one identity-known J2A registry entry.

    A caller-supplied scope string is never accepted: the only input is the
    canonical ``physical_account_id`` of a validated registry entry.  Unknown or
    blank identity fails here, before any lease, persistence, reservation, or
    callback work is attempted.
    """

    physical_account_id = entry.physical_account_id
    if (
        not isinstance(physical_account_id, str)
        or not physical_account_id.strip()
        or not isinstance(entry.identity_status, str)
        or entry.identity_status == "UNKNOWN"
        or not entry.identity_status.strip()
    ):
        raise LaneGuardError("physical_account_identity_unknown", lane_id=entry.lane_id)
    return _derive_physical_account_scope(physical_account_id)


# --------------------------------------------------------------------------
# B-5 / B-7 — PostgreSQL session advisory lease over an ordered keyset
# --------------------------------------------------------------------------

_SESSION_IDENTITY_SQL: Final[str] = (
    "SELECT pg_backend_pid() AS backend_pid, "
    "(SELECT oid FROM pg_database WHERE datname = current_database())::bigint "
    "AS database_oid"
)
_TRY_ADVISORY_LOCK_SQL: Final[str] = (
    "SELECT pg_try_advisory_lock(CAST(:key AS bigint)) AS acquired"
)
_ADVISORY_UNLOCK_SQL: Final[str] = (
    "SELECT pg_advisory_unlock(CAST(:key AS bigint)) AS released"
)
_OWNED_ADVISORY_ROWS_SQL: Final[str] = (
    "SELECT locktype, mode, granted, database::bigint AS database_oid, pid, "
    "objsubid, classid::bigint AS classid, objid::bigint AS objid "
    "FROM pg_locks WHERE locktype = 'advisory' AND pid = :pid"
)
_TERMINATE_BACKEND_SQL: Final[str] = (
    "SELECT pg_terminate_backend(CAST(:pid AS integer)) AS terminated"
)
_BACKEND_ALIVE_SQL: Final[str] = (
    "SELECT count(*) AS alive FROM pg_stat_activity WHERE pid = CAST(:pid AS integer)"
)

_ADVISORY_LOCKTYPE: Final[str] = "advisory"
_ADVISORY_EXCLUSIVE_MODE: Final[str] = "ExclusiveLock"
# A bigint advisory key occupies one (classid, objid) pair with objsubid = 1.
# The two-int32 form uses objsubid = 2 and is a different lock space.
_BIGINT_ADVISORY_OBJSUBID: Final[int] = 1


@dataclass(frozen=True, slots=True)
class BackendTerminationReceipt:
    """Positive, immutable proof that one exact backend session was ended.

    A receipt is only meaningful when it is bound to the *exact* PID and owner
    token of the acquisition it claims to have terminated.  A ``close()``, a
    pool return, or an ambiguous driver error produces no receipt at all — those
    are silence, not proof, and silence is never accepted as a release.
    """

    backend_pid: int
    owner_token: str
    terminated: bool


@runtime_checkable
class LockAuthorityConnection(Protocol):
    """The dedicated session that *is* the lock authority.

    Deliberately narrow: a pooled ``AsyncSession``, a transparently reconnecting
    wrapper, or a file handle cannot satisfy the ownership attestation below.

    ``terminate_backend_session`` is part of the contract, not an optimisation.
    When an unlock cannot be *proven*, the only remaining way to guarantee the
    advisory lock is gone is to end the backend session that holds it.  Closing
    or returning a pooled connection does not do that.  An implementation must
    return a :class:`BackendTerminationReceipt` bound to the exact ``expected_pid``
    and ``owner_token``, or **raise** — silently reporting success here would hand
    a successor an account that is still locked by a live backend.
    """

    async def execute(self, statement: Any, parameters: Any = None, /) -> Any: ...

    async def commit(self) -> None: ...

    async def close(self) -> None: ...

    def can_prove_backend_session_termination(self) -> bool: ...

    async def terminate_backend_session(
        self, *, expected_pid: int, owner_token: str
    ) -> BackendTerminationReceipt: ...


type LockAuthorityConnectionFactory = Callable[[], Awaitable[LockAuthorityConnection]]


class SqlAlchemyLockAuthority:
    """Adapts one **dedicated** SQLAlchemy ``AsyncConnection`` to the authority.

    The constructor accepts nothing else on purpose: an ``AsyncSession``, a
    sessionmaker, or any pooled facade is rejected rather than quietly treated as
    a dedicated session.

    ``terminate_backend_session`` proves the result from an **independent
    observer session**: it runs ``pg_terminate_backend`` against the exact PID and
    then confirms that PID is absent from ``pg_stat_activity``.  Self-termination
    from the dying connection is deliberately not used, because the driver error
    it produces is *ambiguous* — it looks identical to a network blip — and an
    ambiguous error is not a receipt.  Without an observer factory this adapter
    honestly reports that it cannot prove termination.
    """

    __slots__ = ("_connection", "_observer_factory")

    def __init__(
        self,
        connection: AsyncConnection,
        *,
        observer_factory: Callable[[], Awaitable[AsyncConnection]] | None = None,
    ) -> None:
        if not isinstance(connection, AsyncConnection):
            raise CoordinationError(CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE)
        self._connection = connection
        self._observer_factory = observer_factory

    async def execute(self, statement: Any, parameters: Any = None, /) -> Any:
        return await self._connection.execute(statement, parameters)

    async def commit(self) -> None:
        await self._connection.commit()

    async def close(self) -> None:
        await self._connection.close()

    async def terminate_backend_session(
        self, *, expected_pid: int, owner_token: str
    ) -> BackendTerminationReceipt:
        if self._observer_factory is None:
            raise BackendSessionTerminationUnproven(
                "no independent observer session is available to prove termination"
            )
        observer = await self._observer_factory()
        try:
            # The boolean is informational: it is false both when the PID never
            # existed and when it had already gone. Absence from
            # ``pg_stat_activity`` is the decisive proof, and it is the only
            # thing this treats as one.
            await observer.execute(text(_TERMINATE_BACKEND_SQL), {"pid": expected_pid})
            alive = await observer.execute(
                text(_BACKEND_ALIVE_SQL), {"pid": expected_pid}
            )
            if int(alive.scalar_one()) != 0:
                raise BackendSessionTerminationUnproven(
                    "the backend is still present in pg_stat_activity"
                )
            # Absence is proven *here*. The receipt is built before any cleanup
            # so that a cancellation during a close cannot erase a proof we
            # already hold and manufacture a false active hold.
            receipt = BackendTerminationReceipt(
                backend_pid=expected_pid, owner_token=owner_token, terminated=True
            )
        finally:
            # Only the *observer* is disposable on every path. Closing the owner
            # connection here would return a still-locked backend to the pool on
            # exactly the paths where termination was NOT proven.
            await _close_quietly(observer)
        # Proven gone: only now may the owner connection be released, and a
        # failure doing so is reported without invalidating the receipt.
        await _close_quietly(self._connection)
        return receipt

    def can_prove_backend_session_termination(self) -> bool:
        """Without an independent observer, termination can never be proven."""

        return self._observer_factory is not None


async def _close_quietly(closeable: Any) -> None:
    """Close something that is provably not holding a lock we still need."""

    try:
        await closeable.close()
    except BaseException:
        # Including a cancellation: a cleanup failure is never allowed to
        # invalidate a proof that was already established.
        pass


def supports_backend_session_termination(connection: object) -> bool:
    """True only when the authority *asserts* it can end its backend session.

    The presence of a ``terminate_backend_session`` method is not capability:
    an adapter with no independent observer would sail through a callable check,
    take the locks, and only then discover it can never prove termination.  So
    the authority must also answer ``can_prove_backend_session_termination()``
    affirmatively, and it is asked before a single statement is issued.
    """

    if not callable(getattr(connection, "terminate_backend_session", None)):
        return False
    prover = getattr(connection, "can_prove_backend_session_termination", None)
    if not callable(prover):
        return False
    try:
        return bool(prover())
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class UnreleasedAuthorityHold:
    """A **capability-free** record of an authority that is not proven released.

    Deliberately carries no live connection, no grant, no backend PID, no owner
    token, and nothing callable.  Seeing that an account is stuck is a different
    right from being able to unstick it, and only the second one is dangerous:
    a PID plus an owner token is enough to terminate the very backend whose lock
    is the safety property.  The real connection lives in the module-private
    ownership map below, reachable only by the coordination-owned release path.
    """

    hold_id: str
    key_count: int
    reason_code: CoordinationReasonCode
    termination_proven: bool
    durable_evidence_written: bool
    # Whether a retry is *actually reachable*, recomputed on every read — not a
    # guess from the owner's type. A rollback hold has no owning lease, and a
    # coordination-sealed lease is never unsealed after a failed release, so both
    # are permanent until the process dies. Claiming otherwise would be the same
    # kind of false report B30 forbade in the other direction.
    recoverable_in_process: bool


@dataclass(frozen=True, slots=True)
class _RetainedAuthority:
    """A strong reference to the *actual* connection an unproven hold describes.

    Recording metadata alone would let a garbage collection or a pool return
    quietly drop the very authority the metadata claims is still held.  This
    keeps the real dedicated connection, together with the exact grant identity,
    reachable for as long as the hold stands.

    Module-private on purpose: this is the capability, and it is never returned
    from a public function.  It is a lifetime guard only — no TTL, no janitor,
    no retry, no takeover, no scheduler, no reason code of its own.
    """

    hold_id: str
    connection: LockAuthorityConnection
    grant: AdvisoryLeaseGrant
    owner: object


# History is immutable evidence: every hold ever recorded, resolved or not.
# The active views are the honest answer to "what is unresolved right now".
# Reporting a resolved hold as unresolved is as much a defect as the reverse.
_AUTHORITY_HOLD_HISTORY: list[UnreleasedAuthorityHold] = []
_ACTIVE_AUTHORITY_HOLDS: dict[str, UnreleasedAuthorityHold] = {}
_RETAINED_AUTHORITIES: dict[str, _RetainedAuthority] = {}

_HOLD_ID_ALLOCATION_ATTEMPTS: Final[int] = 8
_QUARANTINE_SEQUENCE = 0


def _hold_view(hold: UnreleasedAuthorityHold) -> UnreleasedAuthorityHold:
    """Recompute the reachability flag against the world as it is now.

    A retry is reachable only through an owning lease that is still releasable.
    A rollback hold has no lease at all, and a coordination-sealed lease stays
    sealed forever once its release failed, so neither can be retried in this
    process — and there is deliberately no recovery API to add one.
    """

    retained = _RETAINED_AUTHORITIES.get(hold.hold_id)
    owner = retained.owner if retained is not None else None
    # The two reasons a release is refused are a permanent coordination seal
    # (public paths) and missing durable evidence (every path, owner included).
    # The flag must be the negation of both, or it reports a retry that cannot
    # happen. Deliberately *not* consulted: whether private machinery still
    # exists — a sealed lease has no supported retry, and saying otherwise would
    # imply a recovery API that this epoch does not have.
    recoverable = (
        isinstance(owner, PostgresAdvisoryKeysetLease)
        and not owner.coordination_sealed
        and not owner.released
        and hold.durable_evidence_written
    )
    if recoverable == hold.recoverable_in_process:
        return hold
    return replace(hold, recoverable_in_process=recoverable)


def authority_hold_history() -> tuple[UnreleasedAuthorityHold, ...]:
    """Immutable evidence: every hold ever recorded, in order, including resolved."""

    return tuple(_AUTHORITY_HOLD_HISTORY)


def unreleased_authority_holds() -> tuple[UnreleasedAuthorityHold, ...]:
    """The authorities that are unresolved **right now**, in recording order."""

    return tuple(_hold_view(hold) for hold in _ACTIVE_AUTHORITY_HOLDS.values())


def _retained_authorities() -> Mapping[str, _RetainedAuthority]:
    """Module-private: the live connections behind every active hold."""

    return MappingProxyType(dict(_RETAINED_AUTHORITIES))


def _authority_lockout(
    connection: LockAuthorityConnection, *, owner: object | None = None
) -> str | None:
    """The hold id making this *authority* unreleasable, regardless of wrapper.

    Non-releasability belongs to the authority, not to whichever
    :class:`PostgresAdvisoryKeysetLease` object happens to wrap it. Otherwise a
    fresh wrapper around the same connection and grant would carry a blank
    ``_hold`` straight past the durable-evidence gate.
    """

    for hold_id, retained in _RETAINED_AUTHORITIES.items():
        if retained.connection is not connection:
            continue
        hold = _ACTIVE_AUTHORITY_HOLDS.get(hold_id)
        if hold is not None and not hold.durable_evidence_written:
            # Sticky: nobody may release this, the owning lease included. This
            # clause and the coordination seal are **not** substitutes — the seal
            # stops the public path, and this stops every path, owner or not.
            return hold_id
        if retained.owner is not owner:
            # Whatever the evidence flag says, a foreign wrapper never releases
            # somebody else's retained authority.
            return hold_id
    for hold_id, held in _HELD_COORDINATION.items():
        if held.lease.owns_connection(connection) and held.lease is not owner:
            return hold_id
    return None


def _unregister_owned_coordination(owner: object) -> None:
    """Drop the held-coordination entry owned by this lease, if any.

    Reached from every proven-release path, including the ones that go on to
    re-raise: an authority that is provably released must never keep being
    reported as held just because the triggering call failed afterwards.
    """

    for hold_id, held in list(_HELD_COORDINATION.items()):
        if held.lease is owner:
            _COORDINATION_RELEASE_CAPABILITIES.pop(hold_id, None)
            del _HELD_COORDINATION[hold_id]


def _hold_id_in_use(hold_id: str) -> bool:
    return (
        hold_id in _ACTIVE_AUTHORITY_HOLDS
        or hold_id in _RETAINED_AUTHORITIES
        or hold_id in _HELD_COORDINATION
        or hold_id in _COORDINATION_RELEASE_CAPABILITIES
    )


def _allocate_hold_id() -> str:
    """Allocate an id unused across **every** active map, or fail closed.

    One namespace, one owner. Overwriting a colliding id would let a later hold
    silently adopt an earlier one's entry — and then delete it during its own
    recovery, taking a foreign authority with it.
    """

    for _ in range(_HOLD_ID_ALLOCATION_ATTEMPTS):
        candidate = f"hold:{secrets.token_hex(8)}"
        if not _hold_id_in_use(candidate):
            return candidate
    # Exhaustion must never leave a newly unsafe authority unreachable, so fall
    # back to a quarantine id derived from a monotonic counter rather than
    # raising. It cannot collide (the random ids never take this shape) and it
    # never overwrites an existing owner, so the victim of the collision storm
    # is untouched while the intruder still gets retained.
    global _QUARANTINE_SEQUENCE
    while True:
        _QUARANTINE_SEQUENCE += 1
        candidate = f"hold:quarantine:{_QUARANTINE_SEQUENCE:012d}"
        if not _hold_id_in_use(candidate):
            return candidate


def _resolve_authority_hold(
    hold: UnreleasedAuthorityHold | None,
    *,
    owner: object,
    connection: LockAuthorityConnection,
) -> None:
    """Compare-and-delete exactly this owner's entries, both of them, in one step.

    Only ever called after a proven full unlock or a positive termination
    receipt.  The stored owner identity must match, so a stale or foreign hold
    id clears nothing, and one lease's recovery can never delete another's
    entry.  History is untouched.
    """

    if hold is None:
        return
    retained = _RETAINED_AUTHORITIES.get(hold.hold_id)
    if retained is not None and (
        retained.owner is not owner or retained.connection is not connection
    ):
        return
    if _ACTIVE_AUTHORITY_HOLDS.get(hold.hold_id) is hold:
        del _ACTIVE_AUTHORITY_HOLDS[hold.hold_id]
    if retained is not None:
        _RETAINED_AUTHORITIES.pop(hold.hold_id, None)
    _unregister_owned_coordination(owner)


def split_advisory_key(key: int) -> tuple[int, int]:
    """Reconstruct the unsigned ``(classid, objid)`` halves of a signed key.

    ``pg_locks`` stores the two 32-bit halves as unsigned oids, so a negative
    signed bigint key never equals ``objid``.  Comparing the signed key directly
    against ``objid`` is the classic silent-ownership bug this exists to prevent.
    """

    unsigned_key = key & ((1 << 64) - 1)
    return unsigned_key >> 32, unsigned_key & 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class AdvisoryLockRow:
    """One observed ``pg_locks`` row, projected to the fields that matter."""

    locktype: str
    mode: str
    granted: bool
    database_oid: int
    pid: int
    objsubid: int
    classid: int
    objid: int


def row_proves_ownership(
    row: AdvisoryLockRow, *, key: int, backend_pid: int, database_oid: int
) -> bool:
    """Every predicate must hold; a partial match is not ownership."""

    classid, objid = split_advisory_key(key)
    return (
        row.locktype == _ADVISORY_LOCKTYPE
        and row.mode == _ADVISORY_EXCLUSIVE_MODE
        and row.granted is True
        and row.database_oid == database_oid
        and row.pid == backend_pid
        and row.objsubid == _BIGINT_ADVISORY_OBJSUBID
        and row.classid == classid
        and row.objid == objid
    )


def _rows_hold_any_key(
    rows: Sequence[AdvisoryLockRow],
    *,
    keys: Sequence[int],
    backend_pid: int,
    database_oid: int,
) -> bool:
    """True if this backend already holds *any* of these keys."""

    return any(
        row_proves_ownership(
            row, key=key, backend_pid=backend_pid, database_oid=database_oid
        )
        for key in keys
        for row in rows
    )


def _rows_prove_full_keyset(
    rows: Sequence[AdvisoryLockRow],
    *,
    keys: Sequence[int],
    backend_pid: int,
    database_oid: int,
) -> bool:
    return all(
        any(
            row_proves_ownership(
                row, key=key, backend_pid=backend_pid, database_oid=database_oid
            )
            for row in rows
        )
        for key in keys
    )


def ordered_advisory_keyset(keys: Sequence[int]) -> tuple[int, ...]:
    """De-duplicate, then sort by a deterministic global numeric order.

    A stable acquisition order across every caller is what prevents two
    multi-key holders from deadlocking on each other's partial keysets.
    """

    return tuple(sorted(set(keys)))


@dataclass(frozen=True, slots=True)
class AdvisoryLeaseGrant:
    """Immutable proof of one successful ordered-keyset acquisition.

    This carries the *complete* identity of the acquisition — full keyset,
    backend PID, database oid, a per-acquisition owner token, and the event loop
    the session is bound to.  Both :meth:`PostgresAdvisoryKeysetLease.assert_owned`
    and :meth:`PostgresAdvisoryKeysetLease.release` require the caller to hand
    this exact grant back, so a stale grant or a copied token cannot drive
    either one.
    """

    keys: tuple[int, ...]
    backend_pid: int
    database_oid: int
    connection_token: str
    event_loop: asyncio.AbstractEventLoop


async def _read_session_identity(
    connection: LockAuthorityConnection,
) -> tuple[int, int]:
    result = await connection.execute(text(_SESSION_IDENTITY_SQL))
    row = result.mappings().one()
    return int(row["backend_pid"]), int(row["database_oid"])


async def _read_owned_advisory_rows(
    connection: LockAuthorityConnection, backend_pid: int
) -> tuple[AdvisoryLockRow, ...]:
    result = await connection.execute(
        text(_OWNED_ADVISORY_ROWS_SQL), {"pid": backend_pid}
    )
    return tuple(
        AdvisoryLockRow(
            locktype=str(row["locktype"]),
            mode=str(row["mode"]),
            granted=bool(row["granted"]),
            database_oid=int(row["database_oid"]),
            pid=int(row["pid"]),
            objsubid=int(row["objsubid"]),
            classid=int(row["classid"]),
            objid=int(row["objid"]),
        )
        for row in result.mappings().all()
    )


async def _attest_full_ownership(
    connection: LockAuthorityConnection,
    *,
    keys: Sequence[int],
    backend_pid: int,
    database_oid: int,
) -> bool:
    """Re-read the session identity and prove every exact ``pg_locks`` row."""

    attested_pid, attested_database_oid = await _read_session_identity(connection)
    if attested_pid != backend_pid or attested_database_oid != database_oid:
        return False
    rows = await _read_owned_advisory_rows(connection, backend_pid)
    return _rows_prove_full_keyset(
        rows, keys=keys, backend_pid=backend_pid, database_oid=database_oid
    )


async def _unlock_proven(connection: LockAuthorityConnection, key: int) -> bool:
    """Unlock one key and return what PostgreSQL actually reported.

    ``pg_advisory_unlock`` returns false when this session does not hold the
    key.  Recording an unlock without checking that boolean is how a release
    starts lying about keys it never let go of.
    """

    result = await connection.execute(text(_ADVISORY_UNLOCK_SQL), {"key": key})
    return bool(result.scalar_one())


async def _terminate_authority(
    connection: LockAuthorityConnection, grant: AdvisoryLeaseGrant
) -> BackendTerminationReceipt | None:
    """End the backend session and return a receipt only when it is *proven*.

    A pool return is never accepted as termination, an ambiguous driver error is
    never converted into success, and a receipt that does not match this exact
    acquisition's PID and owner token is discarded.  The caller records an
    :class:`UnreleasedAuthorityHold` for every one of those cases.
    """

    try:
        receipt = await connection.terminate_backend_session(
            expected_pid=grant.backend_pid, owner_token=grant.connection_token
        )
    except BaseException:
        # A cancellation here is not "no lock was taken" — it is "the outcome is
        # unknown", which is the same fail-closed answer as any other failure.
        return None
    if (
        not isinstance(receipt, BackendTerminationReceipt)
        or receipt.terminated is not True
        or receipt.backend_pid != grant.backend_pid
        or receipt.owner_token != grant.connection_token
    ):
        return None
    return receipt


def _record_unreleased_authority(
    grant: AdvisoryLeaseGrant,
    *,
    owner: object,
    reason_code: CoordinationReasonCode,
    termination_proven: bool,
    durable_evidence_written: bool,
    connection: LockAuthorityConnection | None = None,
    hold_id: str | None = None,
) -> UnreleasedAuthorityHold:
    """Record a hold, never overwriting one that belongs to somebody else."""

    if hold_id is None:
        hold_id = _allocate_hold_id()
    else:
        retained = _RETAINED_AUTHORITIES.get(hold_id)
        held = _HELD_COORDINATION.get(hold_id)
        foreign = (
            (retained is not None and retained.owner is not owner)
            or (held is not None and held.lease is not owner)
            or (retained is None and hold_id in _ACTIVE_AUTHORITY_HOLDS)
        )
        if foreign:
            # A supplied id that belongs to another owner is refused outright;
            # adopting it would let this hold delete that one during recovery.
            raise CoordinationError(
                CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE, hold_id=hold_id
            )
    hold = UnreleasedAuthorityHold(
        hold_id=hold_id,
        key_count=len(grant.keys),
        reason_code=reason_code,
        termination_proven=termination_proven,
        durable_evidence_written=durable_evidence_written,
        # Recorded conservatively; :func:`_hold_view` recomputes it on read.
        recoverable_in_process=False,
    )
    _AUTHORITY_HOLD_HISTORY.append(hold)
    _ACTIVE_AUTHORITY_HOLDS[hold.hold_id] = hold
    if connection is not None:
        _RETAINED_AUTHORITIES[hold.hold_id] = _RetainedAuthority(
            hold_id=hold.hold_id, connection=connection, grant=grant, owner=owner
        )
    return hold


class PostgresAdvisoryKeysetLease:
    """A dedicated PostgreSQL session holding an ordered advisory keyset.

    NOT BROKER-ENFORCED FENCING — statement 2 of 3.  Holding this lease proves
    that no *other holder of this same lease* is mutating the account.  It proves
    nothing about the broker: the broker never sees the lease, so a process that
    does not take it (an old deployment, an operator console, another repository)
    is unaffected.  See :data:`NOT_BROKER_ENFORCED_FENCING_STATEMENT` and the
    lane matrix in ``docs/contracts/rob-1262-coordination-port.md``.

    Release is exactly two things: an **attested** owner/key-matched
    ``pg_advisory_unlock``, or a **proven** termination of this backend session.
    There is no TTL and no takeover, and a pool return is neither of those.

    A lease whose post-send durable evidence never landed is not releasable at
    all: ``_retain_authority`` marks it, and every subsequent ``release`` fails
    closed with ``lineage_persistence_unavailable``.  Public introspection yields
    capability-free snapshots only — no lease, no grant, no connection — so there
    is no public route to a release in the first place.

    Release is also cancellation-safe.  The whole re-attest → reverse unlock →
    close sequence runs as a retained task, and the lease is marked released only
    after that sequence proves an allowed outcome.  Whether an interrupted or
    failed release can be retried depends on who owns it: an unsealed standalone
    owner still holding its exact private lease/grant can retry, while a
    coordination-sealed lease and a partial-acquisition rollback have no
    in-process recovery API in this epoch.  Stale or foreign grants are always
    rejected.
    """

    __slots__ = (
        "_connection",
        "_coordination_hold_id",
        "_grant",
        "_hold",
        "_release_capability",
        "_release_task",
        "_released",
        "_termination_receipt",
        "_unlocked_keys",
    )

    def __init__(
        self,
        *,
        connection: LockAuthorityConnection,
        grant: AdvisoryLeaseGrant,
    ) -> None:
        self._connection = connection
        self._grant = grant
        self._released = False
        self._unlocked_keys: tuple[int, ...] = ()
        self._release_task: asyncio.Task[None] | None = None
        self._hold: UnreleasedAuthorityHold | None = None
        self._termination_receipt: BackendTerminationReceipt | None = None
        # While sealed, only the coordination flow that sealed it may release.
        self._release_capability: object | None = None
        self._coordination_hold_id: str | None = None
        lockout = _authority_lockout(connection)
        if lockout is not None:
            # A second wrapper around an authority that is already held would
            # arrive with a blank _hold and sail past the durable-evidence gate.
            raise CoordinationError(
                CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
                hold_id=lockout,
            )

    def owns_connection(self, connection: LockAuthorityConnection) -> bool:
        return self._connection is connection

    @property
    def grant(self) -> AdvisoryLeaseGrant:
        return self._grant

    @property
    def released(self) -> bool:
        return self._released

    @property
    def unlocked_keys(self) -> tuple[int, ...]:
        """Keys PostgreSQL *confirmed* it released, in call order, once each."""

        return self._unlocked_keys

    @property
    def unreleased_authority_hold(self) -> UnreleasedAuthorityHold | None:
        """Set when this lease could not be proven released."""

        return None if self._hold is None else _hold_view(self._hold)

    @property
    def termination_receipt(self) -> BackendTerminationReceipt | None:
        """The positive receipt, when release fell back to a proven termination."""

        return self._termination_receipt

    def _require_grant(self, expected_grant: AdvisoryLeaseGrant) -> None:
        if asyncio.get_running_loop() is not self._grant.event_loop:
            raise CoordinationError(CoordinationReasonCode.LEASE_EVENT_LOOP_MISMATCH)
        if (
            expected_grant is not self._grant
            or expected_grant.connection_token != self._grant.connection_token
            or expected_grant.event_loop is not self._grant.event_loop
            or expected_grant.keys != self._grant.keys
            or expected_grant.backend_pid != self._grant.backend_pid
            or expected_grant.database_oid != self._grant.database_oid
        ):
            raise CoordinationError(CoordinationReasonCode.LEASE_LOST)

    async def assert_owned(self, expected_grant: AdvisoryLeaseGrant) -> None:
        """Re-prove ownership immediately before every mutation.

        There is no heartbeat, so this is a *use-time* check: an idle lease is
        never assumed to still be held.  A transparently reconnected connection
        lands on a new backend PID and therefore owns nothing.
        """

        self._require_grant(expected_grant)
        if self._released:
            raise CoordinationError(CoordinationReasonCode.LEASE_LOST)
        try:
            attested = await _attest_full_ownership(
                self._connection,
                keys=self._grant.keys,
                backend_pid=self._grant.backend_pid,
                database_oid=self._grant.database_oid,
            )
        except Exception as exc:
            raise CoordinationError(
                CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
            ) from exc
        if not attested:
            raise CoordinationError(CoordinationReasonCode.LEASE_LOST)

    def _retain_authority(
        self,
        *,
        reason_code: CoordinationReasonCode,
        durable_evidence_written: bool,
        hold_id: str | None = None,
        capability: object | None = None,
    ) -> UnreleasedAuthorityHold:
        """Deliberately keep the writer authority and record why.

        Used when the durable evidence of a dispatch could not be written: a
        successor must not receive this account while nobody knows whether an
        order went out.  The hold is explicit and auditable, and is never
        resolved by a timer.
        """

        if self._release_capability is not None and (
            capability is None or capability is not self._release_capability
        ):
            # Only the coordination flow that sealed this lease may say anything
            # about its evidence state.
            raise CoordinationError(
                CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
                hold_id=self._coordination_hold_id,
            )
        existing = self._hold
        if existing is not None and not existing.durable_evidence_written:
            # Sticky and monotonic: a missing durable receipt cannot be talked
            # away in-process, and there is no signed recovery API to grant one.
            # Only process termination ends this, and the durable claim outlives
            # that anyway.
            return existing
        hold = _record_unreleased_authority(
            self._grant,
            owner=self,
            reason_code=reason_code,
            termination_proven=False,
            durable_evidence_written=durable_evidence_written,
            connection=self._connection,
            hold_id=hold_id,
        )
        self._hold = hold
        return hold

    @property
    def coordination_sealed(self) -> bool:
        """True while a coordination flow owns this lease's release rights."""

        return self._release_capability is not None

    def _seal_for_coordination(self, *, hold_id: str, capability: object) -> None:
        self._release_capability = capability
        self._coordination_hold_id = hold_id

    async def release(self, expected_grant: AdvisoryLeaseGrant) -> None:
        """The generic, public release. Refused whenever coordination owns this.

        A normal release is *attested unlock, then close*.  If ownership cannot
        be re-attested, or PostgreSQL reports ``false`` for any unlock, the lock
        state is unknown — so this ends the backend session instead of quietly
        closing a pooled connection and calling that a release, and if even that
        cannot be proven it records an :class:`UnreleasedAuthorityHold`.

        A lease that a coordination flow has sealed is off limits here for its
        **entire** active lifetime — while the callback runs, while either
        durable write is in flight, during a cancellation-retained wait, and
        while an acknowledgement anomaly is being recorded.  Holding the exact
        grant does not change that: seeing that an account is stuck and being
        able to unstick it are different rights, and the window in which a
        release would be most damaging is precisely the window in which nothing
        is yet known.
        """

        self._require_grant(expected_grant)
        if self._release_capability is not None:
            raise CoordinationError(
                CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
                hold_id=self._coordination_hold_id,
            )
        await self._release_guarded(expected_grant)

    async def _release_with_capability(
        self, expected_grant: AdvisoryLeaseGrant, capability: object
    ) -> None:
        """The coordination-owned release; reached only after the AND gate."""

        self._require_grant(expected_grant)
        if capability is None or capability is not self._release_capability:
            raise CoordinationError(
                CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
                hold_id=self._coordination_hold_id,
            )
        await self._release_guarded(expected_grant)
        # Unsealed only once a proven outcome was reached.
        self._release_capability = None

    async def _release_guarded(self, expected_grant: AdvisoryLeaseGrant) -> None:
        # The post-send AND gate never closing means nobody knows whether an
        # order went out. Surrendering the advisory lock then would let an old or
        # non-claim-aware writer mutate the account concurrently, and holding the
        # exact grant does not make that safe — so the check lives on the
        # *authority*, not on this wrapper. Public introspection may still see a
        # stuck lease; no wrapper of it may release it. There is deliberately no
        # recovery API in this epoch: process termination ends the ephemeral
        # session, while the durable binary claim survives and keeps blocking a
        # successor's repost.
        lockout = _authority_lockout(self._connection, owner=self)
        if lockout is not None:
            raise CoordinationError(
                CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
                hold_id=lockout,
            )
        if self._released:
            return

        if self._release_task is None:
            self._release_task = asyncio.ensure_future(self._release_impl())
        task = self._release_task
        cancellation = await _await_retained_task(task)
        error = _task_error(task)
        if error is not None:
            # An unsealed owner may retry with this exact grant; a sealed one
            # cannot, and stale or foreign grants never can.
            self._release_task = None
            raise error
        self._released = True
        if cancellation is not None:
            raise cancellation

    async def _release_impl(self) -> None:
        """Attest, reverse-unlock with proof, then prove the rows are gone.

        Every failure mode here — including a ``CancelledError`` arriving inside
        an attestation, an unlock, or a termination — means the authority's fate
        is *unknown*, never that it was released. Partial unlock progress is kept
        so the remaining authority is described honestly.
        """

        try:
            attested = await _attest_full_ownership(
                self._connection,
                keys=self._grant.keys,
                backend_pid=self._grant.backend_pid,
                database_oid=self._grant.database_oid,
            )
        except BaseException as exc:
            await self._terminate_or_hold(CoordinationReasonCode.LEASE_LOST)
            if isinstance(exc, CoordinationError):
                raise
            raise CoordinationError(
                CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
            ) from exc
        if not attested:
            await self._terminate_or_hold(CoordinationReasonCode.LEASE_LOST)
            raise CoordinationError(CoordinationReasonCode.LEASE_LOST)

        confirmed: list[int] = []
        try:
            for key in reversed(self._grant.keys):
                if not await _unlock_proven(self._connection, key):
                    raise CoordinationError(CoordinationReasonCode.LEASE_LOST)
                confirmed.append(key)
            # A session advisory lock is re-entrant, so one true unlock does not
            # prove the row is gone. Ask the same backend.
            remaining = await _read_owned_advisory_rows(
                self._connection, self._grant.backend_pid
            )
        except CoordinationError:
            self._unlocked_keys = tuple(confirmed)
            await self._terminate_or_hold(CoordinationReasonCode.LEASE_LOST)
            raise
        except BaseException as exc:
            self._unlocked_keys = tuple(confirmed)
            await self._terminate_or_hold(
                CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
            )
            raise CoordinationError(
                CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
            ) from exc

        self._unlocked_keys = tuple(confirmed)
        if _rows_hold_any_key(
            remaining,
            keys=self._grant.keys,
            backend_pid=self._grant.backend_pid,
            database_oid=self._grant.database_oid,
        ):
            # A row survived the unlock: this backend held the key more than
            # once, so the account is still locked.
            await self._terminate_or_hold(CoordinationReasonCode.LEASE_LOST)
            raise CoordinationError(CoordinationReasonCode.LEASE_LOST)

        # Proven released at this instant. Resolving before the fallible close
        # keeps introspection honest: a pool-return error must not leave a
        # released authority reported as still held.
        self._released = True
        self._resolve_own_hold()
        _unregister_owned_coordination(self)
        # The close failure is still surfaced — it just cannot un-prove the
        # unlock that already happened.
        await self._connection.close()

    def _resolve_own_hold(self) -> None:
        _resolve_authority_hold(self._hold, owner=self, connection=self._connection)
        self._hold = None

    async def _terminate_or_hold(self, reason_code: CoordinationReasonCode) -> None:
        receipt = await _terminate_authority(self._connection, self._grant)
        if receipt is not None:
            # The exact backend is provably gone, so every advisory key it held
            # is gone with it. That is an allowed release outcome even though the
            # caller still sees the failure that forced it.
            self._termination_receipt = receipt
            self._released = True
            self._resolve_own_hold()
            _unregister_owned_coordination(self)
            return
        if self._hold is not None:
            # One lease is one authority: a repeat failure is the *same*
            # unresolved hold, not a second one accumulating in the active view.
            return
        self._hold = _record_unreleased_authority(
            self._grant,
            owner=self,
            reason_code=reason_code,
            termination_proven=False,
            durable_evidence_written=True,
            connection=self._connection,
            # A sealed lease already has an opaque id an operator is following;
            # allocating a second one here would split one stuck authority
            # across two ids with no public way to join them. An unsealed lease
            # has no such id, so it keeps a fresh allocation.
            hold_id=self._coordination_hold_id,
        )


async def acquire_physical_account_lease(
    *,
    keys: Sequence[int],
    connection_factory: LockAuthorityConnectionFactory,
) -> PostgresAdvisoryKeysetLease:
    """Acquire an ordered advisory keyset on one dedicated session.

    The sequence is fixed: verify the authority can really terminate its own
    backend, retain the backend PID, ``pg_try_advisory_lock`` each key in
    deterministic order, cross an **explicit COMMIT** boundary, then re-read the
    PID and prove every exact ``pg_locks`` row in the new transaction.  A
    transaction pooler that hands the next statement to a different backend fails
    that attestation instead of silently downgrading a session lock.

    Contention yields ``lease_contended``.  Any unattested or failed authority —
    including one that cannot terminate its own backend session — yields
    ``lock_authority_unavailable``.  Neither leaves a key provably held.
    """

    loop = asyncio.get_running_loop()
    ordered_keys = ordered_advisory_keyset(keys)
    if not ordered_keys:
        raise CoordinationError(CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE)

    connection = await _open_lock_authority(connection_factory)

    # The exact session identity is read BEFORE the first lock attempt. Every
    # rollback, hold, and termination request downstream needs the real backend
    # PID; a zero placeholder would ask PostgreSQL to terminate backend 0 and
    # leave the actual lock held.
    try:
        backend_pid, database_oid = await _read_session_identity(connection)
    except Exception as exc:
        # No key has been requested yet, so nothing can be held: closing is both
        # safe and the only thing left to do.
        await _close_quietly(connection)
        raise CoordinationError(
            CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
        ) from exc

    # PostgreSQL session advisory locks are re-entrant per backend: a pooled
    # connection that already holds one of these keys would answer
    # pg_try_advisory_lock with true, and one later unlock would leave the row
    # standing. Refuse to start from a non-zero baseline rather than mistake a
    # stacked lock for an exclusive one.
    try:
        preexisting = await _read_owned_advisory_rows(connection, backend_pid)
    except Exception as exc:
        await _close_quietly(connection)
        raise CoordinationError(
            CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
        ) from exc
    if _rows_hold_any_key(
        preexisting,
        keys=ordered_keys,
        backend_pid=backend_pid,
        database_oid=database_oid,
    ):
        await _close_quietly(connection)
        raise CoordinationError(CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE)

    grant = AdvisoryLeaseGrant(
        keys=ordered_keys,
        backend_pid=backend_pid,
        database_oid=database_oid,
        connection_token=f"lockconn:{secrets.token_hex(8)}",
        event_loop=loop,
    )
    acquired: list[int] = []
    # A key is marked in flight *before* the statement is dispatched and cleared
    # only once a definite answer comes back. Anything still listed here has an
    # unknown owner: PostgreSQL may have granted it before the client-side await
    # failed, exactly like a broker submit that times out locally after the
    # remote mutation already happened.
    in_flight: list[int] = []
    try:
        await _acquire_attested_keyset(connection, grant, acquired, in_flight)
    except BaseException:
        # The rollback runs as a retained task: a cancellation arriving *during*
        # it must not abandon a connection that may still hold a key. The
        # original failure is re-raised only after the rollback reached a safe
        # outcome — confirmed unlock, positive termination, or strong retention.
        rollback: asyncio.Task[None] = asyncio.ensure_future(
            _rollback_partial_acquisition(
                connection, acquired, grant, in_flight=tuple(in_flight)
            )
        )
        rollback_cancellation = await _await_retained_task(rollback)
        if _task_error(rollback) is not None:
            # The rollback itself failed, so nothing proved this connection is
            # safe and nothing may have retained it. Retain it here rather than
            # letting it fall out of scope with a key possibly still held.
            _record_unreleased_authority(
                grant,
                owner=connection,
                reason_code=CoordinationReasonCode.LEASE_LOST,
                termination_proven=False,
                durable_evidence_written=True,
                connection=connection,
            )
        if rollback_cancellation is not None:
            # A cancellation that arrived while the rollback was still running is
            # re-delivered now that a safe outcome exists — never before it.
            raise rollback_cancellation
        raise

    return PostgresAdvisoryKeysetLease(connection=connection, grant=grant)


async def _open_lock_authority(
    connection_factory: LockAuthorityConnectionFactory,
) -> LockAuthorityConnection:
    try:
        connection = await connection_factory()
    except Exception as exc:
        raise CoordinationError(
            CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
        ) from exc
    if not supports_backend_session_termination(connection):
        # No lock is held yet, so closing is safe here — and it is the only
        # thing this object is actually able to do.
        try:
            await connection.close()
        except Exception:
            pass
        raise CoordinationError(CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE)
    return connection


async def _acquire_attested_keyset(
    connection: LockAuthorityConnection,
    grant: AdvisoryLeaseGrant,
    acquired: list[int],
    in_flight: list[int],
) -> None:
    backend_pid = grant.backend_pid
    database_oid = grant.database_oid
    ordered_keys = grant.keys
    try:
        for key in ordered_keys:
            in_flight.append(key)
            result = await connection.execute(
                text(_TRY_ADVISORY_LOCK_SQL), {"key": key}
            )
            # Only an explicit, definite ``False`` proves this key was not
            # granted. Any raise or cancellation between dispatch and here
            # leaves the key marked in flight, because local silence is not
            # evidence that the server did nothing.
            if not bool(result.scalar_one()):
                in_flight.remove(key)
                raise CoordinationError(CoordinationReasonCode.LEASE_CONTENDED)
            in_flight.remove(key)
            acquired.append(key)
        # Explicit COMMIT boundary: everything after this runs in a new
        # transaction, which is exactly where a transaction pooler would move us
        # to a different backend session.
        await connection.commit()
        attested = await _attest_full_ownership(
            connection,
            keys=ordered_keys,
            backend_pid=backend_pid,
            database_oid=database_oid,
        )
    except CoordinationError:
        raise
    except Exception as exc:
        raise CoordinationError(
            CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
        ) from exc
    if not attested:
        raise CoordinationError(CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE)


async def _rollback_partial_acquisition(
    connection: LockAuthorityConnection,
    acquired: Sequence[int],
    grant: AdvisoryLeaseGrant,
    *,
    in_flight: Sequence[int] = (),
) -> None:
    """Unlock every already-acquired key in reverse order, proving each one.

    If any unlock cannot be proven, the session is terminated rather than
    closed: an unproven advisory lock on a surviving backend is exactly the
    stuck-account state this module exists to avoid.  If termination itself
    cannot be proven, that becomes an auditable hold rather than silence.

    When a key was still **in flight**, its ownership is unknown rather than
    absent, so the confirmed-only unlock path and the ordinary close are both
    off limits: this demands a positive exact-PID termination receipt, and
    failing that retains the real connection under an auditable hold.
    """

    if in_flight:
        if await _terminate_authority(connection, grant) is not None:
            return
        _record_unreleased_authority(
            grant,
            owner=connection,
            reason_code=CoordinationReasonCode.LEASE_LOST,
            termination_proven=False,
            durable_evidence_written=True,
            connection=connection,
        )
        return

    proven = True
    try:
        for key in reversed(tuple(acquired)):
            if not await _unlock_proven(connection, key):
                proven = False
                break
    except BaseException:
        # Including a cancellation: an interrupted unlock proves nothing, so the
        # remainder is unknown rather than absent.
        proven = False
    if proven:
        await _close_quietly(connection)
        return
    if await _terminate_authority(connection, grant) is None:
        # Metadata alone would let a GC pass or a pool return drop the very
        # authority it claims is still held, so the real connection is retained.
        _record_unreleased_authority(
            grant,
            owner=connection,
            reason_code=CoordinationReasonCode.LEASE_LOST,
            termination_proven=False,
            durable_evidence_written=True,
            connection=connection,
        )


# --------------------------------------------------------------------------
# B-4 — durable binary send reservation (adapter, never a state machine)
# --------------------------------------------------------------------------


@runtime_checkable
class OrderSendIntentReservationPort(Protocol):
    """The exact binary-reservation surface J3A consumes.

    ``release`` is intentionally absent.  The unrestricted delete is never part
    of this port, so no coordination path can reach it; only the evidence-gated
    ``release_if_matches`` below may remove a claim.
    """

    async def has_reservations(self, *, account_scope: str) -> bool: ...

    async def reserve(
        self,
        *,
        account_scope: str,
        idempotency_key: str,
        symbol: str | None = None,
        side: str | None = None,
        conflicting_key_sides: tuple[tuple[str, str], ...] = (),
    ) -> int: ...

    async def list_reservations(self, *, account_scope: str) -> Sequence[Any]: ...

    async def release_if_matches(
        self,
        *,
        account_scope: str,
        row_id: int,
        idempotency_key: str,
        side: str | None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class DurableClaim:
    """One binary reservation row observed before external I/O.

    This is a claim, not a lifecycle record: there is no state, no hold field,
    no retry counter, and no broker order id.  Its mere existence is the
    account-wide block.
    """

    row_id: int
    claim_account_scope: str
    idempotency_key: str
    side: str | None


@dataclass(frozen=True, slots=True)
class TerminalClaimEvidence:
    """The evidence a lane must present before a claim may be deleted.

    Every field defaults to absent, so a default-constructed instance authorizes
    nothing.  Unknown results, anomalies, a broker rejection without proven
    absence, and a partial fill with an unknown remainder all fail to set these
    flags — which is exactly why they retain the claim and keep the account
    blocked until a human resolves them.  Nothing releases a claim automatically.
    """

    lane_native_terminal_evidence: bool = False
    account_position_reconciled: bool = False
    remainder_known: bool = False
    authoritative_absence_proven: bool = False

    @property
    def exact_booleans(self) -> bool:
        """Every field is a real ``bool``, not merely something truthy.

        The string ``"false"`` is truthy. So is ``"0"``. Accepting either here
        would let a mistyped field authorize the deletion of the durable claim
        that is the entire account block.
        """

        return all(
            value is True or value is False
            for value in (
                self.lane_native_terminal_evidence,
                self.account_position_reconciled,
                self.remainder_known,
                self.authoritative_absence_proven,
            )
        )

    @property
    def authorizes_release(self) -> bool:
        if self.authoritative_absence_proven:
            return self.account_position_reconciled
        return (
            self.lane_native_terminal_evidence
            and self.account_position_reconciled
            and self.remainder_known
        )


def _terminal_evidence_authorizes(evidence: TerminalClaimEvidence) -> bool:
    """The release predicate, recomputed from exact booleans and not overridable."""

    if evidence.authoritative_absence_proven is True:
        return evidence.account_position_reconciled is True
    return (
        evidence.lane_native_terminal_evidence is True
        and evidence.account_position_reconciled is True
        and evidence.remainder_known is True
    )


class DurableSendClaimAdapter:
    """Adapts the existing binary reservation service to J3A coordination.

    The underlying ``OrderSendIntentService`` is imported and adapted, never
    edited into a state machine: its schema holds only binary claim facts and
    this adapter adds none.
    """

    __slots__ = ("_intents",)

    def __init__(self, intents: OrderSendIntentReservationPort) -> None:
        self._intents = intents

    async def account_has_unresolved_claim(self, scope: PhysicalAccountScope) -> bool:
        """Any unresolved reservation blocks every new order on the account."""

        return bool(
            await self._intents.has_reservations(
                account_scope=scope.claim_account_scope
            )
        )

    async def reserve(
        self,
        *,
        scope: PhysicalAccountScope,
        idempotency_key: str,
        symbol: str | None = None,
        side: str | None = None,
    ) -> DurableClaim:
        """Insert and commit the binary claim before any broker callback runs."""

        try:
            row_id = await self._intents.reserve(
                account_scope=scope.claim_account_scope,
                idempotency_key=idempotency_key,
                symbol=symbol,
                side=side,
            )
        except DuplicateOrderIntent as exc:
            raise CoordinationError(
                CoordinationReasonCode.DURABLE_CLAIM_CONFLICT
            ) from exc
        return DurableClaim(
            row_id=row_id,
            claim_account_scope=scope.claim_account_scope,
            idempotency_key=idempotency_key,
            side=side,
        )

    async def release_with_terminal_evidence(
        self, claim: DurableClaim, evidence: TerminalClaimEvidence
    ) -> int:
        """Delete the exact claim row only after terminal + reconcile evidence.

        Without sufficient evidence this raises before touching the database, so
        an evidence-less release cannot happen even by accident.
        """

        # Checked here rather than trusting the property alone: a subclass can
        # override `authorizes_release`, and this adapter is the last thing
        # between a caller's claim of evidence and a durable delete.
        if (
            type(evidence) is not TerminalClaimEvidence
            or not evidence.exact_booleans
            or not _terminal_evidence_authorizes(evidence)
        ):
            raise CoordinationError(CoordinationReasonCode.TERMINAL_EVIDENCE_REQUIRED)
        return int(
            await self._intents.release_if_matches(
                account_scope=claim.claim_account_scope,
                row_id=claim.row_id,
                idempotency_key=claim.idempotency_key,
                side=claim.side,
            )
        )


# --------------------------------------------------------------------------
# B-10 — follow-up capability representation only; never an authorization
# --------------------------------------------------------------------------

CLAIM_FOLLOWUP_OPERATIONS: Final[frozenset[str]] = frozenset({"cancel", "reduce"})


@dataclass(frozen=True, slots=True)
class ClaimFollowupRequest:
    """What a later lane claims to hold before a cancel/reduce follow-up."""

    operation: str
    lane_capability_supports_operation: bool = False
    attributed_native_order_id: str | None = None
    known_remainder: Decimal | None = None
    fresh_guards_passed: bool = False
    lease_ownership_verified: bool = False


@dataclass(frozen=True, slots=True)
class ClaimFollowupCapability:
    """A capability description — deliberately not an authorization.

    ``authorizes_broker_mutation`` and ``releases_durable_claim`` are structural
    ``False``: they cannot be constructed as ``True``.  Whether a follow-up is
    actually permitted, and what transport performs it, stays with J3B/J3C.
    """

    operation: str
    capability_present: bool
    reason_code: CoordinationReasonCode | None
    authorizes_broker_mutation: bool = field(default=False, init=False)
    releases_durable_claim: bool = field(default=False, init=False)


def describe_claim_followup(request: ClaimFollowupRequest) -> ClaimFollowupCapability:
    """Report whether every follow-up precondition is present.

    Increasing quantity, changing symbol, or creating a new order under the same
    claim is not describable here: only ``cancel`` and ``reduce`` are operations
    at all.  Any missing element yields ``claim_followup_not_authorized`` and the
    reservation is retained either way.
    """

    capability_present = (
        request.operation in CLAIM_FOLLOWUP_OPERATIONS
        and request.lane_capability_supports_operation
        and isinstance(request.attributed_native_order_id, str)
        and bool(request.attributed_native_order_id.strip())
        and request.known_remainder is not None
        and request.fresh_guards_passed
        and request.lease_ownership_verified
    )
    return ClaimFollowupCapability(
        operation=request.operation,
        capability_present=capability_present,
        reason_code=(
            None
            if capability_present
            else CoordinationReasonCode.CLAIM_FOLLOWUP_NOT_AUTHORIZED
        ),
    )


# --------------------------------------------------------------------------
# B-6 — coordinated mutation with an injected callback + durable evidence
# --------------------------------------------------------------------------


class MutationCertainty(StrEnum):
    """Whether the transport reached a definite answer or a definite unknown."""

    DEFINITIVE = "definitive"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class MutationCallbackResult:
    """What a lane's injected callback reports back.

    ``broker_order_id`` is the value the *lane* already extracted from its native
    response; J3A parses no broker payload and only hands it to J2B's
    acknowledgement helper.
    """

    certainty: MutationCertainty
    broker_order_id: str | None = None


class _ScopeGate:
    """Liveness flag for one coordinated section; not reachable from the view."""

    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


class CoordinationScope:
    """What a lane callback receives: one operation, and no capability at all.

    A broker callback can await account truth, a token refresh, or rate limiting
    after it is entered, and a same-cycle batch can contain several POSTs. The
    coordinator's single pre-callback attestation is therefore neither temporally
    nor semantically sufficient — J3B and J3C must re-assert immediately before
    **every** send.

    So this exposes exactly one coroutine. It carries no lease, grant,
    connection, backend PID, owner token, key, hold id, release, or termination —
    reading the state and changing it are different rights, and only the first is
    handed out. It also stops working once its coordinated section ends, so a
    captured scope cannot assert against a finished lease.
    """

    __slots__ = ("_assert",)

    def __init__(self, assert_owned: Callable[[], Awaitable[None]]) -> None:
        self._assert = assert_owned

    async def assert_owned(self) -> None:
        """Re-prove ownership *and* the pinned lane binding. Call before each send."""

        await self._assert()


type MockMutationCallback = Callable[
    [CoordinationScope], Awaitable[MutationCallbackResult]
]


class DispatchEvidenceKind(StrEnum):
    """What this process actually learned about one dispatch.

    This is an *evidence* vocabulary, not a reason-code vocabulary: it never
    overlaps :class:`CoordinationReasonCode` and carries no broker state. It
    exists so that "we do not know whether the order went out" is a typed,
    durable fact rather than an inference from a missing field.
    """

    ACKNOWLEDGED = "acknowledged"
    DEFINITIVE_WITHOUT_BROKER_ID = "definitive_without_broker_id"
    LANE_REPORTED_UNCERTAIN = "lane_reported_uncertain"
    CALLBACK_FAILED = "callback_failed"
    ACK_ATTACHMENT_FAILED = "ack_attachment_failed"


@dataclass(frozen=True, slots=True)
class DispatchEvidence:
    """Immutable record of exactly what this process learned about one dispatch.

    Every outcome is durably persisted before any cleanup runs:

    1. a definitive acknowledgement (``ACKNOWLEDGED``);
    2. a definitive uncertainty reported by the lane
       (``LANE_REPORTED_UNCERTAIN``);
    3. a callback failure (``CALLBACK_FAILED``) — uncertain, because the write
       may well have reached the broker;
    4. a broker order id that could not be normalized or attached
       (``ACK_ATTACHMENT_FAILED``) — also uncertain, and never recorded as a
       success just because the transport returned.

    An outer cancellation is orthogonal to all of them and is recorded by
    ``outer_cancellation_requested``: cancelling the *caller* says nothing about
    what the *transport* did, so it never overwrites the kind.

    The lineage correlation fields make this record self-describing at rest: a
    later reconciler can tie the unknown back to its exact intent, plan, and
    attempt without reading this process's memory.  There is no free-form broker
    state and no reason code here — native lifecycle detail belongs to the lane's
    own service, and the durable claim referenced here remains the account block
    until evidence-gated release.
    """

    envelope: LineageEnvelope
    kind: DispatchEvidenceKind
    certainty: MutationCertainty
    broker_order_id: str | None
    callback_failed: bool
    ack_attachment_failed: bool
    outer_cancellation_requested: bool
    decision_intent_id: str
    execution_plan_id: str
    order_attempt_id: str
    cycle_id: str
    attempt_seq: int | None
    claim_account_scope: str
    claim_row_id: int
    idempotency_key: str


def _dispatch_evidence_kind(
    result: MutationCallbackResult | None,
    callback_error: BaseException | None,
    ack_error: BaseException | None,
) -> DispatchEvidenceKind:
    if callback_error is not None or result is None:
        return DispatchEvidenceKind.CALLBACK_FAILED
    if ack_error is not None:
        return DispatchEvidenceKind.ACK_ATTACHMENT_FAILED
    if result.certainty is MutationCertainty.UNCERTAIN:
        return DispatchEvidenceKind.LANE_REPORTED_UNCERTAIN
    if result.broker_order_id is None:
        return DispatchEvidenceKind.DEFINITIVE_WITHOUT_BROKER_ID
    return DispatchEvidenceKind.ACKNOWLEDGED


@runtime_checkable
class DispatchEvidencePort(Protocol):
    """Lane-owned durable write boundary for dispatch evidence.

    J3A supplies no implementation: a lane wires this to its existing native
    lifecycle service.  ``review.order_send_intents`` is explicitly *not* an
    acceptable target — it is a binary reservation, not a state store.
    """

    async def persist_dispatch_evidence(
        self, evidence: DispatchEvidence, /
    ) -> None: ...


def require_dispatch_evidence_port(
    port: DispatchEvidencePort | None, /
) -> DispatchEvidencePort:
    """Fail closed when a lane has not supplied its dispatch-evidence port."""

    if port is None or not isinstance(port, DispatchEvidencePort):
        raise CoordinationError(CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE)
    return port


@dataclass(frozen=True, slots=True)
class HeldCoordinationSnapshot:
    """A capability-free view of one held coordination.

    Enough to see *that* an account is stuck and why; nothing with which to
    unstick it. There is no lease, no grant, no connection, no PID, no owner
    token, and nothing callable — the release capability lives only in the
    module-private ownership maps.
    """

    hold_id: str
    claim_account_scope: str
    durable_evidence_written: bool
    released: bool


@dataclass(frozen=True, slots=True)
class _HeldCoordination:
    """A strong handle on coordination authority that is currently in use.

    Created the instant the durable binary reservation is committed and *before*
    the broker callback task exists, so there is no window in which an exception
    unwinding, a garbage collection, or a pool return can quietly drop the
    authority for an order that may already be on the wire.

    This map is a lifetime and safety guard, nothing more.  It is **not** a retry
    queue, **not** a durable state store, and it has no TTL, janitor, takeover,
    automatic retry, claim deletion, or scheduler.  Process death may end the
    ephemeral DB session, but the durable binary reservation survives and keeps
    blocking a successor, which is the property that actually matters.
    """

    hold_id: str
    lease: PostgresAdvisoryKeysetLease
    grant: AdvisoryLeaseGrant
    claim: DurableClaim
    envelope: LineageEnvelope


# Strong references, keyed by the opaque hold id and nothing else.
_HELD_COORDINATION: dict[str, _HeldCoordination] = {}

# The release rights for each held lease. Deliberately module-private with no
# accessor. Public introspection returns ``HeldCoordinationSnapshot`` objects,
# which carry no lease, grant, connection, PID, owner token, or callable — so
# operators and later lanes can see *what* is stuck without being able to
# unstick it. Capability identity cannot be reconstructed from anything public,
# so a generic caller cannot forge one.
_COORDINATION_RELEASE_CAPABILITIES: dict[str, object] = {}


def _snapshot_held(held: _HeldCoordination) -> HeldCoordinationSnapshot:
    hold = held.lease.unreleased_authority_hold
    return HeldCoordinationSnapshot(
        hold_id=held.hold_id,
        claim_account_scope=held.claim.claim_account_scope,
        durable_evidence_written=(
            hold.durable_evidence_written if hold is not None else False
        ),
        released=held.lease.released,
    )


def held_coordinations() -> Mapping[str, HeldCoordinationSnapshot]:
    """Every coordination authority this process is still holding, redacted."""

    return MappingProxyType(
        {hold_id: _snapshot_held(held) for hold_id, held in _HELD_COORDINATION.items()}
    )


def held_coordination(hold_id: str) -> HeldCoordinationSnapshot | None:
    """Look one held authority up by its opaque id, capability-free."""

    held = _HELD_COORDINATION.get(hold_id)
    return None if held is None else _snapshot_held(held)


def _held_coordination(hold_id: str) -> _HeldCoordination | None:
    """Module-private: the real handle, for the coordination-owned path only."""

    return _HELD_COORDINATION.get(hold_id)


def _register_held_coordination(
    *,
    lease: PostgresAdvisoryKeysetLease,
    grant: AdvisoryLeaseGrant,
    claim: DurableClaim,
    envelope: LineageEnvelope,
) -> _HeldCoordination:
    held = _HeldCoordination(
        hold_id=_allocate_hold_id(),
        lease=lease,
        grant=grant,
        claim=claim,
        envelope=envelope,
    )
    capability = object()
    _COORDINATION_RELEASE_CAPABILITIES[held.hold_id] = capability
    # Sealed at registration — before the callback task exists — so there is no
    # instant in which the handle is public and the lease is generically
    # releasable.
    lease._seal_for_coordination(hold_id=held.hold_id, capability=capability)
    _HELD_COORDINATION[held.hold_id] = held
    return held


async def _release_and_unregister(held: _HeldCoordination) -> None:
    """Drop the handle only after an allowed release outcome is proven.

    This is the single coordination-owned release path. It runs only once both
    post-send durable writes have landed (or, on the early-failure path, before
    the callback ever started), and it is the only caller that holds the
    capability the lease was sealed with.
    """

    capability = _COORDINATION_RELEASE_CAPABILITIES.get(held.hold_id)
    await held.lease._release_with_capability(held.grant, capability)
    # Compare-and-delete: only this exact handle's entries, never a namesake's.
    if _HELD_COORDINATION.get(held.hold_id) is held:
        _COORDINATION_RELEASE_CAPABILITIES.pop(held.hold_id, None)
        del _HELD_COORDINATION[held.hold_id]


async def _release_and_unregister_quietly(held: _HeldCoordination) -> None:
    """Release on an already-failing path; keep the handle if unproven."""

    try:
        await _release_and_unregister(held)
    except CoordinationError:
        pass


@dataclass(frozen=True, slots=True)
class CoordinatedMutationResult:
    """Outcome of one coordinated mutation; the durable claim is still held."""

    envelope: LineageEnvelope
    scope: PhysicalAccountScope
    claim: DurableClaim
    certainty: MutationCertainty
    evidence: DispatchEvidence
    # Opaque derived keys only — never the grant, which pairs a backend PID with
    # an owner token and is therefore a termination capability.
    lease_keys: tuple[int, ...]


async def _await_retained_task(
    task: asyncio.Task[Any],
) -> asyncio.CancelledError | None:
    """Wait for a retained task without ever cancelling it.

    ``asyncio.shield`` alone is not enough: it lets the outer awaiter raise
    ``CancelledError`` while the inner socket write — or the evidence write that
    proves what happened to it, or the unlock sequence that gives up the
    account — continues, after which a naive ``finally`` would release the lease
    and reservation on an order that may well have reached the broker.  So the
    outer cancellation is captured and the retained task is awaited to a definite
    result first.
    """

    outer_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            outer_cancellation = outer_cancellation or exc
            if task.done():
                break
        except BaseException:
            # The retained task itself failed; ``task`` now carries it.
            break
    return outer_cancellation


def _task_error(task: asyncio.Task[Any]) -> BaseException | None:
    if task.cancelled():
        return asyncio.CancelledError()
    return task.exception()


async def _run_retained(
    make_awaitable: Callable[[], Awaitable[Any]],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    """Start, retain, and fully await one durable write."""

    try:
        task: asyncio.Task[Any] = asyncio.ensure_future(make_awaitable())
    except BaseException as exc:
        return exc, None
    cancellation = await _await_retained_task(task)
    return _task_error(task), cancellation


def _callback_outcome(
    task: asyncio.Task[MutationCallbackResult],
) -> tuple[MutationCallbackResult | None, BaseException | None]:
    if task.cancelled():
        return None, asyncio.CancelledError()
    callback_error = task.exception()
    if callback_error is not None:
        return None, callback_error
    return task.result(), None


def _validated_callback_result(
    result: object,
) -> tuple[MutationCallbackResult | None, BaseException | None]:
    """Accept only the exact supported result shape.

    A type annotation is not a runtime guarantee. A raw-string certainty slips
    past every ``is MutationCertainty.X`` branch and lands in the durable record
    misclassified as an acknowledgement; a dict or a stray integer raises on
    attribute access *after* a possible send but before either mandatory write.
    Both become typed uncertainty instead.
    """

    if type(result) is not MutationCallbackResult:
        return None, TypeError(
            f"mock mutation callback returned {type(result).__name__}, "
            "expected MutationCallbackResult"
        )
    if not isinstance(result.certainty, MutationCertainty):
        return None, TypeError(
            "mock mutation callback returned a non-MutationCertainty certainty"
        )
    broker_order_id = result.broker_order_id
    if broker_order_id is not None and (
        not isinstance(broker_order_id, str) or not broker_order_id.strip()
    ):
        return None, TypeError(
            "mock mutation callback returned a non-string broker order id"
        )
    return result, None


def _required_persistence(
    persistence: LineagePersistencePort | None,
) -> LineagePersistencePort:
    try:
        return require_lineage_persistence_port(persistence)
    except LineagePersistenceUnavailable as exc:
        raise CoordinationError(
            CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
        ) from exc


def _require_pinned_entry(
    envelope: LineageEnvelope,
    registry: RegistrySource | None,
    pinned: LaneRegistryEntry,
) -> LaneRegistryEntry:
    """Re-validate, then require the *same* entry that authority was derived from.

    ``RegistrySource`` accepts any mapping or iterable and re-materializes it on
    every call, so a registry mutated during an awaited reserve could validate a
    different physical account on the second look while the callback proceeds
    behind the first account's lease and claim. Full entry equality is the
    conservative comparison: any authority-relevant drift fails.
    """

    entry = _validated_entry(envelope, registry)
    if entry != pinned:
        raise LaneGuardError("canonical_lane_identity_mismatch", lane_id=pinned.lane_id)
    return entry


def _validated_entry(
    envelope: LineageEnvelope, registry: RegistrySource | None
) -> LaneRegistryEntry:
    entry = assert_lineage_registry_binding(envelope, registry)
    assert_entry_execution_ready(entry)
    if envelope.order_attempt is None:
        raise LaneGuardError("lane_binding_incomplete", lane_id=entry.lane_id)
    return entry


async def coordinate_mock_order_mutation(
    *,
    envelope: LineageEnvelope,
    persistence: LineagePersistencePort | None,
    dispatch_evidence: DispatchEvidencePort | None,
    claims: DurableSendClaimAdapter,
    connection_factory: LockAuthorityConnectionFactory,
    mutation: MockMutationCallback,
    registry: RegistrySource | None = None,
    lineage_factory: MockLineageFactory | None = None,
    additional_advisory_keys: Sequence[int] = (),
) -> CoordinatedMutationResult:
    """Run one mutation behind the full J3A coordination order.

    The order is load-bearing, not stylistic::

        1. canonical J2A lane/identity/policy validation
        2. require BOTH lane-supplied durable write ports
        3. persist the immutable envelope — and abort here if the caller was
           cancelled, rather than proceeding to a lease, a claim, and a send
        4. acquire the authoritative physical-account lease
        5. check account-wide unresolved reservations
        6. reserve and COMMIT the binary claim
        7. re-assert lease ownership and re-run the lane guards
        8. invoke the injected callback, retained against cancellation
        9. persist the ACK/uncertainty envelope AND the typed dispatch evidence,
           both retained against cancellation
       10. release the lease **only if** step 9 was durable — otherwise keep the
           writer authority and record an :class:`UnreleasedAuthorityHold`

    Steps 1-3 run before any lock is taken so a rejected plan never contends.
    Step 6 commits before step 8 so a crash mid-send leaves a durable claim.
    Step 9 completes before step 10 so the lease is never surrendered while a
    transport — or the evidence that describes it — is still in flight.  The
    durable claim is never released here: only
    :meth:`DurableSendClaimAdapter.release_with_terminal_evidence` may remove it.

    ``additional_advisory_keys`` is how a later broker lane supplies its own
    extra keys (for example a legacy compatibility key).  J3A never chooses them.
    """

    entry = _validated_entry(envelope, registry)
    # Both are proven present by ``_validated_entry`` above.
    order_attempt = envelope.order_attempt
    execution_plan = envelope.execution_plan
    if order_attempt is None or execution_plan is None:  # pragma: no cover
        raise LaneGuardError("lane_binding_incomplete", lane_id=entry.lane_id)

    scope = physical_account_scope_for_entry(entry)
    port = _required_persistence(persistence)
    evidence_port = require_dispatch_evidence_port(dispatch_evidence)

    persist_error, persist_cancellation = await _run_retained(
        lambda: port.persist(envelope)
    )
    if persist_error is not None:
        raise CoordinationError(
            CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
            lane_id=entry.lane_id,
        ) from persist_error
    if persist_cancellation is not None:
        # Pre-send cancellation. The mandatory persist finished, and we stop
        # here: no lease, no reservation, and above all no broker callback.
        raise persist_cancellation

    lease = await acquire_physical_account_lease(
        keys=(scope.advisory_key, *additional_advisory_keys),
        connection_factory=connection_factory,
    )
    grant = lease.grant
    claim: DurableClaim | None = None
    held: _HeldCoordination | None = None
    try:
        if await claims.account_has_unresolved_claim(scope):
            raise CoordinationError(
                CoordinationReasonCode.DURABLE_CLAIM_CONFLICT, lane_id=entry.lane_id
            )
        claim = await claims.reserve(
            scope=scope,
            idempotency_key=order_attempt.idempotency_key,
            symbol=execution_plan.normalized_symbol,
            side=envelope.decision_intent.side,
        )
        await lease.assert_owned(grant)
        _require_pinned_entry(envelope, registry, entry)
        # Strong handle first, callback second: between these two lines there is
        # no order in flight, and after them nothing can drop the authority
        # implicitly.
        held = _register_held_coordination(
            lease=lease, grant=grant, claim=claim, envelope=envelope
        )
    except BaseException:
        # Genuinely pre-callback: the callable was never invoked, so nothing can
        # be in flight. The durable claim, if taken, stays put — absence of send
        # is not proven here.
        if held is None:
            await _release_lease_quietly(lease, grant)
        else:
            await _release_and_unregister_quietly(held)
        raise

    if claim is None:  # pragma: no cover - the try block either reserves or raises
        raise CoordinationError(
            CoordinationReasonCode.DURABLE_CLAIM_CONFLICT, lane_id=entry.lane_id
        )

    # Everything from here is post-invocation. A supplied callable can run a
    # synchronous broker SDK prelude and *then* raise, or return a non-awaitable;
    # neither is "the callback never started", because an order may already have
    # gone out. Both take the durable-evidence path.
    gate = _ScopeGate()

    async def _assert_scope_owned() -> None:
        if not gate.active:
            raise CoordinationError(
                CoordinationReasonCode.LEASE_LOST, lane_id=entry.lane_id
            )
        await lease.assert_owned(grant)
        _require_pinned_entry(envelope, registry, entry)

    task: asyncio.Task[MutationCallbackResult] | None = None
    invocation_error: BaseException | None = None
    try:
        started = mutation(CoordinationScope(_assert_scope_owned))
        if not isinstance(started, Awaitable):
            raise TypeError(
                "mock mutation callback did not return an awaitable; the send "
                "may already have happened synchronously"
            )
        task = asyncio.ensure_future(started)
    except BaseException as exc:
        invocation_error = exc

    if task is None:
        outer_cancellation = None
        result, callback_error = None, invocation_error
    else:
        outer_cancellation = await _await_retained_task(task)
        result, callback_error = _callback_outcome(task)
    # The coordinated section is over: a captured scope must stop working.
    gate.active = False
    if callback_error is None:
        result, callback_error = _validated_callback_result(result)

    factory = lineage_factory if lineage_factory is not None else MockLineageFactory()
    evidence_envelope = envelope
    ack_error: BaseException | None = None
    if (
        result is not None
        and result.certainty is MutationCertainty.DEFINITIVE
        and result.broker_order_id is not None
    ):
        try:
            evidence_envelope = factory.acknowledge_order_attempt(
                envelope, result.broker_order_id
            )
        except Exception as exc:
            # A malformed or conflicting broker order id is transport
            # uncertainty, not a reason to escape before recording the unknown.
            ack_error = exc
            evidence_envelope = envelope

    kind = _dispatch_evidence_kind(result, callback_error, ack_error)
    evidence = DispatchEvidence(
        envelope=evidence_envelope,
        kind=kind,
        # A failed callback or an unusable acknowledgement is an *uncertainty*:
        # the write may already have landed at the broker.
        certainty=(
            MutationCertainty.UNCERTAIN
            if (result is None or ack_error is not None)
            else result.certainty
        ),
        broker_order_id=(
            result.broker_order_id
            if (result is not None and ack_error is None)
            else None
        ),
        callback_failed=callback_error is not None,
        ack_attachment_failed=ack_error is not None,
        outer_cancellation_requested=outer_cancellation is not None,
        decision_intent_id=envelope.decision_intent.decision_intent_id,
        execution_plan_id=execution_plan.execution_plan_id,
        order_attempt_id=order_attempt.order_attempt_id,
        cycle_id=order_attempt.cycle_id,
        attempt_seq=envelope.attempt_seq,
        claim_account_scope=claim.claim_account_scope,
        claim_row_id=claim.row_id,
        idempotency_key=claim.idempotency_key,
    )

    envelope_error, envelope_cancellation = await _run_retained(
        lambda: port.persist(evidence_envelope)
    )
    evidence_error, evidence_cancellation = await _run_retained(
        lambda: evidence_port.persist_dispatch_evidence(evidence)
    )
    outer_cancellation = (
        outer_cancellation or envelope_cancellation or evidence_cancellation
    )
    durable_write_error = envelope_error or evidence_error

    if durable_write_error is not None:
        # The AND gate did not close: nobody knows whether an order went out, so
        # the writer authority is deliberately kept, the strong handle stays in
        # the map, no key is unlocked, and the connection is not returned. The
        # reservation stays unresolved and no successor may safely mutate this
        # physical account. A lane may later finish the durable write with this
        # exact handle and grant; J3A schedules nothing.
        # One stuck authority, one opaque id: the coordination handle and the
        # authority hold share it so an operator follows a single thread.
        lease._retain_authority(
            reason_code=CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
            durable_evidence_written=False,
            hold_id=held.hold_id if held is not None else None,
            capability=(
                _COORDINATION_RELEASE_CAPABILITIES.get(held.hold_id)
                if held is not None
                else None
            ),
        )
        raise CoordinationError(
            CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
            lane_id=entry.lane_id,
            hold_id=held.hold_id if held is not None else None,
        ) from durable_write_error

    # Both durable receipts are in, so the ephemeral lease may be surrendered
    # and the strong handle dropped. The durable claim stays regardless.
    if held is None:  # pragma: no cover - registered before the callback task
        await lease.release(grant)
    else:
        await _release_and_unregister(held)

    if ack_error is not None:
        raise ack_error
    if callback_error is not None:
        raise callback_error
    if outer_cancellation is not None:
        raise outer_cancellation
    if result is None:  # pragma: no cover - no error implies a result
        raise CoordinationError(
            CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
            lane_id=entry.lane_id,
        )
    return CoordinatedMutationResult(
        envelope=evidence_envelope,
        scope=scope,
        claim=claim,
        certainty=result.certainty,
        evidence=evidence,
        lease_keys=grant.keys,
    )


async def _release_lease_quietly(
    lease: PostgresAdvisoryKeysetLease, grant: AdvisoryLeaseGrant
) -> None:
    """Release on an already-failing path without masking the original error.

    A lease that cannot be proven released here has already either terminated
    its backend or recorded an :class:`UnreleasedAuthorityHold`, so the lock is
    never silently abandoned.
    """

    try:
        await lease.release(grant)
    except CoordinationError:
        pass


# Public here means the right to *see*. `AdvisoryLeaseGrant` pairs a backend PID
# with an owner token and is therefore a termination capability;
# `PostgresAdvisoryKeysetLease` holds the connection and the release methods; and
# `acquire_physical_account_lease` is the one sanctioned way to hand both to a
# caller. None of the three is exported. A broker lane supplies its extra keys
# through `coordinate_mock_order_mutation(additional_advisory_keys=...)` and never
# touches a lease.
__all__ = [
    "AUTOMATIC_CLAIM_RELEASE_AVAILABLE",
    "CLAIM_FOLLOWUP_OPERATIONS",
    "COORDINATION_REASON_CODES",
    "FENCING_NOT_BROKER_ENFORCED",
    "LANE_FENCING_MATRIX",
    "LEASE_TTL_SECONDS",
    "NOT_BROKER_ENFORCED_FENCING_STATEMENT",
    "AdvisoryLockRow",
    "BackendSessionTerminationUnproven",
    "BackendTerminationReceipt",
    "ClaimFollowupCapability",
    "ClaimFollowupRequest",
    "CoordinatedMutationResult",
    "CoordinationError",
    "CoordinationScope",
    "CoordinationReasonCode",
    "DispatchEvidence",
    "DispatchEvidenceKind",
    "DispatchEvidencePort",
    "DurableClaim",
    "DurableSendClaimAdapter",
    "HeldCoordinationSnapshot",
    "LockAuthorityConnection",
    "MockMutationCallback",
    "MutationCallbackResult",
    "MutationCertainty",
    "OrderSendIntentReservationPort",
    "PhysicalAccountScope",
    "SqlAlchemyLockAuthority",
    "TerminalClaimEvidence",
    "UnreleasedAuthorityHold",
    "coordinate_mock_order_mutation",
    "describe_claim_followup",
    "held_coordination",
    "held_coordinations",
    "ordered_advisory_keyset",
    "physical_account_scope_for_entry",
    "require_dispatch_evidence_port",
    "row_proves_ownership",
    "split_advisory_key",
    "supports_backend_session_termination",
    "authority_hold_history",
    "unreleased_authority_holds",
]
