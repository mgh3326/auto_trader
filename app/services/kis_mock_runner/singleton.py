"""Cross-process writer singleton and J3B coordination adapter for KIS mock.

The lock is intentionally not a ``ps`` scan: a process list cannot reliably
identify containers, stale PIDs, or another host.  A non-blocking PostgreSQL
advisory lock keyed by the account mode is the authority.  A PID-bearing file
lock gives local operator visibility and catches same-host contention before a
database connection is opened.

ROB-1263 (J3B) turns this module into the **thin KIS adapter** around the merged
J3A coordination primitive rather than a parallel KIS authority:

* the legacy account-mode advisory key is *reused* verbatim and supplied, with
  the J3A-derived physical-account key, as a two-key set — J3A de-duplicates and
  globally orders them, acquires them, and owns every PostgreSQL
  ownership/rollback/unlock mechanic;
* the boolean re-entrancy flag is replaced by a typed, liveness-checked
  authority record, so "a contextvar is ``True``" can no longer stand in for a
  held lease;
* the real client host / credential namespace / account fingerprint are bound to
  the canonical J2A lane entry immediately before **every** HTTP mutation
  attempt, which J3A explicitly delegates here.

Nothing in this module opens a socket, signs a request, registers a scheduler,
adds a model, or edits a J2A/J2B/J3A file.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import path cheap
    from app.services.mock_integration.coordination import (
        AccountUncertaintyGatePort,
        CoordinatedMutationResult,
        CoordinationScope,
        DispatchEvidencePort,
        DurableSendClaimAdapter,
        MutationCallbackResult,
    )
    from app.services.mock_integration.lineage import (
        LineageEnvelope,
        LineagePersistencePort,
        MockLineageFactory,
    )
    from app.services.mock_lane_registry import LaneRegistryEntry

ACCOUNT_MODE = "kis_mock"
MUTATION_WRITER_SURFACES = frozenset(
    {
        "runner",
        "watch_auto_execute",
        "smoke_cli",
        "manual_mcp_mutation",
        # B0-X KR is a separately approved, manual-only adapter path.  It
        # acquires this same account-wide lease before its confirm preflight so
        # it cannot overlap any catalogued kis_mock writer.
        "b0x_adapter",
    }
)


@dataclass(frozen=True, slots=True)
class _WriterAuthority:
    """Typed replacement for the ROB-853 boolean re-entrancy flag.

    The boolean it replaces could outlive the thing it described: a lease
    released without leaving its context left ``True`` behind, and every nested
    mutation then *skipped acquisition* and reached HTTP holding nothing.
    Liveness is therefore recomputed on every read from the object that actually
    holds authority — either the merged J3A grant or an unreleased legacy lease.
    """

    account_mode: str
    advisory_keys: tuple[int, ...]
    lease: object | None = None
    grant: KISMockCoordinationGrant | None = None

    @property
    def live(self) -> bool:
        """Whether this record still describes authority that is actually held.

        A :class:`KISMockWriterLease` publishes ``acquired``, so a lease released
        without leaving its context reads as dead here and forces re-acquisition.
        An injected lease object with no such attribute is live by construction:
        the record only exists inside that object's ``async with`` body.
        """

        if self.grant is not None:
            return True
        if self.lease is None:
            return False
        acquired = getattr(self.lease, "acquired", None)
        return acquired if isinstance(acquired, bool) else True

    def covers(self, *, account_mode: str, required_keys: Sequence[int]) -> bool:
        """Only an exact account mode plus every required key is re-entrant."""

        if self.account_mode != account_mode:
            return False
        held = set(self.advisory_keys)
        return all(key in held for key in required_keys)


_ACTIVE_WRITER_LEASE: ContextVar[_WriterAuthority | None] = ContextVar(
    "kis_mock_runner_active_writer_lease", default=None
)


class WriterSingletonContended(RuntimeError):
    """Another writer is active; the caller must fail closed immediately."""


class WriterSingletonUnavailable(RuntimeError):
    """The durable advisory-lock authority could not be reached."""


class WriterSurfaceUnknown(ValueError):
    """Prevent a new mutation path from bypassing the explicit writer catalog."""


def account_mode_advisory_key(account_mode: str = ACCOUNT_MODE) -> int:
    """Stable signed bigint key accepted by PostgreSQL advisory-lock functions."""
    if not account_mode.strip():
        raise ValueError("account_mode must be non-blank")
    digest = hashlib.sha256(account_mode.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def assert_known_writer_surface(writer_surface: str) -> None:
    if writer_surface not in MUTATION_WRITER_SURFACES:
        raise WriterSurfaceUnknown(
            f"KIS mock mutation writer is not catalogued: {writer_surface!r}"
        )


class AdvisoryLock(Protocol):
    async def try_acquire(self, key: int) -> bool: ...

    async def release(self, key: int) -> None: ...


class FileLock(Protocol):
    def try_acquire(self) -> bool: ...

    def release(self) -> None: ...


class PidFileLock:
    """Non-blocking local lock carrying a diagnostic PID, never a lock authority."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def try_acquire(self) -> bool:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - Linux/macOS CI supports it
            raise WriterSingletonUnavailable(
                "fcntl file locking is unavailable"
            ) from exc
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class PostgresAdvisoryLock:
    """Hold a dedicated PostgreSQL connection for the lifetime of the writer."""

    def __init__(self) -> None:
        self._connection: AsyncConnection | None = None

    async def try_acquire(self, key: int) -> bool:
        if self._connection is not None:
            raise RuntimeError("advisory lock instance is already acquired")
        from app.core import db

        connection = await db.engine.connect()
        try:
            result = await connection.execute(
                text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
                {"key": key},
            )
            acquired = bool(result.scalar_one())
            if not acquired:
                await connection.close()
                return False
            self._connection = connection
            return True
        except BaseException:
            await connection.close()
            raise

    async def release(self, key: int) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"),
                {"key": key},
            )
        finally:
            await connection.close()


class KISMockWriterLease:
    """Acquire both local visibility and authoritative DB singleton locks."""

    def __init__(
        self,
        *,
        writer_surface: str = "runner",
        account_mode: str = ACCOUNT_MODE,
        file_lock: FileLock | None = None,
        advisory_lock: AdvisoryLock | None = None,
    ) -> None:
        assert_known_writer_surface(writer_surface)
        self._writer_surface = writer_surface
        self._account_mode = account_mode
        self._key = account_mode_advisory_key(account_mode)
        default_path = Path(tempfile.gettempdir()) / "auto_trader_kis_mock_writer.lock"
        self._file_lock = file_lock or PidFileLock(default_path)
        self._advisory_lock = advisory_lock or PostgresAdvisoryLock()
        self._acquired = False
        self._context_token: Token[bool] | None = None

    @property
    def acquired(self) -> bool:
        return self._acquired

    async def acquire(self) -> None:
        """Acquire non-blocking locks; contention and DB failure both close the path."""
        if self._acquired:
            raise RuntimeError("writer lease is already acquired")
        try:
            file_acquired = self._file_lock.try_acquire()
        except WriterSingletonUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - file lock failure must not open writes
            raise WriterSingletonUnavailable("KIS mock PID lock unavailable") from exc
        if not file_acquired:
            raise WriterSingletonContended(
                "KIS mock writer singleton contended at local PID lock"
            )
        try:
            advisory_acquired = await self._advisory_lock.try_acquire(self._key)
        except Exception as exc:  # noqa: BLE001 - cross-process authority unavailable
            self._file_lock.release()
            raise WriterSingletonUnavailable(
                "KIS mock PostgreSQL advisory lock unavailable"
            ) from exc
        if not advisory_acquired:
            self._file_lock.release()
            raise WriterSingletonContended(
                "KIS mock writer singleton contended at PostgreSQL advisory lock"
            )
        self._acquired = True

    async def release(self) -> None:
        """Release in reverse order; an unlock error never leaves the file lock held."""
        if not self._acquired:
            return
        self._acquired = False
        try:
            await self._advisory_lock.release(self._key)
        finally:
            self._file_lock.release()

    async def __aenter__(self) -> KISMockWriterLease:
        await self.acquire()
        self._context_token = _ACTIVE_WRITER_LEASE.set(
            _WriterAuthority(
                account_mode=self._account_mode,
                advisory_keys=(self._key,),
                lease=self,
            )
        )
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        try:
            await self.release()
        finally:
            if self._context_token is not None:
                _ACTIVE_WRITER_LEASE.reset(self._context_token)
                self._context_token = None


def active_writer_authority() -> _WriterAuthority | None:
    """The authority this task actually holds, or ``None`` when nothing is held.

    Liveness is recomputed here rather than trusted from the contextvar: a
    record whose lease has already been released describes no authority at all,
    and returning it would let a nested mutation skip acquisition.
    """

    authority = _ACTIVE_WRITER_LEASE.get()
    if authority is None or not authority.live:
        return None
    return authority


def has_active_writer_lease() -> bool:
    """Whether this task already owns the advisory lease (retry-safe/reentrant)."""
    return active_writer_authority() is not None


@asynccontextmanager
async def enforce_kis_mock_mutation_writer(
    *,
    enabled: bool,
    account_mode: str = ACCOUNT_MODE,
    lease_factory: Callable[[], KISMockWriterLease] = KISMockWriterLease,
) -> AsyncIterator[None]:
    """Enforce writer cardinality for every KRX mock mutation boundary.

    The runner gate makes this dormant until KR-B2 explicitly arms the shell.
    Once armed, unscoped legacy/manual callers obtain the same one-call lease;
    runner-owned retries see the context and reuse their long-lived lease.

    ROB-1263: re-entrancy now requires a *live* authority that actually covers
    this account mode's key.  A released lease, a foreign account mode, or a
    grant that does not include the legacy key all re-acquire instead of
    silently proceeding.
    """
    required_keys = (account_mode_advisory_key(account_mode),)
    authority = active_writer_authority()
    if not enabled or (
        authority is not None
        and authority.covers(account_mode=account_mode, required_keys=required_keys)
    ):
        yield
        return
    lease = lease_factory()
    async with lease:
        token = _ACTIVE_WRITER_LEASE.set(
            _WriterAuthority(
                account_mode=account_mode,
                advisory_keys=required_keys,
                lease=lease,
            )
        )
        try:
            yield
        finally:
            _ACTIVE_WRITER_LEASE.reset(token)


AdvisoryAcquire = Awaitable[bool]


# ==========================================================================
# ROB-1263 (J3B) — KIS coordination adapter over the merged J3A primitive
# ==========================================================================
#
# Ownership boundary, restated so it cannot drift: J3A owns the PostgreSQL key
# math, the COMMIT probe, ownership reconstruction, partial-acquisition
# rollback, unlock order/count, the binary reservation, cancellation shielding,
# and its eight reason codes.  None of that is reimplemented, copied, or
# wrapped here.  J3B owns exactly two things: *which* keys the KIS lane
# supplies, and the real client host / credential namespace / account
# fingerprint check that J3A explicitly delegates to the lane.

KIS_MOCK_LANE_ID: Final[str] = "kr.kis.mock"

# The canonical J2A mock (VTS) REST netloc for this lane.  Declared here as the
# *expected* value and asserted against ``LANE_ALLOWED_HOSTS`` at use time, so a
# registry change can never be silently out-voted by a local copy.
KIS_MOCK_VTS_NETLOC: Final[str] = "openapivts.koreainvestment.com:29443"
KIS_MOCK_CREDENTIAL_NAMESPACE: Final[str] = "KIS_MOCK_*"

# J3B-owned reason literals.  They deliberately do not overlap J3A's eight
# coordination codes or J2B's lineage codes: a lane failure must stay legible
# as a *lane* failure.
KIS_MOCK_TRANSPORT_NOT_MOCK_CLIENT: Final[str] = "kis_mock_transport_not_mock_client"
KIS_MOCK_TRANSPORT_HOST_MISMATCH: Final[str] = "kis_mock_transport_host_mismatch"
KIS_MOCK_CREDENTIAL_NAMESPACE_MISMATCH: Final[str] = (
    "kis_mock_credential_namespace_mismatch"
)
KIS_MOCK_ACCOUNT_FINGERPRINT_MISMATCH: Final[str] = (
    "kis_mock_account_fingerprint_mismatch"
)
KIS_MOCK_LANE_PROFILE_MISMATCH: Final[str] = "kis_mock_lane_profile_mismatch"
KIS_MOCK_LIFECYCLE_PORTS_UNAVAILABLE: Final[str] = (
    "kis_mock_lifecycle_ports_unavailable"
)
KIS_MOCK_KEYSET_NOT_PROVEN: Final[str] = "kis_mock_keyset_not_proven"

KIS_MOCK_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        KIS_MOCK_TRANSPORT_NOT_MOCK_CLIENT,
        KIS_MOCK_TRANSPORT_HOST_MISMATCH,
        KIS_MOCK_CREDENTIAL_NAMESPACE_MISMATCH,
        KIS_MOCK_ACCOUNT_FINGERPRINT_MISMATCH,
        KIS_MOCK_LANE_PROFILE_MISMATCH,
        KIS_MOCK_LIFECYCLE_PORTS_UNAVAILABLE,
        KIS_MOCK_KEYSET_NOT_PROVEN,
    }
)

# The §83 lane-lifecycle status.  This is a *state value*, not a warning: a lane
# that cannot name a recovery owner, a restart trigger, an authoritative
# readback, its seven lane-native evidence points, an exact release condition,
# and an operator-visible blocked state stays here and is never AUTO_ENABLED.
AUTO_READY_BLOCKED_BY_LIFECYCLE: Final[str] = "AUTO_READY_BLOCKED_BY_LIFECYCLE"


class KISMockCoordinationBlocked(RuntimeError):
    """The KIS mock lane refused to coordinate; zero broker mutation happened."""

    def __init__(self, reason_code: str, *, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)


class KISMockSendBoundaryRejected(KISMockCoordinationBlocked):
    """The actual client/host/profile did not match the pinned J2A identity.

    Raised strictly before network I/O.  ``is_mock=True`` on a call signature, a
    mock-labelled registry row, a mock TR id, and admission to the VTS
    distributed gate are each insufficient on their own — the gate that matters
    is the one that reads the *actual* client object and its resolved URL.
    """


def kis_mock_legacy_advisory_key(account_mode: str = ACCOUNT_MODE) -> int:
    """The exact pre-existing account-mode key, reused rather than re-derived.

    Deleting or renaming it is forbidden until a separately approved proof that
    every deployed old process has terminated: an old build holds *only* this
    key, so dropping it would let that build write concurrently with a new
    dual-key writer.
    """

    return account_mode_advisory_key(account_mode)


def kis_mock_advisory_keyset(entry: LaneRegistryEntry) -> tuple[int, ...]:
    """The full ordered keyset this lane requires, for assertions and tests.

    The *supply* is a set of two keys; the order is not J3B's to choose.  This
    returns what the merged J3A primitive will produce from that set so a caller
    can compare it against the grant it was actually handed.
    """

    from app.services.mock_integration.coordination import (
        ordered_advisory_keyset,
        physical_account_scope_for_entry,
    )

    physical_key = physical_account_scope_for_entry(entry).advisory_key
    return ordered_advisory_keyset((physical_key, kis_mock_legacy_advisory_key()))


# --------------------------------------------------------------------------
# Secret-free actual-account fingerprint
# --------------------------------------------------------------------------
#
# J3B-owned derivation.  B-4 requires the actual client's account/profile to map
# to the exact canonical J2A ``physical_account_id``, and no upstream job
# supplies that mapping for KIS, so the lane that verifies it defines it.  It is
# domain-separated, non-reversible, and carries no credential material, so the
# resulting value is safe to store in the registry, log, and compare.
_KIS_MOCK_FINGERPRINT_V1_DOMAIN: Final[bytes] = b"kis-mock-account-v1\0"
_KIS_MOCK_FINGERPRINT_V1_PREFIX: Final[str] = "kismock:v1:"


def kis_mock_account_fingerprint(*, app_key: str, account_no: str) -> str:
    """Derive the secret-free identity a J2A ``physical_account_id`` must equal."""

    normalized_app_key = app_key.strip() if isinstance(app_key, str) else ""
    normalized_account = (
        "".join(char for char in account_no if char.isdigit())
        if isinstance(account_no, str)
        else ""
    )
    if not normalized_app_key or len(normalized_account) < 10:
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_ACCOUNT_FINGERPRINT_MISMATCH,
            detail="actual mock credentials are incomplete",
        )
    digest = hashlib.sha256(
        _KIS_MOCK_FINGERPRINT_V1_DOMAIN
        + normalized_app_key.encode("utf-8")
        + b"\0"
        + normalized_account.encode("utf-8")
    ).hexdigest()
    return _KIS_MOCK_FINGERPRINT_V1_PREFIX + digest


@dataclass(frozen=True, slots=True)
class KISMockCoordinationGrant:
    """An immutable proof of the full dual-key authority, handed to the callback.

    It replaces the boolean: a grant cannot be forged into existence by setting
    a flag, it names both keys it was acquired under, and its only capability is
    to *re-prove* ownership.  It carries no lease, connection, backend PID, owner
    token, hold id, release, or termination — the right to see, never to act.
    """

    lane_id: str
    claim_account_scope: str
    advisory_keys: tuple[int, ...]
    physical_advisory_key: int
    legacy_advisory_key: int
    credential_namespace: str
    allowed_netlocs: tuple[str, ...]
    physical_account_id: str
    _scope: CoordinationScope = field(repr=False)

    def proves_keys(self, keys: Sequence[int]) -> bool:
        """Whether every supplied key is in the acquired set."""

        held = set(self.advisory_keys)
        return bool(keys) and all(key in held for key in keys)

    async def assert_owned(self) -> None:
        """Re-prove the exact lease *and* the pinned registry entry.

        Called immediately before every HTTP mutation attempt.  The coordinator's
        single assertion before the callback is neither temporally nor
        semantically sufficient: ownership can be lost while the lane awaits
        account truth, a token refresh, or a rate limiter.
        """

        await self._scope.assert_owned()


# --------------------------------------------------------------------------
# B-4 — the actual transport / profile gate
# --------------------------------------------------------------------------


def _resolved_netloc(url: object) -> str:
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    return host if parts.port is None else f"{host}:{parts.port}"


def _require_kis_mock_lane_entry(entry: LaneRegistryEntry) -> LaneRegistryEntry:
    """Require the canonical KIS mock lane profile before anything else."""

    from app.services.mock_lane_registry import (
        LANE_ALLOWED_HOSTS,
        LANE_CREDENTIAL_NAMESPACES,
        AccountMode,
        EndpointClass,
    )

    if (
        entry.lane_id != KIS_MOCK_LANE_ID
        or entry.broker != "kis"
        or entry.account_mode is not AccountMode.MOCK
        or entry.endpoint_class is not EndpointClass.MOCK
        or entry.account_profile != "mock"
    ):
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_LANE_PROFILE_MISMATCH,
            detail=f"lane {entry.lane_id!r} is not the canonical KIS mock lane",
        )
    # The local constants are expectations, never a second source of truth.
    if LANE_ALLOWED_HOSTS[KIS_MOCK_LANE_ID] != (KIS_MOCK_VTS_NETLOC,) or (
        LANE_CREDENTIAL_NAMESPACES[KIS_MOCK_LANE_ID] != KIS_MOCK_CREDENTIAL_NAMESPACE
    ):
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_LANE_PROFILE_MISMATCH,
            detail="canonical J2A lane host/credential binding changed",
        )
    return entry


async def assert_kis_mock_send_boundary(
    *,
    client: Any,
    url: str,
    entry: LaneRegistryEntry,
    grant: KISMockCoordinationGrant,
) -> str:
    """Bind the *actual* client, URL, and account to the pinned lane identity.

    Every one of B-4's six conditions is required immediately before each real
    KIS mock mutation HTTP attempt.  Any mismatch raises before network I/O.
    """

    from app.services.mock_lane_registry import (
        LaneGuardError,
        assert_credential_namespace,
        assert_mock_only_endpoint,
    )

    _require_kis_mock_lane_entry(entry)

    # (1) the actual client object, not the declared `is_mock=True` argument.
    if getattr(client, "_is_mock_client", None) is not True:
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_TRANSPORT_NOT_MOCK_CLIENT,
            detail="actual KIS client is not a mock client",
        )

    # (2) the actual resolved request netloc, validated by the J2A guard (which
    #     also rejects the live host list) and then pinned to the VTS constant.
    try:
        netloc = assert_mock_only_endpoint(entry, url)
    except LaneGuardError as exc:
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_TRANSPORT_HOST_MISMATCH, detail=str(exc)
        ) from exc
    if netloc != KIS_MOCK_VTS_NETLOC or netloc not in grant.allowed_netlocs:
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_TRANSPORT_HOST_MISMATCH,
            detail="resolved netloc is not the pinned KIS mock host",
        )

    # (3) the actual settings view must be the mock credential/account namespace,
    #     and its own base URL must resolve to the same mock host.  A live-character
    #     client relabelled as mock fails here even if the URL was rewritten.
    view = getattr(client, "_settings", None)
    if getattr(view, "_is_mock", None) is not True:
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_CREDENTIAL_NAMESPACE_MISMATCH,
            detail="actual settings view is not the KIS mock namespace",
        )
    if _resolved_netloc(getattr(view, "kis_base_url", None)) != KIS_MOCK_VTS_NETLOC:
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_CREDENTIAL_NAMESPACE_MISMATCH,
            detail="actual settings view base URL is not the KIS mock host",
        )
    try:
        assert_credential_namespace(entry, grant.credential_namespace)
    except LaneGuardError as exc:
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_CREDENTIAL_NAMESPACE_MISMATCH, detail=str(exc)
        ) from exc

    # (4) the secret-free actual account fingerprint must be the exact canonical
    #     J2A physical_account_id the lease and claim were derived from.
    fingerprint = kis_mock_account_fingerprint(
        app_key=getattr(view, "kis_app_key", "") or "",
        account_no=getattr(view, "kis_account_no", "") or "",
    )
    if fingerprint != entry.physical_account_id or (
        fingerprint != grant.physical_account_id
    ):
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_ACCOUNT_FINGERPRINT_MISMATCH,
            detail="actual account fingerprint is not the pinned physical account",
        )

    # (5) the grant must still describe this exact lane.
    if grant.lane_id != entry.lane_id:
        raise KISMockSendBoundaryRejected(
            KIS_MOCK_LANE_PROFILE_MISMATCH, detail="grant lane does not match entry"
        )

    # (6) and the full dual-key authority must still be owned *right now*.
    await grant.assert_owned()
    return netloc


def build_kis_mock_send_boundary_hook(
    *,
    client: Any,
    path: str,
    entry: LaneRegistryEntry,
    grant: KISMockCoordinationGrant,
    chained: Callable[[], Awaitable[None]] | None = None,
) -> Callable[[], Awaitable[None]]:
    """Compose the per-send gate as the transport's ``pre_send_hook``.

    The transport fires this immediately before **every** real mutation HTTP
    attempt, re-sends included, so the URL is re-resolved and ownership
    re-proven per POST rather than once per callback.
    """

    async def _pre_send() -> None:
        await assert_kis_mock_send_boundary(
            client=client, url=client._kis_url(path), entry=entry, grant=grant
        )
        if chained is not None:
            await chained()

    return _pre_send


# --------------------------------------------------------------------------
# §83 — lane-native recovery ownership, an activation precondition
# --------------------------------------------------------------------------

KIS_MOCK_LANE_RECOVERY_CONTRACT: Final[dict[str, str]] = {
    # C3-1: exactly one owner. Not a list, not "TBD".
    "recovery_owner": "operator-run KIS mock reconciler "
    "(app/mcp_server/tooling/kis_mock_ledger.py :: KISMockLifecycleService)",
    # C3-2: what rediscovers surviving durable claims after a restart.
    "restart_trigger": "operator-invoked reconciliation over "
    "review.order_send_intents rows whose account_scope is the physical-account "
    "claim scope and whose lineage has no durably attributed broker_order_id",
    # C3-3: the authoritative broker readback.
    "readback_operation": "KIS inquire_daily_order_domestic keyed by the exact "
    "attributed ODNO (order-id-keyed, never symbol/side/qty/time proximity)",
    # C3-5: the exact release condition.
    "release_if_matches": "DurableSendClaimAdapter.release_with_terminal_evidence "
    "with TerminalClaimEvidence(lane_native_terminal_evidence, "
    "account_position_reconciled, remainder_known), or a proven "
    "authoritative_absence_proven + account_position_reconciled",
    # C3-6: what an operator sees when authoritative recovery is impossible.
    "blocked_state": AUTO_READY_BLOCKED_BY_LIFECYCLE,
}

# C3-4: the seven lane-native evidence points. Every one must have a durable
# write site before this lane may be considered for activation.
KIS_MOCK_LANE_EVIDENCE_KINDS: Final[tuple[str, ...]] = (
    "ack",
    "unknown",
    "reject",
    "expiry",
    "partial_fill",
    "cancel",
    "terminal_reconciliation",
)


@dataclass(frozen=True, slots=True)
class KISMockLanePorts:
    """The lane-native durable authorities J3A requires the lane to supply.

    J3A owns none of these by construction: aggregating lane evidence into a
    physical-account verdict, and writing typed dispatch evidence, belong to the
    lane that owns the evidence.
    """

    persistence: LineagePersistencePort | None = None
    dispatch_evidence: DispatchEvidencePort | None = None
    uncertainty_gate: AccountUncertaintyGatePort | None = None
    evidence_kinds: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KISMockLifecycleReadiness:
    """Whether this lane may even be *considered* for activation."""

    status: str
    missing: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.missing


def describe_kis_mock_lifecycle_readiness(
    ports: KISMockLanePorts,
) -> KISMockLifecycleReadiness:
    """Report the §83 lifecycle status; never an authorization to activate."""

    missing: list[str] = []
    if ports.persistence is None:
        missing.append("lineage_persistence_port")
    if ports.dispatch_evidence is None:
        missing.append("dispatch_evidence_port")
    if ports.uncertainty_gate is None:
        missing.append("account_uncertainty_gate_port")
    missing.extend(
        f"lane_evidence:{kind}"
        for kind in KIS_MOCK_LANE_EVIDENCE_KINDS
        if kind not in ports.evidence_kinds
    )
    return KISMockLifecycleReadiness(
        status="AUTO_READY_PENDING_ACTIVATION"
        if not missing
        else AUTO_READY_BLOCKED_BY_LIFECYCLE,
        missing=tuple(missing),
    )


# --------------------------------------------------------------------------
# The coordinated mutation entry point
# --------------------------------------------------------------------------


async def coordinate_kis_mock_mutation(
    *,
    envelope: LineageEnvelope,
    ports: KISMockLanePorts,
    claims: DurableSendClaimAdapter,
    connection_factory: Callable[[], Awaitable[Any]],
    mutation: Callable[[KISMockCoordinationGrant], Awaitable[MutationCallbackResult]],
    registry: Any = None,
    lineage_factory: MockLineageFactory | None = None,
) -> CoordinatedMutationResult:
    """Run one KIS mock mutation behind the merged J3A coordination order.

    J3B contributes exactly three things to that order: the legacy compatibility
    key, the lane-native durable ports, and a callback that re-proves the real
    transport identity before every POST.  Everything else — validation order,
    lease, reservation, evidence AND-gate, release — is J3A's and is not
    reimplemented here.
    """

    from app.services.mock_integration.coordination import (
        coordinate_mock_order_mutation,
        physical_account_scope_for_entry,
    )
    from app.services.mock_lane_registry import assert_lineage_registry_binding

    readiness = describe_kis_mock_lifecycle_readiness(ports)
    if not readiness.ready:
        # Fail closed before the lease, the claim, and any callback: a lane that
        # cannot durably record what happened must not make anything happen.
        raise KISMockCoordinationBlocked(
            KIS_MOCK_LIFECYCLE_PORTS_UNAVAILABLE,
            detail=f"{readiness.status}; missing={list(readiness.missing)}",
        )

    entry = _require_kis_mock_lane_entry(
        assert_lineage_registry_binding(envelope, registry)
    )
    physical_scope = physical_account_scope_for_entry(entry)
    legacy_key = kis_mock_legacy_advisory_key()
    expected_keys = kis_mock_advisory_keyset(entry)

    async def _callback(scope: CoordinationScope) -> MutationCallbackResult:
        grant = KISMockCoordinationGrant(
            lane_id=entry.lane_id,
            claim_account_scope=physical_scope.claim_account_scope,
            advisory_keys=expected_keys,
            physical_advisory_key=physical_scope.advisory_key,
            legacy_advisory_key=legacy_key,
            credential_namespace=KIS_MOCK_CREDENTIAL_NAMESPACE,
            allowed_netlocs=(KIS_MOCK_VTS_NETLOC,),
            physical_account_id=str(entry.physical_account_id),
            _scope=scope,
        )
        token = _ACTIVE_WRITER_LEASE.set(
            _WriterAuthority(
                account_mode=ACCOUNT_MODE,
                advisory_keys=expected_keys,
                grant=grant,
            )
        )
        try:
            return await mutation(grant)
        finally:
            _ACTIVE_WRITER_LEASE.reset(token)

    result = await coordinate_mock_order_mutation(
        envelope=envelope,
        persistence=ports.persistence,
        dispatch_evidence=ports.dispatch_evidence,
        uncertainty_gate=ports.uncertainty_gate,
        claims=claims,
        connection_factory=connection_factory,
        mutation=_callback,
        registry=registry,
        lineage_factory=lineage_factory,
        additional_advisory_keys=(legacy_key,),
    )
    # A single-key acquisition that still returned would mean an old
    # legacy-only writer could have run concurrently; prove the whole set.
    if tuple(result.lease_keys) != expected_keys:
        raise KISMockCoordinationBlocked(
            KIS_MOCK_KEYSET_NOT_PROVEN,
            detail="grant did not prove the full physical+legacy keyset",
        )
    return result


# --------------------------------------------------------------------------
# B-6 — follow-up authorization (capability, never a claim release)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KISMockFollowupDecision:
    """Whether a cancel/reduce may touch the broker at all."""

    operation: str
    authorized: bool
    reason_code: str | None
    releases_durable_claim: bool = field(default=False, init=False)


def authorize_kis_mock_claim_followup(
    *,
    operation: str,
    native_order_id: str | None,
    known_remainder: Decimal | None,
    lane_capability_supports_operation: bool,
    fresh_guards_passed: bool,
    require_coordination_grant: bool = False,
) -> KISMockFollowupDecision:
    """Consume J3A's capability description; never widen it.

    A soft cancel, a missing forwarding org number, an inferred quantity, and a
    local ledger status are each incapable of authorizing a follow-up — and none
    of them releases a durable claim, which stays with the exact send lineage
    until evidence-gated release.
    """

    from app.services.mock_integration.coordination import (
        ClaimFollowupRequest,
        describe_claim_followup,
    )

    authority = active_writer_authority()
    grant_held = authority is not None and authority.grant is not None
    capability = describe_claim_followup(
        ClaimFollowupRequest(
            operation=operation,
            lane_capability_supports_operation=lane_capability_supports_operation,
            attributed_native_order_id=native_order_id,
            known_remainder=known_remainder,
            fresh_guards_passed=fresh_guards_passed,
            lease_ownership_verified=grant_held or not require_coordination_grant,
        )
    )
    return KISMockFollowupDecision(
        operation=operation,
        authorized=capability.capability_present,
        reason_code=(
            None
            if capability.capability_present
            else str(capability.reason_code or "claim_followup_not_authorized")
        ),
    )
