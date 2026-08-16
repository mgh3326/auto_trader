"""ROB-1262 J3A coordination-port tests.

Test-name prefixes map to the six sections of the bound preflight artifact
``answer-codexmock-j3a-preflight-20260816.md`` (§Exact tests and mutants):

``identity_``      Identity, registry, and policy binding
``nullable_cid_``  Nullable broker-native client ID
``lock_``          Advisory lock ownership and pooling
``claim_``         Reservation, persistence, restart, and release
``cancel_``        Cancellation and injected callback
``scope_``         Scope and static safety

No test imports or instantiates a real broker client, opens a socket, or reads a
credential value; ``scope_test_file_imports_no_broker_or_network_surface``
enforces that statically over this very file.

Two things this file deliberately does **not** do:

* it does not forbid future authorized consumers from importing this module —
  the write fence is proved from the job's own ``base..HEAD`` diff, and
  ``scope_authorized_future_consumer_may_import_and_use_the_port`` pins that an
  approved J3B/J3C integration stays green;
* it does not accept "the broker order id is absent" or "the same envelope was
  written twice" as evidence of durable uncertainty — the ``claim_evidence_``
  tests read the typed :class:`DispatchEvidence` record itself.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import shutil
import subprocess
import traceback
import weakref
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services import mock_lane_registry as registry
from app.services.brokers.client_order_ids import BrokerClientIdTarget
from app.services.mock_integration import coordination
from app.services.mock_integration.coordination import (
    AUTOMATIC_CLAIM_RELEASE_AVAILABLE,
    COORDINATION_REASON_CODES,
    FENCING_NOT_BROKER_ENFORCED,
    LANE_FENCING_MATRIX,
    LEASE_TTL_SECONDS,
    NOT_BROKER_ENFORCED_FENCING_STATEMENT,
    AdvisoryLeaseGrant,
    AdvisoryLockRow,
    BackendSessionTerminationUnproven,
    BackendTerminationReceipt,
    ClaimFollowupRequest,
    CoordinationError,
    CoordinationReasonCode,
    CoordinationScope,
    DispatchEvidence,
    DispatchEvidenceKind,
    DispatchEvidencePort,
    DurableSendClaimAdapter,
    HeldCoordinationSnapshot,
    MutationCallbackResult,
    MutationCertainty,
    OrderSendIntentReservationPort,
    PostgresAdvisoryKeysetLease,
    SqlAlchemyLockAuthority,
    _held_coordination,
    _hold_view,
    _retained_authorities,
    acquire_physical_account_lease,
    authority_hold_history,
    coordinate_mock_order_mutation,
    describe_claim_followup,
    held_coordination,
    held_coordinations,
    ordered_advisory_keyset,
    physical_account_scope_for_entry,
    require_dispatch_evidence_port,
    row_proves_ownership,
    split_advisory_key,
    supports_backend_session_termination,
    unreleased_authority_holds,
)
from app.services.mock_integration.lineage import (
    CallerOwnedIdRejected,
    DecisionIntentDraft,
    ExecutionPlanDraft,
    LineageEnvelope,
    LineageReasonCode,
    MockLineageFactory,
    OrderAttemptDraft,
)
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
COORDINATION_SOURCE = (
    REPO_ROOT / "app" / "services" / "mock_integration" / "coordination.py"
)
CONTRACT_DOC = REPO_ROOT / "docs" / "contracts" / "rob-1262-coordination-port.md"
TEST_SOURCE = pathlib.Path(__file__).resolve()

# The exact J3A write fence (brief B-8) and the base this job branched from.
J3A_WRITE_FENCE: frozenset[str] = frozenset(
    {
        "app/services/mock_integration/coordination.py",
        "tests/services/mock_integration/test_coordination.py",
        "docs/contracts/rob-1262-coordination-port.md",
    }
)
J3A_FENCE_BASE_SHA = "e057941425d2ea7d35a36ebf6074a6c70eba3013"
# Operator/test scratch artifacts are explicitly allowed to change and are
# equally explicitly never committed (brief §불변).
FENCE_EXEMPT_PREFIXES: tuple[str, ...] = (".smoke-out/",)

RAW_PHYSICAL_ACCOUNT_ID = "RAW-PHYSICAL-ACCOUNT-4242-DO-NOT-LEAK"


# ---------------------------------------------------------------------------
# Canonical J2B lineage + J2A registry fixtures
# ---------------------------------------------------------------------------


def _intent_draft(**overrides: object) -> DecisionIntentDraft:
    values: dict[str, object] = {
        "policy_version": "trading-policy-v9",
        "policy_version_hash": "f" * 16,
        "decision_timestamp": datetime(2026, 8, 16, 0, 30, tzinfo=UTC),
        "market_data_cutoff": datetime(2026, 8, 16, 0, 29, tzinfo=UTC),
        "symbol": "005930",
        "side": "buy",
        "target_notional": Decimal("100000"),
        "target_notional_currency": "KRW",
        "limit_policy": {"order_type": "limit"},
        "expiry_policy": {"kind": "day"},
        "rationale": "J3A 조정 포트 픽스처",
    }
    values.update(overrides)
    return DecisionIntentDraft(**values)


def _plan_draft(**overrides: object) -> ExecutionPlanDraft:
    values: dict[str, object] = {
        "lane_id": "kr.kis.mock",
        "broker": "kis",
        "account_profile": "mock",
        "account_mode": "mock",
        "normalized_symbol": "005930",
        "quantity": Decimal("1"),
        "limit_price": Decimal("70000"),
        "quote_currency": "KRW",
        "tick_rounding": {"mode": "down"},
        "session": "regular",
        "time_in_force": "day",
        "min_order_validation": {"min_notional": "1"},
        "risk_caps": {"max_notional": "100000"},
    }
    values.update(overrides)
    return ExecutionPlanDraft(**values)


def _attempt_draft(**overrides: object) -> OrderAttemptDraft:
    values: dict[str, object] = {
        "cycle_id": "cycle-j3a-1",
        "attempt_seq": 1,
        # KIS/Kiwoom have no confirmed broker client-order-ID boundary, so J2B
        # requires the both-None pair rather than a synthesized native ID.
        "lane_prefix": None,
        "broker_client_id_target": None,
    }
    values.update(overrides)
    return OrderAttemptDraft(**values)


def _attempt_envelope(
    *,
    intent_overrides: dict[str, object] | None = None,
    plan_overrides: dict[str, object] | None = None,
    attempt_overrides: dict[str, object] | None = None,
) -> tuple[MockLineageFactory, LineageEnvelope]:
    factory = MockLineageFactory()
    intent = factory.create_decision_intent(_intent_draft(**(intent_overrides or {})))
    plan_envelope = factory.create_plan_envelope(
        intent, _plan_draft(**(plan_overrides or {}))
    )
    return factory, factory.create_attempt_envelope(
        plan_envelope, _attempt_draft(**(attempt_overrides or {}))
    )


def _by_id() -> dict[str, registry.LaneRegistryEntry]:
    return {entry.lane_id: entry for entry in registry.CANONICAL_LANE_REGISTRY}


def _fully_bound_entry(
    envelope: LineageEnvelope,
    lane_id: str,
    **overrides: object,
) -> registry.LaneRegistryEntry:
    """Test-only fully bound row; canonical rows are intentionally blocked."""

    values: dict[str, object] = {
        "lane_status": LaneStatus.AUTO_ENABLED,
        "activation_status": registry.ActivationStatus.ENABLED,
        "activation_reason": "test-only fully bound fixture",
        "policy_binding": registry.PolicyBinding(
            envelope.decision_intent.policy_version,
            envelope.decision_intent.policy_version_hash,
        ),
        "execution_mode": "test-only-bounded",
        "scheduler_owner": SchedulerOwner.MANUAL,
        "timing_owner": "test-only-timing",
        "writer": True,
        "auto_order_enabled": True,
        "max_order_notional": Decimal("100000"),
        "max_orders_per_session": 1,
        "max_open_orders": 1,
        "allowed_order_types": ("limit",),
        "allowed_time_in_force": ("day",),
        "reconcile_required": True,
        "physical_account_id": RAW_PHYSICAL_ACCOUNT_ID,
        "identity_status": "KNOWN",
        "fingerprint_evidence_ref": "test-only-fingerprint",
        "canary_binding": "test-only-bounded-canary",
        "missing_bindings": (),
    }
    values.update(overrides)
    return replace(_by_id()[lane_id], **values)


def _bound_registry(
    envelope: LineageEnvelope, lane_id: str = "kr.kis.mock", **overrides: object
) -> tuple[registry.LaneRegistryEntry, ...]:
    replacement = _fully_bound_entry(envelope, lane_id, **overrides)
    return tuple(
        replacement if entry.lane_id == replacement.lane_id else entry
        for entry in registry.CANONICAL_LANE_REGISTRY
    )


# ---------------------------------------------------------------------------
# Injected fakes — no broker transport, no socket, no credential value
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def mappings(self) -> _FakeResult:
        return self

    def one(self) -> dict[str, Any]:
        if len(self._rows) != 1:
            raise AssertionError(f"expected exactly one row, got {len(self._rows)}")
        return self._rows[0]

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        return next(iter(self.one().values()))


class FakeLockSpace:
    """A minimal stand-in for one PostgreSQL cluster's advisory-lock table."""

    def __init__(self) -> None:
        self.held: dict[int, int] = {}
        # PostgreSQL session advisory locks stack per backend. ``pg_locks`` shows
        # one row however deep the stack is, which is exactly why a single true
        # unlock is not proof that the row is gone.
        self.depth: dict[tuple[int, int], int] = {}

    def try_lock(self, key: int, pid: int) -> bool:
        owner = self.held.get(key)
        if owner is None or owner == pid:
            self.held[key] = pid
            self.depth[(key, pid)] = self.depth.get((key, pid), 0) + 1
            return True
        return False

    def unlock(self, key: int, pid: int) -> bool:
        if self.held.get(key) != pid:
            return False
        remaining = self.depth.get((key, pid), 1) - 1
        if remaining <= 0:
            self.depth.pop((key, pid), None)
            del self.held[key]
        else:
            self.depth[(key, pid)] = remaining
        return True

    def terminate(self, pid: int) -> None:
        """Backend-session termination releases every advisory lock it held."""

        for key in [key for key, owner in self.held.items() if owner == pid]:
            del self.held[key]
            self.depth.pop((key, pid), None)

    def rows(self, pid: int, database_oid: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, owner in self.held.items():
            if owner != pid:
                continue
            classid, objid = split_advisory_key(key)
            rows.append(
                {
                    "locktype": "advisory",
                    "mode": "ExclusiveLock",
                    "granted": True,
                    "database_oid": database_oid,
                    "pid": owner,
                    "objsubid": 1,
                    "classid": classid,
                    "objid": objid,
                }
            )
        return rows


class FakeLockConnection:
    """An injected stand-in for one dedicated PostgreSQL session.

    ``close`` models a *pool return*: the backend and its locks survive.  Only
    ``terminate_backend_session`` ends the session for real, which is exactly
    the distinction the lease contract depends on.
    """

    def __init__(
        self,
        space: FakeLockSpace,
        *,
        pid: int = 4242,
        database_oid: int = 99001,
        pid_after_commit: int | None = None,
        fail_sql: str | None = None,
        row_filter: Any = None,
        unlock_returns: Any = None,
        termination_receipt: Any = "default",
        termination_raises: BaseException | None = None,
        can_prove_termination: bool = True,
        close_raises: BaseException | None = None,
        fail_sql_error: BaseException | None = None,
        unlock_false_on_key: int | None = None,
        release_observer: Any = None,
        unlock_gate_on_key: int | None = None,
        unlock_gate: asyncio.Event | None = None,
        unlock_gate_started: asyncio.Event | None = None,
        unlock_raises_on_key: int | None = None,
        unlock_raises_error: BaseException | None = None,
        raise_after_lock_on_key: int | None = None,
        raise_after_lock_error: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self._space = space
        self._pid = pid
        self._database_oid = database_oid
        self._pid_after_commit = pid_after_commit
        self._fail_sql = fail_sql
        self._row_filter = row_filter
        self._unlock_returns = unlock_returns
        self._termination_receipt = termination_receipt
        self._termination_raises = termination_raises
        self._can_prove_termination = can_prove_termination
        self._close_raises = close_raises
        self._fail_sql_error = fail_sql_error
        self._unlock_false_on_key = unlock_false_on_key
        self._release_observer = release_observer
        self._unlock_gate_on_key = unlock_gate_on_key
        self._unlock_gate = unlock_gate
        self._unlock_gate_started = unlock_gate_started
        self._unlock_raises_on_key = unlock_raises_on_key
        self._unlock_raises_error = unlock_raises_error
        self._raise_after_lock_on_key = raise_after_lock_on_key
        self._raise_after_lock_error = raise_after_lock_error
        self._events = events
        self.termination_calls: list[tuple[int, str]] = []
        self.statements: list[str] = []
        self.lock_calls: list[int] = []
        self.unlock_calls: list[int] = []
        self.committed = False
        self.closed = False
        self.terminated = False

    @property
    def session_pid(self) -> int:
        if self.committed and self._pid_after_commit is not None:
            return self._pid_after_commit
        return self._pid

    def simulate_reconnect(self, *, new_pid: int) -> None:
        """Drop the old backend (releasing its locks) and land on a new one."""

        self._space.terminate(self._pid)
        self._pid = new_pid
        self._pid_after_commit = None

    def simulate_session_loss(self) -> None:
        self._space.terminate(self.session_pid)

    async def execute(self, statement: Any, parameters: Any = None, /) -> _FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        if self._fail_sql is not None and self._fail_sql in sql:
            raise self._fail_sql_error or RuntimeError(
                "simulated PostgreSQL authority failure"
            )
        params = dict(parameters or {})
        if "pg_terminate_backend" in sql:
            self.terminated = True
            self._space.terminate(self.session_pid)
            return _FakeResult([{"terminated": True}])
        if "pg_backend_pid" in sql:
            return _FakeResult(
                [
                    {
                        "backend_pid": self.session_pid,
                        "database_oid": self._database_oid,
                    }
                ]
            )
        if "pg_try_advisory_lock" in sql:
            key = int(params["key"])
            self.lock_calls.append(key)
            granted = self._space.try_lock(key, self.session_pid)
            if key == self._raise_after_lock_on_key:
                # PostgreSQL granted the key; the client-side await then fails
                # before the caller ever sees the value.
                raise self._raise_after_lock_error or RuntimeError(
                    "connection lost after the lock was granted"
                )
            return _FakeResult([{"acquired": granted}])
        if "pg_advisory_unlock" in sql:
            key = int(params["key"])
            self.unlock_calls.append(key)
            if key == self._unlock_gate_on_key:
                if self._unlock_gate_started is not None:
                    self._unlock_gate_started.set()
                if self._unlock_gate is not None:
                    await self._unlock_gate.wait()
            if key == self._unlock_raises_on_key:
                raise self._unlock_raises_error or RuntimeError("unlock interrupted")
            if self._release_observer is not None:
                self._release_observer("lease_unlock")
            if self._events is not None:
                self._events.append("lease_unlock")
            if key == self._unlock_false_on_key:
                # PostgreSQL says "you did not hold that one" mid-sequence.
                return _FakeResult([{"released": False}])
            if self._unlock_returns is None:
                released = self._space.unlock(key, self.session_pid)
            else:
                # PostgreSQL saying "you did not hold that key" must not also
                # quietly drop it from the lock space.
                released = self._unlock_returns
                if released:
                    self._space.unlock(key, self.session_pid)
            return _FakeResult([{"released": released}])
        if "pg_locks" in sql:
            rows = self._space.rows(int(params["pid"]), self._database_oid)
            if self._row_filter is not None:
                rows = self._row_filter(rows)
            return _FakeResult(rows)
        raise AssertionError(f"unexpected statement: {sql}")

    def can_prove_backend_session_termination(self) -> bool:
        return self._can_prove_termination

    async def commit(self) -> None:
        self.committed = True

    async def close(self) -> None:
        """A pool return: the backend survives, and so would its locks."""

        if self._close_raises is not None:
            raise self._close_raises
        self.closed = True
        if self._release_observer is not None:
            self._release_observer("lease_closed")
        if self._events is not None:
            self._events.append("lease_closed")

    async def terminate_backend_session(
        self, *, expected_pid: int, owner_token: str
    ) -> BackendTerminationReceipt:
        """A receipt, an unproven-termination error, or a deliberately bad receipt.

        ``close()`` is intentionally *not* called here: a pool return is never
        part of proving a backend died.
        """

        self.termination_calls.append((expected_pid, owner_token))
        if self._termination_raises is not None:
            raise self._termination_raises
        if expected_pid != self._pid:
            # A real ``pg_terminate_backend(pid)`` targets the PID it is given.
            # Asking it to kill backend 0 — or any other session — must never
            # look like this backend died.
            raise BackendSessionTerminationUnproven(
                f"expected_pid {expected_pid} is not this backend ({self._pid})"
            )
        receipt = self._termination_receipt
        if receipt == "default":
            receipt = BackendTerminationReceipt(
                backend_pid=expected_pid, owner_token=owner_token, terminated=True
            )
        if isinstance(receipt, BackendTerminationReceipt) and receipt.terminated:
            self.terminated = True
            self._space.terminate(self._pid)
        return receipt  # type: ignore[return-value]


class PooledOnlyConnection:
    """An authority that can only be *returned to a pool*, never terminated."""

    def __init__(self, space: FakeLockSpace) -> None:
        self._space = space
        self.closed = False
        self.statements: list[str] = []

    async def execute(self, statement: Any, parameters: Any = None, /) -> _FakeResult:
        self.statements.append(str(statement))
        raise AssertionError("an unusable authority must never be queried")

    async def commit(self) -> None:  # pragma: no cover - never reached
        raise AssertionError("an unusable authority must never commit")

    async def close(self) -> None:
        self.closed = True


class ConnectionFactory:
    def __init__(self, *connections: Any, fail: bool = False) -> None:
        self._connections = list(connections)
        self._fail = fail
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        if self._fail:
            raise RuntimeError("cannot open a dedicated lock-authority session")
        return self._connections.pop(0)


class RecordingPersistence:
    """A lane-owned lineage persistence port stand-in."""

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        fail_from_call: int | None = None,
        cancel_from_call: int | None = None,
        gate: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
        gate_on_call: int = 1,
    ) -> None:
        self.persisted: list[LineageEnvelope] = []
        self._gate_on_call = gate_on_call
        self.calls = 0
        self._events = events
        self._fail_from_call = fail_from_call
        self._cancel_from_call = cancel_from_call
        self._gate = gate
        self._started = started

    async def persist(self, envelope: LineageEnvelope, /) -> None:
        self.calls += 1
        if self._started is not None and self.calls == self._gate_on_call:
            self._started.set()
        if self._gate is not None and self.calls == self._gate_on_call:
            await self._gate.wait()
        if self._cancel_from_call is not None and self.calls > self._cancel_from_call:
            raise asyncio.CancelledError()
        if self._fail_from_call is not None and self.calls > self._fail_from_call:
            raise RuntimeError("simulated lane persistence backend failure")
        self.persisted.append(envelope)
        if self._events is not None:
            # B56: the pre-send write and the mandatory post-send write must be
            # separate events. Sharing one label lets `events.index("persist")`
            # silently select the pre-send write, so an assertion about "the"
            # lineage write proves nothing about the post-send one.
            self._events.append("persist_pre" if self.calls == 1 else "persist_post")


class RecordingDispatchEvidence:
    """A lane-owned dispatch-evidence port stand-in (the B1 durable unknown)."""

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        fail: bool = False,
        cancel: bool = False,
        gate: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
    ) -> None:
        self.records: list[DispatchEvidence] = []
        self.calls = 0
        self._events = events
        self._fail = fail
        self._cancel = cancel
        self._gate = gate
        self._started = started

    async def persist_dispatch_evidence(self, evidence: DispatchEvidence, /) -> None:
        self.calls += 1
        if self._events is not None:
            self._events.append("evidence_start")
        if self._started is not None:
            self._started.set()
        if self._gate is not None:
            await self._gate.wait()
        if self._cancel:
            raise asyncio.CancelledError()
        if self._fail:
            raise RuntimeError("simulated dispatch-evidence backend failure")
        self.records.append(evidence)
        if self._events is not None:
            self._events.append("evidence")

    @property
    def only(self) -> DispatchEvidence:
        assert len(self.records) == 1, self.records
        return self.records[0]


class FakeIntents:
    """Structural stand-in for ``OrderSendIntentService`` (binary claims only)."""

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        always_duplicate: bool = False,
    ) -> None:
        self.rows: dict[int, tuple[str, str, str | None]] = {}
        self.reserve_calls: list[dict[str, Any]] = []
        self.release_if_matches_calls: list[dict[str, Any]] = []
        self.has_reservations_calls = 0
        self.unrestricted_release_calls = 0
        self._next_id = 1
        self._events = events
        self._always_duplicate = always_duplicate

    async def has_reservations(self, *, account_scope: str) -> bool:
        self.has_reservations_calls += 1
        return any(scope == account_scope for scope, _, _ in self.rows.values())

    async def reserve(
        self,
        *,
        account_scope: str,
        idempotency_key: str,
        symbol: str | None = None,
        side: str | None = None,
        conflicting_key_sides: tuple[tuple[str, str], ...] = (),
    ) -> int:
        self.reserve_calls.append(
            {
                "account_scope": account_scope,
                "idempotency_key": idempotency_key,
                "symbol": symbol,
                "side": side,
            }
        )
        already = any(
            (scope, key) == (account_scope, idempotency_key)
            for scope, key, _ in self.rows.values()
        )
        if self._always_duplicate or already:
            raise DuplicateOrderIntent(f"already reserved: {account_scope}")
        row_id = self._next_id
        self._next_id += 1
        self.rows[row_id] = (account_scope, idempotency_key, side)
        if self._events is not None:
            self._events.append("reserve")
        return row_id

    async def list_reservations(self, *, account_scope: str) -> list[tuple[int, str]]:
        return [
            (row_id, key)
            for row_id, (scope, key, _) in self.rows.items()
            if scope == account_scope
        ]

    async def release_if_matches(
        self,
        *,
        account_scope: str,
        row_id: int,
        idempotency_key: str,
        side: str | None,
    ) -> int:
        self.release_if_matches_calls.append(
            {
                "account_scope": account_scope,
                "row_id": row_id,
                "idempotency_key": idempotency_key,
                "side": side,
            }
        )
        if self.rows.get(row_id) != (account_scope, idempotency_key, side):
            return 0
        del self.rows[row_id]
        return 1

    async def release(self, *, account_scope: str, idempotency_key: str) -> int:
        self.unrestricted_release_calls += 1
        raise AssertionError("J3A must never call the unrestricted release()")


class RecordingCallback:
    """The injected mutation callback; a fake, never a broker client."""

    def __init__(
        self,
        *,
        result: MutationCallbackResult | None = None,
        error: BaseException | None = None,
        events: list[str] | None = None,
        gate: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
        assert_before_send: bool = False,
    ) -> None:
        self.calls = 0
        self.scopes: list[Any] = []
        self.assert_before_send = assert_before_send
        self._result = result or MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO-0000001"
        )
        self._error = error
        self._events = events
        self._gate = gate
        self._started = started

    async def __call__(self, scope: Any) -> MutationCallbackResult:
        self.calls += 1
        self.scopes.append(scope)
        if self.assert_before_send:
            # What a real lane does immediately before each POST.
            await scope.assert_owned()
        if self._events is not None:
            self._events.append("callback_start")
        if self._started is not None:
            self._started.set()
        if self._gate is not None:
            await self._gate.wait()
        if self._events is not None:
            self._events.append("callback_end")
        if self._error is not None:
            raise self._error
        return self._result


def _coordination_kwargs(
    envelope: LineageEnvelope,
    *,
    lane_registry: tuple[registry.LaneRegistryEntry, ...],
    persistence: RecordingPersistence | None,
    evidence: RecordingDispatchEvidence | None,
    intents: FakeIntents,
    factory: ConnectionFactory,
    callback: RecordingCallback,
) -> dict[str, Any]:
    return {
        "envelope": envelope,
        "registry": lane_registry,
        "persistence": persistence,
        "dispatch_evidence": evidence,
        "claims": DurableSendClaimAdapter(intents),
        "connection_factory": factory,
        "mutation": callback,
    }


def _default_stack(events: list[str] | None = None) -> dict[str, Any]:
    space = FakeLockSpace()
    connection = FakeLockConnection(space, events=events)
    return {
        "space": space,
        "connection": connection,
        "factory": ConnectionFactory(connection),
        "persistence": RecordingPersistence(events=events),
        "evidence": RecordingDispatchEvidence(events=events),
        "intents": FakeIntents(events=events),
        "callback": RecordingCallback(events=events),
    }


async def _run(
    envelope: LineageEnvelope,
    stack: dict[str, Any],
    *,
    lane_registry: tuple[registry.LaneRegistryEntry, ...] | None = None,
) -> Any:
    return await coordinate_mock_order_mutation(
        **_coordination_kwargs(
            envelope,
            lane_registry=lane_registry or _bound_registry(envelope),
            persistence=stack["persistence"],
            evidence=stack["evidence"],
            intents=stack["intents"],
            factory=stack["factory"],
            callback=stack["callback"],
        )
    )


def _assert_no_downstream_work(stack: dict[str, Any]) -> None:
    """Lease, persistence, reservation, and callback counts are all zero."""

    assert stack["factory"].calls == 0
    assert stack["persistence"].calls == 0
    assert stack["evidence"].calls == 0
    assert stack["intents"].reserve_calls == []
    assert stack["callback"].calls == 0
    assert stack["connection"].lock_calls == []


# ===========================================================================
# §Identity, registry, and policy binding
# ===========================================================================


def test_identity_same_physical_account_across_two_lanes_derives_identical_scope():
    _, envelope = _attempt_envelope()
    kis_entry = _fully_bound_entry(envelope, "kr.kis.mock")
    kiwoom_entry = _fully_bound_entry(envelope, "kr.kiwoom.mock")
    assert kis_entry.physical_account_id == kiwoom_entry.physical_account_id

    kis_scope = physical_account_scope_for_entry(kis_entry)
    kiwoom_scope = physical_account_scope_for_entry(kiwoom_entry)

    assert kis_scope == kiwoom_scope
    assert kis_scope.claim_account_scope == kiwoom_scope.claim_account_scope
    assert kis_scope.advisory_key == kiwoom_scope.advisory_key


def test_identity_different_physical_accounts_derive_different_scopes():
    _, envelope = _attempt_envelope()
    first = physical_account_scope_for_entry(
        _fully_bound_entry(envelope, "kr.kis.mock", physical_account_id="account-a")
    )
    second = physical_account_scope_for_entry(
        _fully_bound_entry(envelope, "kr.kis.mock", physical_account_id="account-b")
    )
    assert first.claim_account_scope != second.claim_account_scope
    assert first.advisory_key != second.advisory_key


def test_identity_scope_derivation_uses_the_pinned_domain_separated_bytes():
    import hashlib

    _, envelope = _attempt_envelope()
    entry = _fully_bound_entry(envelope, "kr.kis.mock")
    digest = hashlib.sha256(
        b"mock-physical-account-v1\0" + RAW_PHYSICAL_ACCOUNT_ID.encode("utf-8")
    ).digest()

    scope = physical_account_scope_for_entry(entry)

    assert scope.claim_account_scope == "mockpa:v1:" + digest.hex()
    assert scope.advisory_key == int.from_bytes(
        digest[:8], byteorder="big", signed=True
    )


@pytest.mark.asyncio
async def test_identity_raw_physical_account_id_never_leaves_the_derivation():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    entry = _fully_bound_entry(envelope, "kr.kis.mock")
    scope = physical_account_scope_for_entry(entry)

    result = await _run(envelope, stack)

    serialized_artifacts = [
        repr(scope),
        scope.claim_account_scope,
        repr(entry),
        repr(result.claim),
        repr(result.lease_keys),
        result.claim.claim_account_scope,
        result.evidence.claim_account_scope,
        json.dumps(stack["intents"].reserve_calls),
        stack["persistence"].persisted[-1].model_dump_json(),
        str(
            CoordinationError(CoordinationReasonCode.LEASE_LOST, lane_id="kr.kis.mock")
        ),
    ]
    for artifact in serialized_artifacts:
        assert RAW_PHYSICAL_ACCOUNT_ID not in artifact


@pytest.mark.asyncio
async def test_identity_unknown_physical_identity_fails_before_any_downstream_work():
    _, envelope = _attempt_envelope()
    stack = _default_stack()

    with pytest.raises(registry.LaneGuardError) as excinfo:
        # Canonical rows ship exactly like this: null id, UNKNOWN, writer/auto false.
        await _run(envelope, stack, lane_registry=registry.CANONICAL_LANE_REGISTRY)

    assert excinfo.value.code == "lane_binding_incomplete"
    _assert_no_downstream_work(stack)


def test_identity_null_physical_identity_rejected_by_the_scope_derivation():
    _, envelope = _attempt_envelope()
    unknown = _fully_bound_entry(
        envelope,
        "kr.kis.mock",
        physical_account_id=None,
        identity_status="UNKNOWN",
        fingerprint_evidence_ref=None,
        writer=False,
        auto_order_enabled=False,
    )
    with pytest.raises(registry.LaneGuardError) as excinfo:
        physical_account_scope_for_entry(unknown)
    assert excinfo.value.code == "physical_account_identity_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_overrides", "intent_overrides", "entry_overrides", "expected"),
    [
        ({"broker": "kiwoom"}, {}, {}, "lane_broker_mismatch"),
        ({"account_profile": "paper"}, {}, {}, "lane_account_profile_mismatch"),
        ({"account_mode": "paper"}, {}, {}, "lane_account_mode_mismatch"),
        ({}, {"policy_version": "other-policy"}, {}, "lane_policy_binding_mismatch"),
        ({}, {"policy_version_hash": "a" * 16}, {}, "lane_policy_binding_mismatch"),
        (
            {},
            {},
            {
                "policy_binding": None,
                "missing_bindings": (registry.MissingBinding.POLICY,),
                "activation_status": registry.ActivationStatus.BLOCKED,
                "lane_status": LaneStatus.NOT_READY,
                "writer": False,
                "auto_order_enabled": False,
            },
            "lane_binding_incomplete",
        ),
    ],
    ids=[
        "broker_mismatch",
        "account_profile_mismatch",
        "account_mode_mismatch",
        "policy_version_mismatch",
        "policy_hash_mismatch",
        "missing_policy_binding",
    ],
)
async def test_identity_registry_mismatch_fails_with_zero_downstream_calls(
    plan_overrides: dict[str, object],
    intent_overrides: dict[str, object],
    entry_overrides: dict[str, object],
    expected: str,
):
    # The registry is bound against a matching envelope; only the *plan under
    # test* diverges, so the failure is the mismatch and nothing else.
    _, bound_envelope = _attempt_envelope()
    _, envelope = _attempt_envelope(
        intent_overrides=intent_overrides, plan_overrides=plan_overrides
    )
    stack = _default_stack()

    with pytest.raises(registry.LaneGuardError) as excinfo:
        await _run(
            envelope,
            stack,
            lane_registry=_bound_registry(bound_envelope, **entry_overrides),
        )

    assert excinfo.value.code == expected
    _assert_no_downstream_work(stack)


@pytest.mark.asyncio
async def test_identity_canonical_fully_bound_envelope_passes_without_a_broker():
    _, envelope = _attempt_envelope()
    stack = _default_stack()

    result = await _run(envelope, stack)

    assert result.certainty is MutationCertainty.DEFINITIVE
    assert stack["callback"].calls == 1
    assert stack["connection"].closed is True
    assert stack["connection"].terminated is False
    # The only I/O surfaces reached are the injected fakes.
    assert stack["factory"].calls == 1


def test_identity_j2a_policy_binding_is_consumed_not_redefined():
    assert coordination.__dict__.get("PolicyBinding") is None
    source = ast.parse(COORDINATION_SOURCE.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(source)
        if isinstance(node, ast.ClassDef | ast.FunctionDef)
    }
    assert "PolicyBinding" not in defined
    binding = registry.PolicyBinding("v", "h")
    assert (binding.policy_version, binding.policy_version_hash) == ("v", "h")
    with pytest.raises(ValueError, match="lane_binding_incomplete"):
        registry.PolicyBinding("  ", "h")


def test_identity_reason_code_vocabulary_is_the_exact_eight_values():
    assert [code.value for code in CoordinationReasonCode] == [
        "lock_authority_unavailable",
        "lease_contended",
        "lease_lost",
        "lease_event_loop_mismatch",
        "durable_claim_conflict",
        "lineage_persistence_unavailable",
        "terminal_evidence_required",
        "claim_followup_not_authorized",
    ]
    assert len(COORDINATION_REASON_CODES) == 8
    # The one shared literal is reused from J2B, never redefined.
    assert (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE.value
        == LineageReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE.value
    )
    # J2B's own enum is untouched.
    assert sorted(code.value for code in LineageReasonCode) == [
        "broker_client_id_constraint_violation",
        "broker_client_id_target_mismatch",
        "broker_order_id_conflict",
        "currency_conversion_not_authorized",
        "lane_quote_currency_mismatch",
        "lineage_persistence_unavailable",
    ]
    # The evidence vocabulary is separate and never overlaps the reason codes.
    assert COORDINATION_REASON_CODES.isdisjoint(
        {kind.value for kind in DispatchEvidenceKind}
    )


# ===========================================================================
# §Nullable broker-native client ID
# ===========================================================================


@pytest.mark.parametrize(
    ("lane_id", "broker"), [("kr.kis.mock", "kis"), ("kr.kiwoom.mock", "kiwoom")]
)
def test_nullable_cid_internal_ids_stay_non_null_while_broker_cid_is_null(
    lane_id: str, broker: str
):
    _, envelope = _attempt_envelope(
        plan_overrides={"lane_id": lane_id, "broker": broker}
    )
    attempt = envelope.order_attempt
    assert attempt is not None
    assert attempt.order_attempt_id.startswith("mock-attempt-v1:")
    assert attempt.idempotency_key.startswith("mock-idempotency-v1:")
    assert attempt.broker_client_order_id is None
    assert attempt.broker_order_id is None
    assert envelope.lane_prefix is None
    assert envelope.broker_client_id_target is None


def test_nullable_cid_persistence_roundtrip_preserves_json_null():
    _, envelope = _attempt_envelope()
    payload = json.loads(envelope.model_dump_json())
    assert payload["order_attempt"]["broker_client_order_id"] is None
    assert payload["lane_prefix"] is None
    assert payload["broker_client_id_target"] is None
    restored = LineageEnvelope.model_validate_json(envelope.model_dump_json())
    assert restored.order_attempt.broker_client_order_id is None


@pytest.mark.asyncio
async def test_nullable_cid_durable_claim_uses_internal_idempotency_key():
    _, envelope = _attempt_envelope()
    stack = _default_stack()

    result = await _run(envelope, stack)

    assert result.claim.idempotency_key == envelope.order_attempt.idempotency_key
    assert result.claim.idempotency_key.startswith("mock-idempotency-v1:")
    assert stack["intents"].reserve_calls[0]["idempotency_key"] == (
        envelope.order_attempt.idempotency_key
    )
    assert envelope.order_attempt.broker_client_order_id is None


@pytest.mark.asyncio
async def test_nullable_cid_lane_ack_lands_only_on_broker_order_id():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["callback"] = RecordingCallback(
        result=MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id=" ODNO-42 "
        )
    )

    result = await _run(envelope, stack)

    acknowledged = result.envelope.order_attempt
    assert acknowledged.broker_order_id == "ODNO-42"
    assert acknowledged.broker_client_order_id is None
    assert acknowledged.idempotency_key == envelope.order_attempt.idempotency_key
    assert stack["persistence"].persisted[-1] is result.envelope
    assert stack["evidence"].only.kind is DispatchEvidenceKind.ACKNOWLEDGED


def test_nullable_cid_mutant_copying_internal_key_into_broker_cid_is_red():
    _, envelope = _attempt_envelope()
    attempt = envelope.order_attempt
    mutant_attempt = attempt.model_copy(
        update={"broker_client_order_id": attempt.idempotency_key}
    )
    with pytest.raises(ValidationError) as excinfo:
        LineageEnvelope(
            decision_intent=envelope.decision_intent,
            execution_plan=envelope.execution_plan,
            order_attempt=mutant_attempt,
            attempt_seq=envelope.attempt_seq,
            lane_prefix=envelope.lane_prefix,
            broker_client_id_target=envelope.broker_client_id_target,
        )
    # J2B's CallerOwnedIdRejected, surfaced through pydantic's validator wrapper.
    assert "broker_client_order_id must be absent" in str(excinfo.value)
    assert isinstance(CallerOwnedIdRejected("x"), ValueError)


def test_nullable_cid_mutant_adding_kis_or_kiwoom_enum_target_is_red():
    assert {target.value for target in BrokerClientIdTarget} == {
        "toss",
        "binance_spot_demo",
        "alpaca_paper",
    }
    for absent in ("kis", "kiwoom"):
        with pytest.raises(ValueError, match=absent):
            BrokerClientIdTarget(absent)


# ===========================================================================
# §Advisory lock ownership and pooling
# ===========================================================================


def _row(**overrides: Any) -> AdvisoryLockRow:
    classid, objid = split_advisory_key(-7)
    values: dict[str, Any] = {
        "locktype": "advisory",
        "mode": "ExclusiveLock",
        "granted": True,
        "database_oid": 99001,
        "pid": 4242,
        "objsubid": 1,
        "classid": classid,
        "objid": objid,
    }
    values.update(overrides)
    return AdvisoryLockRow(**values)


def test_lock_negative_signed_key_reconstructs_the_unsigned_halves():
    key = -7
    classid, objid = split_advisory_key(key)
    assert classid == 0xFFFFFFFF
    assert objid == 0xFFFFFFF9
    assert (classid << 32 | objid) == key & ((1 << 64) - 1)
    assert row_proves_ownership(_row(), key=key, backend_pid=4242, database_oid=99001)


def test_lock_direct_signed_key_to_objid_comparison_mutant_fails():
    """The mutant: ``row.objid == key``. For a negative key it is never true."""

    key = -7
    row = _row()
    assert row.objid != key
    assert row_proves_ownership(row, key=key, backend_pid=4242, database_oid=99001)


@pytest.mark.parametrize(
    "override",
    [
        {"database_oid": 12345},
        {"pid": 9999},
        {"classid": 1},
        {"objid": 1},
        {"objsubid": 2},
        {"granted": False},
        {"mode": "ShareLock"},
        {"locktype": "transactionid"},
    ],
    ids=[
        "wrong_database_oid",
        "wrong_retained_pid",
        "wrong_classid",
        "wrong_objid",
        "wrong_objsubid",
        "not_granted",
        "non_exclusive_mode",
        "wrong_locktype",
    ],
)
def test_lock_any_single_predicate_mismatch_fails_ownership(override: dict[str, Any]):
    assert not row_proves_ownership(
        _row(**override), key=-7, backend_pid=4242, database_oid=99001
    )


@pytest.mark.asyncio
async def test_lock_reconnect_after_acquisition_cannot_pass_assert_owned():
    """G1: the single quietest failure path in this design."""

    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=4242)
    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    await lease.assert_owned(lease.grant)

    connection.simulate_reconnect(new_pid=5555)

    with pytest.raises(CoordinationError) as excinfo:
        await lease.assert_owned(lease.grant)
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST


@pytest.mark.asyncio
async def test_lock_false_green_naive_acquire_only_check_survives_a_reconnect():
    """Documented false green.

    "``pg_try_advisory_lock`` returned true, therefore we still own it" stays
    green across a reconnect. Only the PID + exact-row attestation catches it.
    """

    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=4242)
    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    connection.simulate_reconnect(new_pid=5555)

    naive_check_is_green = bool(connection.lock_calls)
    assert naive_check_is_green is True

    with pytest.raises(CoordinationError) as excinfo:
        await lease.assert_owned(lease.grant)
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST


@pytest.mark.asyncio
async def test_lock_commit_boundary_with_a_changed_backend_pid_fails():
    """G2: exactly what a transaction pooler does to a session-level lock."""

    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=4242, pid_after_commit=7777)

    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7], connection_factory=ConnectionFactory(connection)
        )

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert connection.committed is True


@pytest.mark.asyncio
async def test_lock_unattested_session_semantics_fail_closed():
    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=4242, row_filter=lambda rows: [])

    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7], connection_factory=ConnectionFactory(connection)
        )

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert connection.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("open_fails", [True, False], ids=["open_fails", "query_fails"])
async def test_lock_authority_failure_runs_no_callback_and_no_file_fallback(
    open_fails: bool,
):
    _, envelope = _attempt_envelope()
    space = FakeLockSpace()
    connection = FakeLockConnection(space, fail_sql="pg_backend_pid")
    stack = _default_stack()
    stack["connection"] = connection
    stack["factory"] = ConnectionFactory(connection, fail=open_fails)

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert stack["callback"].calls == 0
    assert stack["intents"].reserve_calls == []
    assert stack["evidence"].calls == 0
    # G5: no file lock exists anywhere in this authority.
    assert "fcntl" not in COORDINATION_SOURCE.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_lock_authority_without_backend_termination_is_rejected_at_acquire():
    """A pool return is not a termination, so a pool-only authority is unusable."""

    space = FakeLockSpace()
    pooled_only = PooledOnlyConnection(space)
    assert supports_backend_session_termination(pooled_only) is False

    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7], connection_factory=ConnectionFactory(pooled_only)
        )

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    # Rejected before a single statement, so no lock could have been taken.
    assert pooled_only.statements == []
    assert pooled_only.closed is True
    assert space.held == {}


@pytest.mark.asyncio
async def test_lock_coordination_with_a_pool_only_authority_runs_no_callback():
    _, envelope = _attempt_envelope()
    space = FakeLockSpace()
    stack = _default_stack()
    stack["factory"] = ConnectionFactory(PooledOnlyConnection(space))

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert stack["callback"].calls == 0
    assert stack["intents"].reserve_calls == []


async def _lease_forced_down_the_termination_path(
    connection: FakeLockConnection,
) -> Any:
    """Acquire a lease, then make its unlock unprovable so release must terminate."""

    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    connection._unlock_returns = False
    return lease


@pytest.mark.asyncio
async def test_lock_termination_receipt_must_bind_the_exact_pid_and_owner_token():
    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=4242)
    lease = await _lease_forced_down_the_termination_path(connection)
    holds_before = len(unreleased_authority_holds())

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    # Termination was actually invoked, bound to this exact acquisition.
    assert connection.termination_calls == [(4242, lease.grant.connection_token)]
    receipt = lease.termination_receipt
    assert receipt is not None
    assert receipt.backend_pid == lease.grant.backend_pid
    assert receipt.owner_token == lease.grant.connection_token
    assert receipt.terminated is True
    # A proven termination is an allowed outcome, and the false unlock is not
    # counted as a release.
    assert lease.released is True
    assert lease.unlocked_keys == ()
    assert lease.unreleased_authority_hold is None
    assert len(unreleased_authority_holds()) == holds_before
    assert space.held == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt", "raises"),
    [
        (
            BackendTerminationReceipt(
                backend_pid=9999, owner_token="", terminated=True
            ),
            None,
        ),
        (
            BackendTerminationReceipt(
                backend_pid=4242, owner_token="forged", terminated=True
            ),
            None,
        ),
        (
            BackendTerminationReceipt(
                backend_pid=4242, owner_token="", terminated=False
            ),
            None,
        ),
        (None, None),
        (None, PermissionError("must be a superuser to terminate")),
        (None, RuntimeError("query failed")),
        (None, BackendSessionTerminationUnproven("ambiguous driver error")),
    ],
    ids=[
        "wrong_pid",
        "wrong_owner_token",
        "terminated_false",
        "null_receipt",
        "permission_failure",
        "query_failure",
        "ambiguous_driver_error",
    ],
)
async def test_lock_unproven_termination_is_never_counted_as_a_release(
    receipt: Any, raises: BaseException | None
):
    space = FakeLockSpace()
    connection = FakeLockConnection(
        space,
        pid=4242,
        termination_receipt=receipt,
        termination_raises=raises,
    )
    lease = await _lease_forced_down_the_termination_path(connection)
    if isinstance(receipt, BackendTerminationReceipt) and receipt.owner_token == "":
        # Bind the "right token, wrong everything else" cases to the real token.
        connection._termination_receipt = replace(
            receipt, owner_token=lease.grant.connection_token
        )
    holds_before = len(unreleased_authority_holds())

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert lease.released is False
    assert lease.unlocked_keys == ()
    assert lease.termination_receipt is None
    hold = lease.unreleased_authority_hold
    assert hold is not None
    assert hold.termination_proven is False
    assert hold.key_count == len(lease.grant.keys)
    assert hold.recoverable_in_process is True
    # B26: the capability-free record carries no PID and no owner token.
    assert not hasattr(hold, "backend_pid")
    assert not hasattr(hold, "connection_token")
    assert len(unreleased_authority_holds()) == holds_before + 1
    # The active view recomputes reachability, so compare by value not identity.
    assert unreleased_authority_holds()[-1] == hold


@pytest.mark.asyncio
async def test_lock_close_alone_is_never_accepted_as_backend_termination():
    space = FakeLockSpace()
    connection = FakeLockConnection(
        space,
        pid=4242,
        termination_raises=BackendSessionTerminationUnproven("only close() worked"),
    )
    lease = await _lease_forced_down_the_termination_path(connection)

    with pytest.raises(CoordinationError):
        await lease.release(lease.grant)

    # The connection object can still be closed; that proves nothing.
    await connection.close()
    assert connection.closed is True
    assert connection.terminated is False
    assert lease.released is False
    assert lease.unreleased_authority_hold is not None


@pytest.mark.asyncio
async def test_lock_exact_grant_recovers_after_a_failed_release():
    space = FakeLockSpace()
    connection = FakeLockConnection(
        space, pid=4242, termination_raises=RuntimeError("cannot terminate")
    )
    lease = await _lease_forced_down_the_termination_path(connection)
    active_before = set(_retained_authorities())
    history_before = len(authority_hold_history())

    with pytest.raises(CoordinationError):
        await lease.release(lease.grant)
    assert lease.released is False

    # Exactly one active strong hold appears.
    new_active = set(_retained_authorities()) - active_before
    assert len(new_active) == 1
    hold_id = new_active.pop()
    assert _retained_authorities()[hold_id].connection is connection
    assert lease.unreleased_authority_hold is not None
    assert lease.unreleased_authority_hold.hold_id == hold_id

    # A stale or foreign grant keeps failing during recovery, and clears nothing.
    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(replace(lease.grant, connection_token="lockconn:forged"))
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert hold_id in _retained_authorities()

    # A second retry that still cannot prove anything keeps the hold.
    with pytest.raises(CoordinationError):
        await lease.release(lease.grant)
    assert hold_id in _retained_authorities()

    # The exact grant finally proves a full reverse unlock.
    connection._unlock_returns = None
    await lease.release(lease.grant)
    assert lease.released is True
    assert lease.unlocked_keys == (-7,)
    assert connection.closed is True

    # B20: the resolved hold is gone from BOTH active views, and the retained
    # connection is no longer reachable through them.
    assert hold_id not in _retained_authorities()
    assert hold_id not in {hold.hold_id for hold in unreleased_authority_holds()}
    assert lease.unreleased_authority_hold is None
    assert all(
        retained.connection is not connection
        for retained in _retained_authorities().values()
    )
    # History is immutable evidence and still records what happened.
    assert len(authority_hold_history()) > history_before
    assert hold_id in {hold.hold_id for hold in authority_hold_history()}


@pytest.mark.asyncio
async def test_lock_resolving_one_hold_leaves_a_concurrent_hold_untouched():
    space = FakeLockSpace()
    stuck = FakeLockConnection(
        space, pid=5150, termination_raises=RuntimeError("cannot terminate")
    )
    stuck_lease = await acquire_physical_account_lease(
        keys=[101], connection_factory=ConnectionFactory(stuck)
    )
    stuck._unlock_returns = False
    with pytest.raises(CoordinationError):
        await stuck_lease.release(stuck_lease.grant)
    stuck_hold = stuck_lease.unreleased_authority_hold
    assert stuck_hold is not None

    recovering = FakeLockConnection(
        space, pid=5151, termination_raises=RuntimeError("cannot terminate")
    )
    recovering_lease = await acquire_physical_account_lease(
        keys=[202], connection_factory=ConnectionFactory(recovering)
    )
    recovering._unlock_returns = False
    with pytest.raises(CoordinationError):
        await recovering_lease.release(recovering_lease.grant)
    recovering_hold = recovering_lease.unreleased_authority_hold
    assert recovering_hold is not None
    assert recovering_hold.hold_id != stuck_hold.hold_id

    # Resolve exactly one of them.
    recovering._unlock_returns = None
    await recovering_lease.release(recovering_lease.grant)

    active = _retained_authorities()
    assert recovering_hold.hold_id not in active
    assert stuck_hold.hold_id in active
    assert active[stuck_hold.hold_id].connection is stuck
    assert stuck_lease.released is False


def test_lock_sqlalchemy_authority_rejects_anything_but_a_dedicated_connection():
    class _NotAConnection:
        async def execute(self, statement: Any, parameters: Any = None, /) -> None:
            return None

    for rejected in (_NotAConnection(), object(), None):
        with pytest.raises(CoordinationError) as excinfo:
            SqlAlchemyLockAuthority(rejected)  # type: ignore[arg-type]
        assert excinfo.value.reason_code is (
            CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
        )


# --- B17 · B14 · B15 · B18 --------------------------------------------------


def _dedicated_connection_double(results: list[Any]) -> Any:
    """A ``MagicMock`` that really is an ``AsyncConnection`` for isinstance."""

    connection = MagicMock(spec=AsyncConnection)
    connection.execute = AsyncMock(side_effect=list(results))
    connection.close = AsyncMock()
    connection.commit = AsyncMock()
    assert isinstance(connection, AsyncConnection)
    return connection


@pytest.mark.asyncio
async def test_lock_authority_that_cannot_prove_termination_is_rejected_before_sql():
    """B17: a callable named ``terminate_backend_session`` is not a capability."""

    space = FakeLockSpace()
    unprovable = FakeLockConnection(space, can_prove_termination=False)
    assert callable(unprovable.terminate_backend_session)
    assert supports_backend_session_termination(unprovable) is False

    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7], connection_factory=ConnectionFactory(unprovable)
        )

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    # Not one statement was issued and not one key was requested.
    assert unprovable.statements == []
    assert unprovable.lock_calls == []
    assert space.held == {}


def test_lock_sqlalchemy_authority_without_an_observer_cannot_prove_termination():
    """B17 regression: ``observer_factory=None`` must not pass the gate."""

    connection = _dedicated_connection_double([])
    authority = SqlAlchemyLockAuthority(connection)
    assert authority.can_prove_backend_session_termination() is False
    assert supports_backend_session_termination(authority) is False
    with_observer = SqlAlchemyLockAuthority(
        _dedicated_connection_double([]), observer_factory=AsyncMock()
    )
    assert supports_backend_session_termination(with_observer) is True


@pytest.mark.asyncio
async def test_lock_sqlalchemy_authority_with_no_observer_acquires_nothing():
    connection = _dedicated_connection_double([])
    authority = SqlAlchemyLockAuthority(connection)

    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7], connection_factory=ConnectionFactory(authority)
        )

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert connection.execute.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observer_results",
    [
        [_FakeResult([{"terminated": False}]), _FakeResult([{"alive": 1}])],
        [_FakeResult([{"terminated": True}]), _FakeResult([{"alive": 1}])],
        RuntimeError("permission denied for function pg_terminate_backend"),
    ],
    ids=[
        "terminate_false_and_still_alive",
        "backend_still_alive",
        "permission_failure",
    ],
)
async def test_lock_adapter_never_closes_the_owner_when_termination_is_unproven(
    observer_results: Any,
):
    """B14: the owner connection still holds the lock — it must not be returned."""

    owner = _dedicated_connection_double([])
    if isinstance(observer_results, BaseException):
        observer = MagicMock(spec=AsyncConnection)
        observer.execute = AsyncMock(side_effect=observer_results)
        observer.close = AsyncMock()
    else:
        observer = _dedicated_connection_double(observer_results)

    async def observer_factory() -> Any:
        return observer

    authority = SqlAlchemyLockAuthority(owner, observer_factory=observer_factory)

    with pytest.raises(Exception) as excinfo:
        await authority.terminate_backend_session(
            expected_pid=4242, owner_token="lockconn:abc"
        )
    assert not isinstance(excinfo.value, BackendTerminationReceipt)

    # The observer is disposable; the owner is not.
    assert observer.close.await_count == 1
    assert owner.close.await_count == 0


@pytest.mark.asyncio
async def test_lock_adapter_closes_the_owner_only_after_a_proven_termination():
    owner = _dedicated_connection_double([])
    observer = _dedicated_connection_double(
        [_FakeResult([{"terminated": True}]), _FakeResult([{"alive": 0}])]
    )

    async def observer_factory() -> Any:
        return observer

    authority = SqlAlchemyLockAuthority(owner, observer_factory=observer_factory)
    receipt = await authority.terminate_backend_session(
        expected_pid=4242, owner_token="lockconn:abc"
    )

    assert receipt == BackendTerminationReceipt(
        backend_pid=4242, owner_token="lockconn:abc", terminated=True
    )
    assert owner.close.await_count == 1
    assert observer.close.await_count == 1
    # The PID actually sent to PostgreSQL is the one we were asked to terminate.
    sent = observer.execute.await_args_list[0].args
    assert sent[1] == {"pid": 4242}


@pytest.mark.asyncio
async def test_lock_partial_rollback_terminates_with_the_real_backend_pid_and_token():
    """B18: a rollback must not ask PostgreSQL to terminate backend 0."""

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=2002)
    await acquire_physical_account_lease(
        keys=[11], connection_factory=ConnectionFactory(blocker)
    )
    # This session's unlocks are unprovable, so rollback must fall back to
    # terminating *this* backend.
    connection = FakeLockConnection(space, pid=3003, unlock_returns=False)

    with pytest.raises(CoordinationError):
        await acquire_physical_account_lease(
            keys=[-7, 5, 11], connection_factory=ConnectionFactory(connection)
        )

    assert len(connection.termination_calls) == 1
    sent_pid, sent_token = connection.termination_calls[0]
    assert sent_pid == 3003
    assert sent_pid != 0
    assert sent_token.startswith("lockconn:")
    assert connection.terminated is True


@pytest.mark.asyncio
async def test_lock_unprovable_rollback_retains_the_real_connection_strongly():
    """B15: metadata alone would let GC or a pool return drop the authority."""

    import gc

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=2002)
    await acquire_physical_account_lease(
        keys=[11], connection_factory=ConnectionFactory(blocker)
    )
    connection = FakeLockConnection(
        space,
        pid=3003,
        unlock_returns=False,
        termination_raises=RuntimeError("cannot terminate"),
    )
    retained_before = set(_retained_authorities())

    with pytest.raises(CoordinationError):
        await acquire_physical_account_lease(
            keys=[-7, 5, 11], connection_factory=ConnectionFactory(connection)
        )

    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    hold_id = new_holds.pop()
    retained = _retained_authorities()[hold_id]

    # The real connection object is reachable, with the real identity.
    assert retained.connection is connection
    assert retained.grant.backend_pid == 3003
    assert retained.grant.database_oid == connection._database_oid
    assert retained.grant.keys == (-7, 5, 11)
    assert retained.grant.event_loop is asyncio.get_running_loop()

    # It survives a collection, and was never closed or pool-returned.
    gc.collect()
    assert _retained_authorities()[hold_id].connection is connection
    assert connection.closed is False
    assert connection.terminated is False

    # The keys the fake still reports as held stay contended for a successor.
    assert set(space.held) >= {-7, 5}
    successor = FakeLockConnection(space, pid=4004)
    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7], connection_factory=ConnectionFactory(successor)
        )
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED

    # A stale/foreign hold id recovers nothing.
    assert _retained_authorities().get("hold:forged") is None


@pytest.mark.asyncio
async def test_lock_two_contenders_for_one_physical_key_yield_exactly_one_owner():
    space = FakeLockSpace()
    first_connection = FakeLockConnection(space, pid=1001)
    second_connection = FakeLockConnection(space, pid=1002)

    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(first_connection)
    )
    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7], connection_factory=ConnectionFactory(second_connection)
        )

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED
    assert second_connection.closed is True
    await lease.assert_owned(lease.grant)
    await lease.release(lease.grant)


@pytest.mark.asyncio
async def test_lock_two_distinct_physical_keys_do_not_contend():
    space = FakeLockSpace()
    first = await acquire_physical_account_lease(
        keys=[-7],
        connection_factory=ConnectionFactory(FakeLockConnection(space, pid=1)),
    )
    second = await acquire_physical_account_lease(
        keys=[11],
        connection_factory=ConnectionFactory(FakeLockConnection(space, pid=2)),
    )
    await first.assert_owned(first.grant)
    await second.assert_owned(second.grant)
    await first.release(first.grant)
    await second.release(second.grant)


def test_lock_keyset_is_deduplicated_and_globally_sorted():
    assert ordered_advisory_keyset([5, -7, 5, 11, -7]) == (-7, 5, 11)


@pytest.mark.asyncio
async def test_lock_duplicate_key_is_acquired_once_and_unlocked_once():
    space = FakeLockSpace()
    connection = FakeLockConnection(space)
    lease = await acquire_physical_account_lease(
        keys=[-7, -7, -7], connection_factory=ConnectionFactory(connection)
    )
    assert lease.grant.keys == (-7,)
    assert connection.lock_calls == [-7]

    await lease.release(lease.grant)
    assert connection.unlock_calls == [-7]
    assert lease.unlocked_keys == (-7,)


@pytest.mark.asyncio
async def test_lock_partial_multi_key_acquisition_rolls_back_every_acquired_key():
    space = FakeLockSpace()
    blocker_connection = FakeLockConnection(space, pid=2002)
    await acquire_physical_account_lease(
        keys=[11], connection_factory=ConnectionFactory(blocker_connection)
    )
    connection = FakeLockConnection(space, pid=3003)

    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7, 5, 11], connection_factory=ConnectionFactory(connection)
        )

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED
    assert connection.lock_calls == [-7, 5, 11]
    # Reverse order, exactly the keys that were actually acquired.
    assert connection.unlock_calls == [5, -7]
    assert connection.closed is True
    assert space.held == {11: 2002}


@pytest.mark.asyncio
async def test_lock_unprovable_rollback_terminates_the_backend_session():
    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=2002)
    await acquire_physical_account_lease(
        keys=[11], connection_factory=ConnectionFactory(blocker)
    )
    # This session's unlocks always report false: the lock state is unknown.
    connection = FakeLockConnection(space, pid=3003, unlock_returns=False)

    with pytest.raises(CoordinationError):
        await acquire_physical_account_lease(
            keys=[-7, 5, 11], connection_factory=ConnectionFactory(connection)
        )

    assert connection.terminated is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("connection lost after the lock was granted"),
        asyncio.CancelledError(),
    ],
    ids=["exception_after_grant", "cancellation_after_grant"],
)
async def test_lock_in_flight_acquisition_never_takes_the_confirmed_rollback_path(
    error: BaseException,
):
    """B19: the key may already be held server-side even though we never saw it."""

    space = FakeLockSpace()
    connection = FakeLockConnection(
        space,
        pid=3003,
        raise_after_lock_on_key=5,
        raise_after_lock_error=error,
        # Termination cannot be proven, so the authority must be retained.
        termination_raises=RuntimeError("cannot terminate"),
    )
    retained_before = set(_retained_authorities())

    with pytest.raises(BaseException) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7, 5], connection_factory=ConnectionFactory(connection)
        )
    assert isinstance(excinfo.value, type(error))

    # PostgreSQL really did grant the ambiguous key.
    assert connection.lock_calls == [-7, 5]
    assert space.held.get(5) == 3003
    # The confirmed-only unlock path was NOT taken, and the owner was not
    # closed or pool-returned.
    assert connection.unlock_calls == []
    assert connection.closed is False
    assert connection.terminated is False
    # Exact-PID termination was attempted and could not be proven.
    assert connection.termination_calls == [(3003, connection.termination_calls[0][1])]
    assert connection.termination_calls[0][0] == 3003

    # The real connection is retained strongly under an opaque hold.
    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    retained = _retained_authorities()[new_holds.pop()]
    assert retained.connection is connection
    assert retained.grant.backend_pid == 3003
    assert retained.grant.keys == (-7, 5)

    # A successor still contends on the ambiguous key.
    successor = FakeLockConnection(space, pid=4004)
    with pytest.raises(CoordinationError) as contended:
        await acquire_physical_account_lease(
            keys=[5], connection_factory=ConnectionFactory(successor)
        )
    assert contended.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED


@pytest.mark.asyncio
async def test_lock_in_flight_uncertainty_is_resolved_by_a_proven_termination():
    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=3003, raise_after_lock_on_key=5)
    retained_before = set(_retained_authorities())

    with pytest.raises(RuntimeError):
        await acquire_physical_account_lease(
            keys=[-7, 5], connection_factory=ConnectionFactory(connection)
        )

    # A positive exact-PID receipt resolves the ambiguity without a hold.
    assert connection.termination_calls[0][0] == 3003
    assert connection.terminated is True
    assert connection.unlock_calls == []
    assert space.held == {}
    assert set(_retained_authorities()) == retained_before


@pytest.mark.asyncio
async def test_lock_explicit_false_stays_a_known_not_acquired_path():
    """Over-fail-closed is also a defect: a definite ``False`` is not ambiguity."""

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=2002)
    await acquire_physical_account_lease(
        keys=[11], connection_factory=ConnectionFactory(blocker)
    )
    connection = FakeLockConnection(space, pid=3003)
    retained_before = set(_retained_authorities())

    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[-7, 5, 11], connection_factory=ConnectionFactory(connection)
        )

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED
    # The ordinary confirmed rollback ran: reverse unlock, then a plain close,
    # with no termination and no hold invented.
    assert connection.unlock_calls == [5, -7]
    assert connection.closed is True
    assert connection.terminated is False
    assert connection.termination_calls == []
    assert set(_retained_authorities()) == retained_before


@pytest.mark.asyncio
async def test_lock_multi_key_release_is_reverse_order_with_exact_unlock_count():
    space = FakeLockSpace()
    connection = FakeLockConnection(space)
    lease = await acquire_physical_account_lease(
        keys=[11, -7, 5], connection_factory=ConnectionFactory(connection)
    )
    assert lease.grant.keys == (-7, 5, 11)

    await lease.release(lease.grant)

    assert connection.unlock_calls == [11, 5, -7]
    assert lease.unlocked_keys == (11, 5, -7)
    assert connection.closed is True
    assert connection.terminated is False

    # A second release must not unlock anything a second time.
    await lease.release(lease.grant)
    assert connection.unlock_calls == [11, 5, -7]
    assert lease.unlocked_keys == (11, 5, -7)


@pytest.mark.asyncio
async def test_lock_release_reattests_ownership_before_unlocking():
    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=4242)
    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    connection.simulate_reconnect(new_pid=5555)

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    # No unlock was even attempted.
    assert connection.unlock_calls == []
    assert lease.unlocked_keys == ()
    # Termination was requested against the *retained* PID — never the new one —
    # and this reconnected session cannot prove it, so the authority is held
    # rather than reported as released.
    assert connection.termination_calls == [(4242, lease.grant.connection_token)]
    assert connection.terminated is False
    assert lease.released is False
    hold = lease.unreleased_authority_hold
    assert hold is not None
    assert hold.termination_proven is False
    assert hold.key_count == 1
    assert _retained_authorities()[hold.hold_id].connection is connection


@pytest.mark.asyncio
async def test_lock_false_unlock_return_is_never_recorded_as_a_release():
    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=4242)
    lease = await acquire_physical_account_lease(
        keys=[-7, 5], connection_factory=ConnectionFactory(connection)
    )
    # PostgreSQL now reports "you did not hold that key".
    connection._unlock_returns = False

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert connection.unlock_calls == [5]
    # The unproven key is NOT counted as released.
    assert lease.unlocked_keys == ()
    assert connection.terminated is True


@pytest.mark.asyncio
async def test_lock_stale_grant_or_token_cannot_drive_assert_or_release():
    space = FakeLockSpace()
    connection = FakeLockConnection(space)
    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    stale_token = replace(lease.grant, connection_token="lockconn:forged")
    copied_grant = replace(lease.grant)

    for foreign in (stale_token, copied_grant):
        with pytest.raises(CoordinationError) as excinfo:
            await lease.assert_owned(foreign)
        assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
        with pytest.raises(CoordinationError) as excinfo:
            await lease.release(foreign)
        assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST

    assert connection.unlock_calls == []
    assert lease.released is False
    await lease.release(lease.grant)


@pytest.mark.asyncio
async def test_lock_event_loop_mismatch_fails_before_any_db_or_callback_work():
    """G6: a lease is bound to the loop that acquired it."""

    space = FakeLockSpace()
    connection = FakeLockConnection(space)
    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    statements_before = len(connection.statements)

    foreign_loop = asyncio.new_event_loop()
    try:
        foreign_grant = replace(lease.grant, event_loop=foreign_loop)
        cross_loop_lease = PostgresAdvisoryKeysetLease(
            connection=connection, grant=foreign_grant
        )
        with pytest.raises(CoordinationError) as excinfo:
            await cross_loop_lease.assert_owned(foreign_grant)
        assert excinfo.value.reason_code is (
            CoordinationReasonCode.LEASE_EVENT_LOOP_MISMATCH
        )
        with pytest.raises(CoordinationError) as excinfo:
            await cross_loop_lease.release(foreign_grant)
        assert excinfo.value.reason_code is (
            CoordinationReasonCode.LEASE_EVENT_LOOP_MISMATCH
        )
    finally:
        foreign_loop.close()

    assert len(connection.statements) == statements_before
    assert connection.unlock_calls == []
    await lease.release(lease.grant)


@pytest.mark.asyncio
async def test_lock_grant_is_immutable_and_carries_the_full_acquisition_identity():
    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=4242, database_oid=99001)
    lease = await acquire_physical_account_lease(
        keys=[5, -7], connection_factory=ConnectionFactory(connection)
    )

    grant = lease.grant
    assert grant.keys == (-7, 5)
    assert grant.backend_pid == 4242
    assert grant.database_oid == 99001
    assert grant.connection_token.startswith("lockconn:")
    assert grant.event_loop is asyncio.get_running_loop()
    assert isinstance(grant, AdvisoryLeaseGrant)
    with pytest.raises(AttributeError):
        grant.backend_pid = 1  # type: ignore[misc]
    await lease.release(grant)


@pytest.mark.asyncio
async def test_lock_idle_lease_is_rechecked_at_use_time_not_by_a_heartbeat():
    """G4: no heartbeat exists, so ownership is proven immediately before use."""

    space = FakeLockSpace()
    connection = FakeLockConnection(space)
    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    connection.simulate_session_loss()

    with pytest.raises(CoordinationError) as excinfo:
        await lease.assert_owned(lease.grant)
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST


# ===========================================================================
# §Reservation, persistence, restart, and release
# ===========================================================================


@pytest.mark.asyncio
async def test_claim_persist_precedes_reserve_precedes_callback_precedes_release():
    _, envelope = _attempt_envelope()
    events: list[str] = []
    stack = _default_stack(events)

    await _run(envelope, stack)

    assert events == [
        "persist_pre",
        "reserve",
        "callback_start",
        "callback_end",
        "persist_post",
        "evidence_start",
        "evidence",
        "lease_unlock",
        "lease_closed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["persistence", "evidence"])
async def test_claim_absent_durable_port_blocks_lease_reserve_and_callback(
    missing: str,
):
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack[missing] = None

    with pytest.raises(CoordinationError) as excinfo:
        await coordinate_mock_order_mutation(
            **_coordination_kwargs(
                envelope,
                lane_registry=_bound_registry(envelope),
                persistence=stack["persistence"],
                evidence=stack["evidence"],
                intents=stack["intents"],
                factory=stack["factory"],
                callback=stack["callback"],
            )
        )

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert str(excinfo.value).startswith("lineage_persistence_unavailable")
    assert stack["intents"].reserve_calls == []
    assert stack["callback"].calls == 0
    assert stack["factory"].calls == 0


@pytest.mark.asyncio
async def test_claim_failing_persistence_port_blocks_reserve_and_callback():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["persistence"] = RecordingPersistence(fail_from_call=0)

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert stack["intents"].reserve_calls == []
    assert stack["callback"].calls == 0
    assert stack["factory"].calls == 0


@pytest.mark.asyncio
async def test_claim_duplicate_binary_claim_maps_to_durable_claim_conflict():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    # has_reservations stays False; the conflict surfaces at INSERT time.
    stack["intents"] = FakeIntents(always_duplicate=True)

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert excinfo.value.reason_code is CoordinationReasonCode.DURABLE_CLAIM_CONFLICT
    assert stack["callback"].calls == 0
    assert stack["evidence"].calls == 0
    assert stack["connection"].closed is True


@pytest.mark.asyncio
async def test_claim_account_wide_unresolved_reservation_blocks_a_different_order():
    _, first_envelope = _attempt_envelope()
    _, second_envelope = _attempt_envelope(
        attempt_overrides={"cycle_id": "cycle-j3a-2"}
    )
    assert (
        first_envelope.order_attempt.idempotency_key
        != second_envelope.order_attempt.idempotency_key
    )
    space = FakeLockSpace()
    intents = FakeIntents()
    first_stack = _default_stack()
    first_stack["space"] = space
    first_stack["connection"] = FakeLockConnection(space, pid=1)
    first_stack["factory"] = ConnectionFactory(first_stack["connection"])
    first_stack["intents"] = intents

    await _run(first_envelope, first_stack)
    assert len(intents.rows) == 1

    second_stack = _default_stack()
    second_stack["connection"] = FakeLockConnection(space, pid=2)
    second_stack["factory"] = ConnectionFactory(second_stack["connection"])
    second_stack["intents"] = intents

    with pytest.raises(CoordinationError) as excinfo:
        await _run(second_envelope, second_stack)

    assert excinfo.value.reason_code is CoordinationReasonCode.DURABLE_CLAIM_CONFLICT
    assert second_stack["callback"].calls == 0
    assert len(intents.rows) == 1


@pytest.mark.asyncio
async def test_claim_survives_a_crash_and_blocks_the_next_lease_owner():
    _, envelope = _attempt_envelope()
    _, next_envelope = _attempt_envelope(attempt_overrides={"cycle_id": "cycle-j3a-9"})
    space = FakeLockSpace()
    intents = FakeIntents()
    stack = _default_stack()
    stack["connection"] = FakeLockConnection(space, pid=1)
    stack["factory"] = ConnectionFactory(stack["connection"])
    stack["intents"] = intents
    stack["callback"] = RecordingCallback(
        error=RuntimeError("process crashed mid-send")
    )

    with pytest.raises(RuntimeError, match="process crashed mid-send"):
        await _run(envelope, stack)

    # The ephemeral lease is gone; the durable reservation is not.
    assert stack["connection"].closed is True
    assert space.held == {}
    assert len(intents.rows) == 1

    successor = _default_stack()
    successor["connection"] = FakeLockConnection(space, pid=2)
    successor["factory"] = ConnectionFactory(successor["connection"])
    successor["intents"] = intents

    with pytest.raises(CoordinationError) as excinfo:
        await _run(next_envelope, successor)

    assert excinfo.value.reason_code is CoordinationReasonCode.DURABLE_CLAIM_CONFLICT
    assert successor["callback"].calls == 0
    assert len(intents.rows) == 1


@pytest.mark.asyncio
async def test_claim_terminal_evidence_plus_reconcile_permits_exact_release():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    result = await _run(envelope, stack)
    adapter = DurableSendClaimAdapter(stack["intents"])

    deleted = await adapter.release_with_terminal_evidence(
        result.claim,
        coordination.TerminalClaimEvidence(
            lane_native_terminal_evidence=True,
            account_position_reconciled=True,
            remainder_known=True,
        ),
    )

    assert deleted == 1
    assert stack["intents"].release_if_matches_calls == [
        {
            "account_scope": result.claim.claim_account_scope,
            "row_id": result.claim.row_id,
            "idempotency_key": result.claim.idempotency_key,
            "side": "buy",
        }
    ]
    assert stack["intents"].rows == {}
    assert stack["intents"].unrestricted_release_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [
        coordination.TerminalClaimEvidence(),
        coordination.TerminalClaimEvidence(lane_native_terminal_evidence=True),
        coordination.TerminalClaimEvidence(
            lane_native_terminal_evidence=True, account_position_reconciled=True
        ),
        coordination.TerminalClaimEvidence(
            account_position_reconciled=True, remainder_known=True
        ),
        coordination.TerminalClaimEvidence(authoritative_absence_proven=True),
    ],
    ids=[
        "no_evidence",
        "terminal_without_reconcile",
        "partial_with_unknown_remainder",
        "unknown_or_anomaly_without_terminal",
        "claimed_absence_without_reconcile",
    ],
)
async def test_claim_insufficient_evidence_retains_the_claim_and_the_account_block(
    evidence: coordination.TerminalClaimEvidence,
):
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    result = await _run(envelope, stack)
    adapter = DurableSendClaimAdapter(stack["intents"])

    with pytest.raises(CoordinationError) as excinfo:
        await adapter.release_with_terminal_evidence(result.claim, evidence)

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.TERMINAL_EVIDENCE_REQUIRED
    )
    # The evidence-less delete never reached the database at all.
    assert stack["intents"].release_if_matches_calls == []
    assert len(stack["intents"].rows) == 1
    assert await adapter.account_has_unresolved_claim(result.scope) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [
        coordination.TerminalClaimEvidence(
            lane_native_terminal_evidence="false",
            account_position_reconciled="no",
            remainder_known="0",
        ),
        coordination.TerminalClaimEvidence(
            lane_native_terminal_evidence=1,
            account_position_reconciled=1,
            remainder_known=1,
        ),
        coordination.TerminalClaimEvidence(
            lane_native_terminal_evidence=object(),
            account_position_reconciled=object(),
            remainder_known=object(),
        ),
        coordination.TerminalClaimEvidence(
            authoritative_absence_proven="yes", account_position_reconciled="yes"
        ),
    ],
    ids=["strings", "ints", "objects", "truthy_absence"],
)
async def test_claim_truthy_non_boolean_evidence_cannot_delete_the_claim(
    evidence: Any,
):
    """B36: ``"false"`` is truthy, and so is ``"0"``.

    The durable claim is the entire account block. A mistyped field must not be
    able to authorize deleting it, and the adapter must not simply trust an
    overridable authorization property.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    result = await _run(envelope, stack)
    adapter = DurableSendClaimAdapter(stack["intents"])

    with pytest.raises(CoordinationError) as excinfo:
        await adapter.release_with_terminal_evidence(result.claim, evidence)

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.TERMINAL_EVIDENCE_REQUIRED
    )
    assert stack["intents"].release_if_matches_calls == []
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_evidence_subclass_cannot_override_its_way_to_a_release():
    """B36: the adapter recomputes the predicate rather than trusting it."""

    class ForgedEvidence(coordination.TerminalClaimEvidence):
        @property
        def authorizes_release(self) -> bool:
            return True

        @property
        def exact_booleans(self) -> bool:
            return True

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    result = await _run(envelope, stack)
    adapter = DurableSendClaimAdapter(stack["intents"])

    with pytest.raises(CoordinationError) as excinfo:
        await adapter.release_with_terminal_evidence(result.claim, ForgedEvidence())

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.TERMINAL_EVIDENCE_REQUIRED
    )
    assert stack["intents"].release_if_matches_calls == []
    assert len(stack["intents"].rows) == 1


def test_claim_no_ttl_no_clock_and_no_automatic_release_exists():
    """The TTL / local-clock-expiry / automatic-cleanup mutants are structural.

    Adding any of them requires a clock, and this module has none.
    """

    assert AUTOMATIC_CLAIM_RELEASE_AVAILABLE is False
    assert LEASE_TTL_SECONDS is None

    tree = ast.parse(COORDINATION_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"time", "datetime", "sched", "croniter"})

    forbidden_asyncio_attrs = {"sleep", "wait_for", "timeout", "TimeoutError"}
    used_asyncio_attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "asyncio"
    }
    assert used_asyncio_attrs.isdisjoint(forbidden_asyncio_attrs)


def test_claim_reservation_port_excludes_the_unrestricted_release():
    """``OrderSendIntentService.release`` exists but is not part of the port."""

    assert hasattr(OrderSendIntentService, "release")
    assert not hasattr(OrderSendIntentReservationPort, "release")
    assert isinstance(FakeIntents(), OrderSendIntentReservationPort)

    tree = ast.parse(COORDINATION_SOURCE.read_text(encoding="utf-8"))
    port_release_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "release"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_intents"
    ]
    assert port_release_calls == []


def test_claim_followup_capability_never_authorizes_a_broker_mutation():
    complete = ClaimFollowupRequest(
        operation="cancel",
        lane_capability_supports_operation=True,
        attributed_native_order_id="ODNO-42",
        known_remainder=Decimal("1"),
        fresh_guards_passed=True,
        lease_ownership_verified=True,
    )
    capability = describe_claim_followup(complete)
    assert capability.capability_present is True
    assert capability.reason_code is None
    # Even a complete capability authorizes nothing here.
    assert capability.authorizes_broker_mutation is False
    assert capability.releases_durable_claim is False
    with pytest.raises(TypeError):
        coordination.ClaimFollowupCapability(
            operation="cancel",
            capability_present=True,
            reason_code=None,
            authorizes_broker_mutation=True,
        )


@pytest.mark.parametrize(
    "override",
    [
        {"operation": "increase"},
        {"operation": "new_order"},
        {"lane_capability_supports_operation": False},
        {"attributed_native_order_id": None},
        {"attributed_native_order_id": "  "},
        {"known_remainder": None},
        {"fresh_guards_passed": False},
        {"lease_ownership_verified": False},
    ],
)
def test_claim_followup_missing_element_is_not_authorized(override: dict[str, Any]):
    request = ClaimFollowupRequest(
        **{
            "operation": "reduce",
            "lane_capability_supports_operation": True,
            "attributed_native_order_id": "ODNO-42",
            "known_remainder": Decimal("1"),
            "fresh_guards_passed": True,
            "lease_ownership_verified": True,
            **override,
        }
    )
    capability = describe_claim_followup(request)
    assert capability.capability_present is False
    assert capability.reason_code is (
        CoordinationReasonCode.CLAIM_FOLLOWUP_NOT_AUTHORIZED
    )


# ===========================================================================
# §Durable dispatch evidence — the typed unknown fact (B1/B5)
# ===========================================================================


def _assert_correlated(evidence: DispatchEvidence, envelope: LineageEnvelope) -> None:
    """Evidence must be self-describing at rest, not only in this process."""

    assert evidence.decision_intent_id == envelope.decision_intent.decision_intent_id
    assert evidence.execution_plan_id == envelope.execution_plan.execution_plan_id
    assert evidence.order_attempt_id == envelope.order_attempt.order_attempt_id
    assert evidence.cycle_id == envelope.order_attempt.cycle_id
    assert evidence.attempt_seq == envelope.attempt_seq
    assert evidence.idempotency_key == envelope.order_attempt.idempotency_key
    assert evidence.claim_account_scope.startswith("mockpa:v1:")
    assert evidence.claim_row_id > 0


def test_claim_evidence_port_is_required_and_fail_closed():
    with pytest.raises(CoordinationError) as excinfo:
        require_dispatch_evidence_port(None)
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    with pytest.raises(CoordinationError):
        require_dispatch_evidence_port(object())  # type: ignore[arg-type]
    port = RecordingDispatchEvidence()
    assert isinstance(port, DispatchEvidencePort)
    assert require_dispatch_evidence_port(port) is port


@pytest.mark.asyncio
async def test_claim_evidence_records_a_typed_acknowledgement():
    _, envelope = _attempt_envelope()
    stack = _default_stack()

    result = await _run(envelope, stack)

    evidence = stack["evidence"].only
    assert evidence is result.evidence
    assert evidence.kind is DispatchEvidenceKind.ACKNOWLEDGED
    assert evidence.certainty is MutationCertainty.DEFINITIVE
    assert evidence.broker_order_id == "ODNO-0000001"
    assert evidence.callback_failed is False
    assert evidence.outer_cancellation_requested is False
    _assert_correlated(evidence, envelope)


@pytest.mark.asyncio
async def test_claim_evidence_records_a_typed_lane_reported_uncertainty():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["callback"] = RecordingCallback(
        result=MutationCallbackResult(certainty=MutationCertainty.UNCERTAIN)
    )

    result = await _run(envelope, stack)

    evidence = stack["evidence"].only
    # The unknown is a typed fact, not an inference from a missing field.
    assert evidence.kind is DispatchEvidenceKind.LANE_REPORTED_UNCERTAIN
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    assert evidence.broker_order_id is None
    assert evidence.callback_failed is False
    _assert_correlated(evidence, envelope)
    assert result.certainty is MutationCertainty.UNCERTAIN
    # Uncertainty keeps the account blocked.
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_evidence_records_a_typed_callback_failure():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["callback"] = RecordingCallback(error=RuntimeError("transport exploded"))

    with pytest.raises(RuntimeError, match="transport exploded"):
        await _run(envelope, stack)

    evidence = stack["evidence"].only
    assert evidence.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    assert evidence.callback_failed is True
    assert evidence.broker_order_id is None
    _assert_correlated(evidence, envelope)
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_evidence_records_a_typed_outer_cancellation():
    _, envelope = _attempt_envelope()
    started = asyncio.Event()
    gate = asyncio.Event()
    stack = _default_stack()
    stack["callback"] = RecordingCallback(
        gate=gate,
        started=started,
        result=MutationCallbackResult(certainty=MutationCertainty.UNCERTAIN),
    )

    task = asyncio.ensure_future(_run(envelope, stack))
    await started.wait()
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    evidence = stack["evidence"].only
    assert evidence.outer_cancellation_requested is True
    assert evidence.kind is DispatchEvidenceKind.LANE_REPORTED_UNCERTAIN
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    _assert_correlated(evidence, envelope)
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_evidence_a_second_identical_envelope_write_is_not_evidence():
    """Adversarial: the pre-B1 shape would have passed on these two facts alone.

    Writing the same envelope twice and finding ``broker_order_id is None``
    proves nothing — neither carries the uncertainty. The typed record must.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["callback"] = RecordingCallback(
        result=MutationCallbackResult(certainty=MutationCertainty.UNCERTAIN)
    )

    await _run(envelope, stack)

    persisted = stack["persistence"].persisted
    # The old false-green signals are still present...
    assert len(persisted) == 2
    assert persisted[0] == persisted[1]
    assert persisted[-1].order_attempt.broker_order_id is None
    # ...and they are demonstrably not evidence of anything.
    envelope_payload = json.loads(persisted[-1].model_dump_json())
    assert "uncertain" not in json.dumps(envelope_payload).lower()
    # Only the typed record carries the fact.
    assert stack["evidence"].only.kind is DispatchEvidenceKind.LANE_REPORTED_UNCERTAIN


def _post_send_write_failure_stacks() -> dict[str, dict[str, Any]]:
    """The four independent ways the post-send AND gate can fail to close."""

    return {
        "lineage_write_failure": {
            "persistence": RecordingPersistence(fail_from_call=1)
        },
        "lineage_write_cancellation": {
            "persistence": RecordingPersistence(cancel_from_call=1)
        },
        "evidence_write_failure": {"evidence": RecordingDispatchEvidence(fail=True)},
        "evidence_write_cancellation": {
            "evidence": RecordingDispatchEvidence(cancel=True)
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(_post_send_write_failure_stacks()))
async def test_claim_evidence_post_send_write_failure_holds_all_authority(
    scenario: str,
):
    """Neither post-send durable write landing means nothing may be given up."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack.update(_post_send_write_failure_stacks()[scenario])
    held_before = set(held_coordinations())

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )

    # 1. the lease was never surrendered, by any route
    connection = stack["connection"]
    assert connection.unlock_calls == []
    assert connection.closed is False
    assert connection.terminated is False

    # 2. the durable claim stays unresolved
    assert len(stack["intents"].rows) == 1
    assert stack["intents"].release_if_matches_calls == []

    # 3. the strong handle is still reachable after the raise — a GC pass or a
    #    pool return cannot have collected it
    new_holds = set(held_coordinations()) - held_before
    assert len(new_holds) == 1
    hold_id = new_holds.pop()
    assert excinfo.value.hold_id == hold_id
    held = _held_coordination(hold_id)
    assert held is not None
    assert held.grant is held.lease.grant
    assert held.claim.claim_account_scope.startswith("mockpa:v1:")
    import gc

    gc.collect()
    # The private strong reference survives; the public view stays capability-free.
    assert _held_coordination(hold_id) is held
    public = held_coordination(hold_id)
    assert isinstance(public, HeldCoordinationSnapshot)
    assert public.durable_evidence_written is False

    # 4. the authority hold is auditable and never says it was released
    authority_hold = held.lease.unreleased_authority_hold
    assert authority_hold is not None
    assert authority_hold.durable_evidence_written is False
    assert held.lease.released is False

    # 5. a successor cannot mutate this physical account
    _, successor_envelope = _attempt_envelope(
        attempt_overrides={"cycle_id": "successor"}
    )
    successor = _default_stack()
    successor["intents"] = stack["intents"]
    successor["connection"] = FakeLockConnection(stack["space"], pid=9)
    successor["factory"] = ConnectionFactory(successor["connection"])
    with pytest.raises(CoordinationError) as successor_error:
        await _run(successor_envelope, successor)
    assert successor_error.value.reason_code in {
        CoordinationReasonCode.LEASE_CONTENDED,
        CoordinationReasonCode.DURABLE_CLAIM_CONFLICT,
    }
    assert successor["callback"].calls == 0

    # The hold is intentionally left in place: dropping it here would be the
    # very takeover this contract forbids.


def _windowed_stack(
    window: str, events: list[str]
) -> tuple[dict[str, Any], asyncio.Event, asyncio.Event]:
    """A stack that blocks inside exactly one stage of the coordinated flow."""

    gate = asyncio.Event()
    started = asyncio.Event()
    stack = _default_stack(events)
    if window in {"callback", "cancellation_retained_wait"}:
        stack["callback"] = RecordingCallback(events=events, gate=gate, started=started)
    elif window == "lineage_persist":
        # The *post-send* lineage write, i.e. after the broker callback returned.
        stack["persistence"] = RecordingPersistence(
            events=events, gate=gate, started=started, gate_on_call=2
        )
    elif window == "dispatch_evidence":
        stack["evidence"] = RecordingDispatchEvidence(
            events=events, gate=gate, started=started
        )
    else:  # pragma: no cover - guarded by the parametrization
        raise AssertionError(window)
    return stack, gate, started


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "window",
    ["callback", "lineage_persist", "dispatch_evidence", "cancellation_retained_wait"],
)
async def test_claim_public_release_is_refused_for_the_whole_held_lifetime(
    window: str,
):
    """B22: the dangerous window is *before* anything is known, not after.

    A durable-false hold only exists once a write has already failed. These are
    the stages where nothing has failed yet and nothing has succeeded either —
    the broker callback may be mid-flight — and a generic release there is the
    most damaging one available.
    """

    _, envelope = _attempt_envelope()
    events: list[str] = []
    stack, gate, started = _windowed_stack(window, events)
    held_before = set(held_coordinations())

    task = asyncio.ensure_future(_run(envelope, stack))
    await started.wait()
    if window == "cancellation_retained_wait":
        task.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert task.done() is False

    hold_id = (set(held_coordinations()) - held_before).pop()
    held = _held_coordination(hold_id)
    assert held is not None
    # No durable-false hold exists yet: this is precisely the B21 blind spot.
    assert held.lease.unreleased_authority_hold is None
    assert held.lease.coordination_sealed is True
    connection = stack["connection"]

    for _ in range(2):
        with pytest.raises(CoordinationError) as excinfo:
            await held.lease.release(held.grant)
        assert excinfo.value.reason_code is (
            CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
        )
        assert excinfo.value.hold_id == hold_id
    # A forged capability object is no better than none.
    with pytest.raises(CoordinationError):
        await held.lease._release_with_capability(held.grant, object())
    # And neither is a fresh wrapper around the same authority: no durable-false
    # hold exists yet in this window, so only the held-coordination lockout can
    # catch it.
    with pytest.raises(CoordinationError) as clone_error:
        PostgresAdvisoryKeysetLease(connection=connection, grant=held.grant)
    assert clone_error.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )

    assert connection.unlock_calls == []
    assert connection.closed is False
    assert connection.terminated is False
    assert held.lease.released is False
    assert len(stack["intents"].rows) == 1

    gate.set()
    if window == "cancellation_retained_wait":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task

    # Only the coordination-owned path released it, and only at the end.
    assert connection.unlock_calls == [held.grant.keys[0]]
    assert connection.closed is True
    assert events.index("evidence") < events.index("lease_unlock")
    assert hold_id not in held_coordinations()


@pytest.mark.asyncio
async def test_claim_only_the_coordination_owned_path_releases_a_sealed_lease():
    _, envelope = _attempt_envelope()
    events: list[str] = []
    stack = _default_stack(events)
    held_before = set(held_coordinations())

    result = await _run(envelope, stack)

    # Both durable writes landed, so the internal path released and unregistered.
    assert set(held_coordinations()) == held_before
    assert stack["connection"].unlock_calls == [result.lease_keys[0]]
    assert stack["connection"].closed is True
    assert events.index("evidence") < events.index("lease_unlock")


@pytest.mark.asyncio
async def test_lock_unsealed_standalone_lease_still_honours_the_durable_false_hold():
    """B21 in isolation: no seal involved, so its own guard is what must fire.

    Keeping this separate from the sealed path is what stops the B22 seal from
    masking a regression in the B21 guard.
    """

    space = FakeLockSpace()
    connection = FakeLockConnection(space)
    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    assert lease.coordination_sealed is False
    hold = lease._retain_authority(
        reason_code=CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
        durable_evidence_written=False,
    )

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert excinfo.value.hold_id == hold.hold_id
    assert connection.unlock_calls == []
    assert connection.closed is False
    assert lease.released is False


@pytest.mark.asyncio
async def test_claim_two_held_leases_do_not_share_release_authority():
    _, first_envelope = _attempt_envelope()
    _, second_envelope = _attempt_envelope(attempt_overrides={"cycle_id": "second"})
    gate = asyncio.Event()
    started = asyncio.Event()

    first_stack = _default_stack()
    first_stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(first_envelope, first_stack)
    stuck_id = (set(held_coordinations()) - held_before).pop()

    second_stack = _default_stack()
    second_stack["callback"] = RecordingCallback(gate=gate, started=started)
    second_task = asyncio.ensure_future(_run(second_envelope, second_stack))
    await started.wait()
    in_flight_id = (set(held_coordinations()) - held_before - {stuck_id}).pop()

    stuck = _held_coordination(stuck_id)
    in_flight = _held_coordination(in_flight_id)
    assert stuck is not None and in_flight is not None
    assert stuck_id != in_flight_id

    # Neither handle can release the other, nor itself.
    for holder, other in ((stuck, in_flight), (in_flight, stuck)):
        with pytest.raises(CoordinationError):
            await holder.lease.release(holder.grant)
        with pytest.raises(CoordinationError):
            await other.lease.release(holder.grant)

    assert first_stack["connection"].unlock_calls == []
    assert second_stack["connection"].unlock_calls == []

    gate.set()
    await second_task

    # The in-flight one completed and unregistered; the stuck one is untouched.
    assert in_flight_id not in held_coordinations()
    assert stuck_id in held_coordinations()
    assert first_stack["connection"].unlock_calls == []
    assert first_stack["connection"].closed is False


@pytest.mark.asyncio
async def test_claim_durable_false_hold_cannot_be_released_via_the_public_handle():
    """B21: public introspection may see a stuck lease; it may not release it."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())

    with pytest.raises(CoordinationError) as coordinate_error:
        await _run(envelope, stack)

    hold_id = (set(held_coordinations()) - held_before).pop()
    # One stuck authority carries one opaque id across both surfaces.
    assert coordinate_error.value.hold_id == hold_id
    held = _held_coordination(hold_id)
    assert held is not None
    authority_hold = held.lease.unreleased_authority_hold
    assert authority_hold is not None
    assert authority_hold.hold_id == hold_id
    assert authority_hold.durable_evidence_written is False

    connection = stack["connection"]
    for attempt in range(3):
        # Repeated attempts through the *public* handle and grant.
        with pytest.raises(CoordinationError) as excinfo:
            await held.lease.release(held.grant)
        assert excinfo.value.reason_code is (
            CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
        ), attempt
        assert excinfo.value.hold_id == hold_id
        # A stale/foreign grant is refused for its own reason and also changes
        # nothing.
        with pytest.raises(CoordinationError) as foreign:
            await held.lease.release(
                replace(held.grant, connection_token="lockconn:forged")
            )
        assert foreign.value.reason_code is CoordinationReasonCode.LEASE_LOST

    assert connection.unlock_calls == []
    assert connection.closed is False
    assert connection.terminated is False
    assert held.lease.released is False
    assert hold_id in held_coordinations()
    assert hold_id in {h.hold_id for h in unreleased_authority_holds()}
    # The durable claim — the account block — is untouched throughout.
    assert len(stack["intents"].rows) == 1
    assert stack["intents"].release_if_matches_calls == []


@pytest.mark.asyncio
async def test_claim_durable_false_hold_does_not_disturb_a_durable_true_hold():
    """The two hold kinds are independent: handling one never moves the other."""

    # A durable-TRUE hold: the release itself failed, so B20 recovery applies.
    space = FakeLockSpace()
    recoverable_conn = FakeLockConnection(
        space, pid=6001, termination_raises=RuntimeError("cannot terminate")
    )
    recoverable = await acquire_physical_account_lease(
        keys=[909], connection_factory=ConnectionFactory(recoverable_conn)
    )
    recoverable_conn._unlock_returns = False
    with pytest.raises(CoordinationError):
        await recoverable.release(recoverable.grant)
    true_hold = recoverable.unreleased_authority_hold
    assert true_hold is not None
    assert true_hold.durable_evidence_written is True

    # A durable-FALSE hold from a failed post-send evidence write.
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(envelope, stack)
    false_hold_id = (set(held_coordinations()) - held_before).pop()

    # Recovering the durable-true one succeeds and clears only itself.
    recoverable_conn._unlock_returns = None
    await recoverable.release(recoverable.grant)
    assert recoverable.released is True
    assert true_hold.hold_id not in _retained_authorities()

    # The durable-false hold is completely unaffected and still unreleasable.
    assert false_hold_id in held_coordinations()
    stuck = _held_coordination(false_hold_id)
    assert stuck is not None
    with pytest.raises(CoordinationError) as excinfo:
        await stuck.lease.release(stuck.grant)
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False


@pytest.mark.asyncio
async def test_claim_evidence_lease_release_count_is_zero_until_evidence_lands():
    _, envelope = _attempt_envelope()
    events: list[str] = []
    started = asyncio.Event()
    gate = asyncio.Event()
    stack = _default_stack(events)
    stack["evidence"] = RecordingDispatchEvidence(
        events=events, gate=gate, started=started
    )

    task = asyncio.ensure_future(_run(envelope, stack))
    await started.wait()
    for _ in range(10):
        await asyncio.sleep(0)

    # Evidence is mid-write: the lease has not been surrendered.
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert task.done() is False

    gate.set()
    await task

    assert events.index("evidence") < events.index("lease_unlock")
    assert events.index("evidence") < events.index("lease_closed")


@pytest.mark.asyncio
async def test_claim_evidence_cancellation_during_evidence_write_holds_the_lease():
    _, envelope = _attempt_envelope()
    events: list[str] = []
    started = asyncio.Event()
    gate = asyncio.Event()
    stack = _default_stack(events)
    stack["evidence"] = RecordingDispatchEvidence(
        events=events, gate=gate, started=started
    )

    task = asyncio.ensure_future(_run(envelope, stack))
    await started.wait()
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)

    # Cancelling the caller must not abandon an in-flight evidence write.
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert stack["evidence"].records == []

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(stack["evidence"].records) == 1
    assert events.index("evidence") < events.index("lease_unlock")
    assert len(stack["intents"].rows) == 1


# ===========================================================================
# §Pre-I/O cancellation, ACK attachment anomaly, and held-handle lifetime
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_during_initial_persistence_never_reaches_lease_or_broker():
    """A caller cancelled before the send must not acquire, reserve, or send."""

    _, envelope = _attempt_envelope()
    gate = asyncio.Event()
    started = asyncio.Event()
    stack = _default_stack()
    stack["persistence"] = RecordingPersistence(gate=gate, started=started)
    held_before = set(held_coordinations())

    task = asyncio.ensure_future(_run(envelope, stack))
    await started.wait()
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)

    assert task.done() is False
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The mandatory persist completed...
    assert stack["persistence"].calls == 1
    assert stack["persistence"].persisted == [envelope]
    # ...and absolutely nothing after it ran.
    assert stack["factory"].calls == 0
    assert stack["connection"].lock_calls == []
    assert stack["intents"].reserve_calls == []
    assert stack["callback"].calls == 0
    assert stack["evidence"].calls == 0
    assert set(held_coordinations()) == held_before


class _FailingAckFactory(MockLineageFactory):
    """A J2B factory whose acknowledgement step rejects the broker's id."""

    def acknowledge_order_attempt(
        self, envelope: LineageEnvelope, broker_order_id: str
    ) -> LineageEnvelope:
        raise ValueError("broker_order_id must be strip-normalized")


@pytest.mark.asyncio
async def test_claim_evidence_ack_attachment_failure_is_durable_before_release():
    _, envelope = _attempt_envelope()
    events: list[str] = []
    stack = _default_stack(events)

    with pytest.raises(ValueError, match="strip-normalized"):
        await coordinate_mock_order_mutation(
            **_coordination_kwargs(
                envelope,
                lane_registry=_bound_registry(envelope),
                persistence=stack["persistence"],
                evidence=stack["evidence"],
                intents=stack["intents"],
                factory=stack["factory"],
                callback=stack["callback"],
            ),
            lineage_factory=_FailingAckFactory(),
        )

    evidence = stack["evidence"].only
    assert evidence.kind is DispatchEvidenceKind.ACK_ATTACHMENT_FAILED
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    assert evidence.ack_attachment_failed is True
    # No unusable broker id is smuggled through as an acknowledgement.
    assert evidence.broker_order_id is None
    assert evidence.envelope is envelope
    assert evidence.envelope.order_attempt.broker_order_id is None
    _assert_correlated(evidence, envelope)
    # Durable first, cleanup second — and the original J2B error still surfaces.
    assert events.index("evidence") < events.index("lease_unlock")
    assert events.index("evidence") < events.index("lease_closed")
    assert stack["connection"].closed is True
    # The claim is not released by an unusable acknowledgement.
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_evidence_ack_failure_with_broken_durability_keeps_the_hold():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())

    with pytest.raises(CoordinationError) as excinfo:
        await coordinate_mock_order_mutation(
            **_coordination_kwargs(
                envelope,
                lane_registry=_bound_registry(envelope),
                persistence=stack["persistence"],
                evidence=stack["evidence"],
                intents=stack["intents"],
                factory=stack["factory"],
                callback=stack["callback"],
            ),
            lineage_factory=_FailingAckFactory(),
        )

    # The persistence failure wins and chains the cause; the hold survives.
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert len(set(held_coordinations()) - held_before) == 1


@pytest.mark.asyncio
async def test_claim_held_handle_is_dropped_only_after_a_proven_release():
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    held_before = set(held_coordinations())

    result = await _run(envelope, stack)

    assert set(held_coordinations()) == held_before
    assert stack["connection"].unlock_calls == [result.lease_keys[0]]
    assert stack["connection"].closed is True


@pytest.mark.asyncio
async def test_lock_cancellation_mid_release_still_reaches_a_safe_outcome():
    """Cancelling the releaser must not abandon a half-finished unlock."""

    space = FakeLockSpace()
    connection = FakeLockConnection(space)
    lease = await acquire_physical_account_lease(
        keys=[11, -7], connection_factory=ConnectionFactory(connection)
    )

    async def release_and_wait() -> None:
        await lease.release(lease.grant)

    task = asyncio.ensure_future(release_and_wait())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The retained release ran to a definite, safe result anyway.
    assert lease.released is True
    assert lease.unlocked_keys == (11, -7)
    assert connection.closed is True
    assert space.held == {}


def test_scope_no_ttl_retry_queue_scheduler_or_schema_was_introduced():
    """The absences are structural, not a matter of vocabulary.

    A TTL or an expiry needs a clock, a retry queue or scheduler needs a
    scheduling import, and a schema change needs DDL. None of the three has any
    machinery here — the words themselves appear only in prohibitions.
    """

    tree = ast.parse(COORDINATION_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(
        {
            "alembic",
            "apscheduler",
            "celery",
            "croniter",
            "datetime",
            "prefect",
            "sched",
            "taskiq",
            "time",
        }
    )

    assert LEASE_TTL_SECONDS is None
    assert AUTOMATIC_CLAIM_RELEASE_AVAILABLE is False

    upper_source = COORDINATION_SOURCE.read_text(encoding="utf-8").upper()
    for ddl in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "INSERT INTO"):
        assert ddl not in upper_source, ddl

    # The evidence vocabulary never leaks into the signed reason-code set.
    assert len(COORDINATION_REASON_CODES) == 8
    assert COORDINATION_REASON_CODES.isdisjoint(
        {kind.value for kind in DispatchEvidenceKind}
    )


# ===========================================================================
# §Cancellation and injected callback
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_during_write_holds_lease_and_claim_until_inner_completes():
    _, envelope = _attempt_envelope()
    events: list[str] = []
    gate = asyncio.Event()
    started = asyncio.Event()
    stack = _default_stack(events)
    stack["callback"] = RecordingCallback(
        events=events,
        gate=gate,
        started=started,
        result=MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO-INFLIGHT"
        ),
    )

    task = asyncio.ensure_future(_run(envelope, stack))
    await started.wait()

    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)

    # The socket write may still be in flight: nothing has been given up.
    assert task.done() is False
    assert "callback_end" not in events
    assert stack["connection"].closed is False
    assert stack["connection"].unlock_calls == []
    assert len(stack["intents"].rows) == 1

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events.index("callback_end") < events.index("evidence")
    assert events.index("evidence") < events.index("lease_closed")
    evidence = stack["evidence"].only
    assert evidence.kind is DispatchEvidenceKind.ACKNOWLEDGED
    assert evidence.broker_order_id == "ODNO-INFLIGHT"
    assert evidence.outer_cancellation_requested is True
    # The durable claim is never released by cancellation.
    assert len(stack["intents"].rows) == 1
    assert stack["intents"].release_if_matches_calls == []


@pytest.mark.asyncio
async def test_cancel_bare_shield_finally_release_mutant_is_red():
    """The mutant releases in ``finally`` while the inner task still writes."""

    events: list[str] = []
    gate = asyncio.Event()
    started = asyncio.Event()
    space = FakeLockSpace()
    connection = FakeLockConnection(space, events=events)
    lease = await acquire_physical_account_lease(
        keys=[-7], connection_factory=ConnectionFactory(connection)
    )
    callback = RecordingCallback(events=events, gate=gate, started=started)

    async def _noop_assert() -> None:
        return None

    async def bare_shield_mutant() -> None:
        inner = asyncio.ensure_future(callback(CoordinationScope(_noop_assert)))
        try:
            await asyncio.shield(inner)
        finally:
            await lease.release(lease.grant)

    mutant_task = asyncio.ensure_future(bare_shield_mutant())
    await started.wait()
    mutant_task.cancel()
    for _ in range(200):
        if "lease_closed" in events:
            break
        await asyncio.sleep(0)

    # The invariant asserted by the cancellation test above is already violated:
    # the lease is gone while the inner task is still running.
    assert "lease_closed" in events
    assert "callback_end" not in events

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await mutant_task
    assert events.index("lease_closed") < events.index("callback_end")


# ===========================================================================
# §Authority ownership — B23-B32
# ===========================================================================


def _drop_exception_roots(error: BaseException) -> None:
    """Clear frames along an exception chain before a GC lifetime assertion.

    r35 U1: a raised exception keeps its traceback, and those frames keep their
    locals — including the connection a rollback helper was handed. That is a
    *non-module* strong root, so a `weakref() is not None` assertion downstream
    would pass for the wrong reason and could not detect the module dropping its
    own root. Clearing the chain first makes the assertion mean what it says.
    """

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__traceback__ is not None:
            traceback.clear_frames(current.__traceback__)
        current = current.__cause__ or current.__context__


def _active_holds() -> dict[str, Any]:
    """Module-private active-hold map, read-only, for exact-record assertions."""

    return coordination._ACTIVE_AUTHORITY_HOLDS


def _capture_rollback_identities(monkeypatch: Any, captured: dict[str, Any]) -> None:
    """Weakref the exact connection and grant *entering* the rollback.

    r29 W1/W4: a witness taken afterwards from ``_retained_authorities`` only
    re-reads the object the module already holds, so it cannot show that the
    module is the *only* root. These references are bound before any retention
    happens and own nothing.
    """

    real = coordination._rollback_partial_acquisition

    async def spy(
        connection: Any, acquired: Any, grant: Any, *, in_flight: Any = ()
    ) -> Any:
        captured["connection"] = weakref.ref(connection)
        captured["grant"] = weakref.ref(grant)
        return await real(connection, acquired, grant, in_flight=in_flight)

    monkeypatch.setattr(coordination, "_rollback_partial_acquisition", spy)


async def _durable_true_hold(space: FakeLockSpace, *, pid: int, key: int) -> Any:
    """A lease whose *release* failed, so B20 recovery semantics apply."""

    connection = FakeLockConnection(
        space, pid=pid, termination_raises=RuntimeError("cannot terminate")
    )
    lease = await acquire_physical_account_lease(
        keys=[key], connection_factory=ConnectionFactory(connection)
    )
    connection._unlock_returns = False
    with pytest.raises(CoordinationError):
        await lease.release(lease.grant)
    hold = lease.unreleased_authority_hold
    assert hold is not None and hold.durable_evidence_written is True
    connection._unlock_returns = None
    return lease, connection, hold


@pytest.mark.asyncio
async def test_claim_proven_termination_on_an_error_path_clears_the_held_handle():
    """B28: a provably released authority must stop being reported as held.

    The coordination-owned release fails its unlock, falls back to a positive
    termination receipt, and then re-raises. The handle must still be gone: the
    authority really is released, and saying otherwise is a lie that outlives
    the process's memory of why.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    connection = stack["connection"]
    held_before = set(held_coordinations())

    async def flip_then_report(scope: Any) -> MutationCallbackResult:
        # By release time the unlocks will be unprovable, forcing termination.
        connection._unlock_returns = False
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO-TERM"
        )

    stack["callback"] = flip_then_report

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    # Termination was proven against the exact backend...
    assert connection.terminated is True
    assert connection.termination_calls[0][0] == connection._pid
    # ...so nothing may still be reported as held, even though the call raised.
    assert set(held_coordinations()) == held_before
    assert all(
        retained.connection is not connection
        for retained in _retained_authorities().values()
    )
    # Both durable writes had landed, so the evidence exists; only the claim
    # stays, because releasing that needs terminal evidence, not a lock.
    assert stack["evidence"].calls == 1
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_lock_proven_unlock_resolves_the_hold_even_when_close_fails():
    """B23: a pool-return error cannot un-prove an unlock that already happened."""

    space = FakeLockSpace()
    lease, connection, hold = await _durable_true_hold(space, pid=8001, key=811)
    other_lease, _, other_hold = await _durable_true_hold(space, pid=8002, key=822)
    assert hold.hold_id in _retained_authorities()

    connection._close_raises = RuntimeError("pool return failed")
    with pytest.raises(RuntimeError, match="pool return failed"):
        await lease.release(lease.grant)

    # The unlock proof stands, and the hold is honestly resolved.
    assert connection.unlock_calls[-1] == 811
    assert lease.unlocked_keys == (811,)
    assert lease.released is True
    assert hold.hold_id not in _retained_authorities()
    assert hold.hold_id not in {h.hold_id for h in unreleased_authority_holds()}
    # History keeps the evidence; the other hold is untouched.
    assert hold.hold_id in {h.hold_id for h in authority_hold_history()}
    assert other_hold.hold_id in _retained_authorities()
    assert other_lease.released is False


@pytest.mark.asyncio
async def test_lock_unproven_unlock_keeps_the_hold_regardless_of_close():
    space = FakeLockSpace()
    lease, connection, hold = await _durable_true_hold(space, pid=8010, key=830)
    connection._unlock_returns = False
    connection._close_raises = RuntimeError("pool return failed")

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert hold.hold_id in _retained_authorities()
    assert lease.released is False


@pytest.mark.asyncio
async def test_lock_hold_id_collision_never_overwrites_a_foreign_entry(monkeypatch):
    """B24: forced collision — allocation must not adopt somebody else's id."""

    space = FakeLockSpace()
    _, victim_connection, victim_hold = await _durable_true_hold(
        space, pid=8101, key=841
    )
    victim_owner = _retained_authorities()[victim_hold.hold_id].owner

    # Force every fresh token to collide with the victim's id.
    colliding = victim_hold.hold_id.removeprefix("hold:")
    monkeypatch.setattr(coordination.secrets, "token_hex", lambda _n: colliding)

    intruder = FakeLockConnection(
        space, pid=8102, termination_raises=RuntimeError("cannot terminate")
    )
    intruder_lease = await acquire_physical_account_lease(
        keys=[842], connection_factory=ConnectionFactory(intruder)
    )
    intruder._unlock_returns = False
    with pytest.raises(CoordinationError) as excinfo:
        await intruder_lease.release(intruder_lease.grant)
    # Allocation exhausted its attempts rather than stealing the id.
    assert excinfo.value.reason_code in {
        CoordinationReasonCode.LEASE_LOST,
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE,
    }

    retained = _retained_authorities()[victim_hold.hold_id]
    assert retained.connection is victim_connection
    assert retained.owner is victim_owner


@pytest.mark.asyncio
async def test_lock_foreign_lease_cannot_reuse_an_exposed_hold_id():
    space = FakeLockSpace()
    _, victim_connection, victim_hold = await _durable_true_hold(
        space, pid=8201, key=851
    )
    intruder = FakeLockConnection(space, pid=8202)
    intruder_lease = await acquire_physical_account_lease(
        keys=[852], connection_factory=ConnectionFactory(intruder)
    )

    with pytest.raises(CoordinationError) as excinfo:
        intruder_lease._retain_authority(
            reason_code=CoordinationReasonCode.LEASE_LOST,
            durable_evidence_written=True,
            hold_id=victim_hold.hold_id,
        )

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert _retained_authorities()[victim_hold.hold_id].connection is victim_connection
    assert intruder_lease.unreleased_authority_hold is None
    await intruder_lease.release(intruder_lease.grant)


@pytest.mark.asyncio
async def test_claim_durable_false_state_cannot_be_promoted_by_a_caller():
    """B25: the safety boolean is monotonic and not caller-settable."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(envelope, stack)
    hold_id = (set(held_coordinations()) - held_before).pop()
    held = _held_coordination(hold_id)
    assert held is not None

    # Sealed, so a caller-supplied capability is refused outright...
    with pytest.raises(CoordinationError):
        held.lease._retain_authority(
            reason_code=CoordinationReasonCode.LEASE_LOST,
            durable_evidence_written=True,
            hold_id=hold_id,
        )
    # ...and even holding the real capability cannot promote a false to a true.
    capability = coordination._COORDINATION_RELEASE_CAPABILITIES[hold_id]
    unchanged = held.lease._retain_authority(
        reason_code=CoordinationReasonCode.LEASE_LOST,
        durable_evidence_written=True,
        hold_id=hold_id,
        capability=capability,
    )
    assert unchanged.durable_evidence_written is False
    assert held.lease.unreleased_authority_hold.durable_evidence_written is False

    with pytest.raises(CoordinationError) as excinfo:
        await held.lease.release(held.grant)
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False


def test_scope_no_stale_public_capability_prose_survives():
    """B34: prose drifts silently, and nothing else in this suite catches it.

    Every phrase here described a public surface that three rounds of narrowing
    removed. Leaving them behind is how a reader concludes a capability is
    reachable when it is not — or, worse, that it is safe because a document
    said so.
    """

    def flat(text: str) -> str:
        # Prose wraps and gets recapitalised; a phrase split across a line break
        # or starting a sentence is still the phrase.
        return " ".join(text.split()).lower()

    source = flat(COORDINATION_SOURCE.read_text(encoding="utf-8"))
    doc = flat(CONTRACT_DOC.read_text(encoding="utf-8"))

    for text, label in ((source, "coordination.py"), (doc, "contract")):
        for stale in (
            "exact grant obtained from public introspection",
            "exact grant reachable from",
            "publishes the lease and the grant",
            "recoverable by the exact grant",
            ":meth:`retain_authority`",
            "grant is obtainable through public introspection",
        ):
            assert stale not in text, f"{label}: stale phrase {stale!r}"

    # A public `retain_authority` no longer exists; only the private one may be
    # named, and only in the source.
    assert "retain_authority" not in doc
    assert "def retain_authority" not in source
    assert "_retain_authority" in source

    # The contract must state the honest recovery split.
    assert "no in-process recovery api in this epoch" in doc
    assert "recoverable_in_process" in doc
    for owner_state in ("unsealed standalone lease", "coordination-sealed lease"):
        assert owner_state in doc, owner_state


def test_scope_contract_documents_the_every_send_callback_interface():
    """B45: J3B/J3C integrate from the contract, not from the source.

    A contract that still describes ``assert_owned(grant)`` tells a downstream
    lane to reach for a capability that is deliberately not public, and says
    nothing about the per-send timing that is the whole point of the view.
    """

    doc = CONTRACT_DOC.read_text(encoding="utf-8")
    flat = " ".join(doc.split())

    # The exact interface, by name.
    assert "CoordinationScope" in doc
    assert "await scope.assert_owned()" in doc
    assert "MockMutationCallback" in doc
    assert "one argument" in flat or "one-argument" in flat

    # The timing obligation, not just the existence of the call.
    assert "immediately before **every** POST" in flat
    assert "cancel or reduce" in flat
    assert "captured scope" in flat

    # The capability-free shape, as exact propositions. Searching for the words
    # "lease"/"grant" is a false green: rewriting "carries no lease" into
    # "carries a lease" keeps every one of those words present.
    assert "carries no lease, grant, connection, backend PID, owner token" in flat
    assert "the right to see, never the right to act" in flat
    for forbidden in ("carries a lease", "exposes the grant", "returns the connection"):
        assert forbidden not in flat, forbidden
    # The stale grant-bearing instruction is gone.
    assert "`assert_owned(grant)` repeats" not in doc


def test_scope_capability_bearing_symbols_are_not_public():
    """r17: a lane supplies extra keys through the coordinator, never a lease."""

    for name in (
        "PostgresAdvisoryKeysetLease",
        "acquire_physical_account_lease",
        "AdvisoryLeaseGrant",
    ):
        assert name not in coordination.__all__, name
        # Still defined — narrowing the surface, not deleting the machinery.
        assert hasattr(coordination, name), name

    exported = set(coordination.__all__)
    assert "coordinate_mock_order_mutation" in exported
    # Nothing left in the public surface hands out a connection, a grant, or a
    # release/terminate callable.
    for name in exported:
        value = getattr(coordination, name)
        assert not isinstance(value, coordination.AdvisoryLeaseGrant), name
    star: dict[str, Any] = {}
    exec("from app.services.mock_integration.coordination import *", star)  # noqa: S102
    for forbidden in (
        "PostgresAdvisoryKeysetLease",
        "acquire_physical_account_lease",
        "AdvisoryLeaseGrant",
        "_retained_authorities",
        "_HeldCoordination",
    ):
        assert forbidden not in star, forbidden


def test_scope_public_introspection_exposes_no_release_capability():
    """B26: static shape — a snapshot must not carry a way to act."""

    import dataclasses

    forbidden = {"connection", "lease", "grant", "owner_token", "backend_pid", "pid"}
    for record in (
        coordination.HeldCoordinationSnapshot,
        coordination.UnreleasedAuthorityHold,
    ):
        names = {field.name for field in dataclasses.fields(record)}
        assert names.isdisjoint(forbidden), (record, names & forbidden)
        for field in dataclasses.fields(record):
            assert field.type not in {"LockAuthorityConnection", "AdvisoryLeaseGrant"}
    # Neither the private ownership map nor the raw handle is exported.
    assert "_retained_authorities" not in coordination.__all__
    assert "HeldCoordination" not in coordination.__all__
    assert "_HeldCoordination" not in coordination.__all__


@pytest.mark.asyncio
async def test_scope_public_snapshot_offers_nothing_callable_to_release_with():
    space = FakeLockSpace()
    lease, connection, hold = await _durable_true_hold(space, pid=8301, key=861)

    snapshot = [h for h in unreleased_authority_holds() if h.hold_id == hold.hold_id]
    assert len(snapshot) == 1
    for name in ("connection", "lease", "grant", "terminate_backend_session"):
        assert not hasattr(snapshot[0], name)
    await lease.release(lease.grant)


@pytest.mark.asyncio
async def test_scope_held_coordination_lookup_returns_a_snapshot_not_the_handle():
    """B26 at runtime: the public lookup must not hand back the real handle."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(envelope, stack)
    hold_id = (set(held_coordinations()) - held_before).pop()

    public = held_coordination(hold_id)
    assert isinstance(public, HeldCoordinationSnapshot)
    assert type(public) is HeldCoordinationSnapshot
    for name in ("lease", "grant", "connection", "claim", "envelope"):
        assert not hasattr(public, name), name
    assert all(
        isinstance(value, HeldCoordinationSnapshot)
        for value in held_coordinations().values()
    )
    # The real handle is reachable only through the module-private hook.
    assert _held_coordination(hold_id) is not public


@pytest.mark.asyncio
async def test_lock_in_flight_grant_with_cancelled_termination_is_retained():
    """B27: a CancelledError inside termination is 'unknown', not 'released'."""

    space = FakeLockSpace()
    connection = FakeLockConnection(
        space,
        pid=8401,
        raise_after_lock_on_key=872,
        raise_after_lock_error=asyncio.CancelledError(),
        termination_raises=asyncio.CancelledError(),
    )
    retained_before = set(_retained_authorities())

    with pytest.raises(asyncio.CancelledError):
        await acquire_physical_account_lease(
            keys=[871, 872], connection_factory=ConnectionFactory(connection)
        )

    assert space.held.get(872) == 8401
    assert connection.closed is False
    assert connection.terminated is False
    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    retained = _retained_authorities()[new_holds.pop()]
    assert retained.connection is connection
    assert retained.grant.backend_pid == 8401

    successor = FakeLockConnection(space, pid=8402)
    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[872], connection_factory=ConnectionFactory(successor)
        )
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED


@pytest.mark.asyncio
async def test_lock_partial_acquire_hold_is_not_reported_as_recoverable():
    """B30 §4: with no owning lease there is no in-process resolution path."""

    space = FakeLockSpace()
    connection = FakeLockConnection(
        space,
        pid=8501,
        raise_after_lock_on_key=882,
        termination_raises=RuntimeError("cannot terminate"),
    )
    retained_before = set(_retained_authorities())

    with pytest.raises(RuntimeError):
        await acquire_physical_account_lease(
            keys=[881, 882], connection_factory=ConnectionFactory(connection)
        )

    hold_id = (set(_retained_authorities()) - retained_before).pop()
    hold = next(h for h in unreleased_authority_holds() if h.hold_id == hold_id)
    assert hold.recoverable_in_process is False
    assert _retained_authorities()[hold_id].connection is connection


@pytest.mark.asyncio
async def test_lock_durable_true_hold_stays_resolvable_after_the_caller_drops_it():
    """B30 §3: the retained record identifies the owner, not the caller's local."""

    import gc

    space = FakeLockSpace()
    lease, connection, hold = await _durable_true_hold(space, pid=8601, key=891)
    assert hold.recoverable_in_process is True
    retained = _retained_authorities()[hold.hold_id]
    owner = retained.owner
    del lease
    gc.collect()

    # The module still owns the exact lease, so a proven release still resolves.
    assert _retained_authorities()[hold.hold_id].owner is owner
    await owner.release(owner.grant)
    assert hold.hold_id not in _retained_authorities()
    assert connection.closed is True


@pytest.mark.asyncio
async def test_lock_second_wrapper_over_a_held_authority_is_refused():
    """B29: non-releasability belongs to the authority, not to a wrapper."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(envelope, stack)
    hold_id = (set(held_coordinations()) - held_before).pop()
    held = _held_coordination(hold_id)
    assert held is not None
    connection = stack["connection"]

    with pytest.raises(CoordinationError) as excinfo:
        PostgresAdvisoryKeysetLease(connection=connection, grant=held.grant)
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert connection.unlock_calls == []
    assert connection.closed is False
    assert connection.terminated is False
    assert hold_id in held_coordinations()


@pytest.mark.asyncio
async def test_lock_clone_over_a_durable_true_retained_authority_is_refused():
    """C3: the lockout is symmetric — the evidence flag is not what makes a
    foreign wrapper unsafe. Only the owning lease may retry its own authority."""

    space = FakeLockSpace()
    lease, connection, hold = await _durable_true_hold(space, pid=9301, key=951)
    assert hold.durable_evidence_written is True

    with pytest.raises(CoordinationError) as excinfo:
        PostgresAdvisoryKeysetLease(connection=connection, grant=lease.grant)
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert connection.unlock_calls[-1] == 951  # only the owner's failed attempt
    assert connection.closed is False

    # The owner itself is still allowed to retry, which is what makes the
    # `recoverable_in_process=True` report honest.
    await lease.release(lease.grant)
    assert lease.released is True


@pytest.mark.asyncio
async def test_lock_single_true_unlock_is_not_full_release_of_a_stacked_lock():
    """B31: session advisory locks are re-entrant; one unlock is not enough."""

    space = FakeLockSpace()
    connection = FakeLockConnection(
        space, pid=8701, termination_raises=RuntimeError("cannot terminate")
    )
    lease = await acquire_physical_account_lease(
        keys=[901], connection_factory=ConnectionFactory(connection)
    )
    # Somebody stacked a second lock on the same backend and key.
    assert space.try_lock(901, 8701) is True
    assert space.depth[(901, 8701)] == 2
    retained_before = set(_retained_authorities())

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    # The unlock returned true, and the row is still there — not a release.
    assert connection.unlock_calls == [901]
    assert space.held.get(901) == 8701
    assert connection.closed is False
    assert lease.released is False
    assert len(set(_retained_authorities()) - retained_before) == 1


@pytest.mark.asyncio
async def test_lock_acquisition_refuses_a_backend_that_already_holds_the_key():
    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=8801)
    space.try_lock(911, 8801)

    with pytest.raises(CoordinationError) as excinfo:
        await acquire_physical_account_lease(
            keys=[911], connection_factory=ConnectionFactory(connection)
        )

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert connection.lock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["attestation", "unlock", "termination"])
async def test_lock_cancellation_inside_release_leaves_no_untracked_authority(
    stage: str,
):
    """B32: a CancelledError in the critical section is 'unknown', not 'done'."""

    space = FakeLockSpace()
    kwargs: dict[str, Any] = {
        "pid": 8901,
        "termination_raises": RuntimeError("cannot terminate"),
    }
    if stage == "termination":
        kwargs["termination_raises"] = asyncio.CancelledError()
    connection = FakeLockConnection(space, **kwargs)
    lease = await acquire_physical_account_lease(
        keys=[921, 922], connection_factory=ConnectionFactory(connection)
    )
    if stage == "attestation":
        connection._fail_sql = "pg_backend_pid"
        connection._fail_sql_error = asyncio.CancelledError()
    else:
        connection._unlock_raises_on_key = 921
        connection._unlock_raises_error = asyncio.CancelledError()
    retained_before = set(_retained_authorities())

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code in {
        CoordinationReasonCode.LEASE_LOST,
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE,
    }
    # No untracked live authority: it is retained, and nothing was closed.
    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    retained = _retained_authorities()[new_holds.pop()]
    assert retained.connection is connection
    assert retained.owner is lease
    assert connection.closed is False
    assert lease.released is False


@pytest.mark.asyncio
async def test_lock_recoverable_flag_matches_actual_retry_reachability():
    """C1: the flag must describe a retry that exists, not the owner's type."""

    space = FakeLockSpace()

    # (a) standalone durable-TRUE hold: the caller holds a releasable lease.
    lease, connection, hold = await _durable_true_hold(space, pid=9101, key=931)
    active = {h.hold_id: h for h in unreleased_authority_holds()}
    assert active[hold.hold_id].recoverable_in_process is True
    # And the claim is honest: the retry really does work.
    await lease.release(lease.grant)
    assert lease.released is True

    # (b) partial-acquisition rollback hold: no owning lease exists at all.
    rollback_conn = FakeLockConnection(
        space,
        pid=9102,
        raise_after_lock_on_key=942,
        termination_raises=RuntimeError("cannot terminate"),
    )
    with pytest.raises(RuntimeError):
        await acquire_physical_account_lease(
            keys=[941, 942], connection_factory=ConnectionFactory(rollback_conn)
        )
    rollback_hold = next(
        h
        for h in unreleased_authority_holds()
        if _retained_authorities()[h.hold_id].connection is rollback_conn
    )
    assert rollback_hold.recoverable_in_process is False

    # (c) coordination-sealed durable-FALSE hold: sealed forever, so no retry.
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(envelope, stack)
    sealed_id = (set(held_coordinations()) - held_before).pop()
    sealed_hold = next(
        h for h in unreleased_authority_holds() if h.hold_id == sealed_id
    )
    assert sealed_hold.recoverable_in_process is False
    sealed = _held_coordination(sealed_id)
    assert sealed is not None and sealed.lease.coordination_sealed is True
    with pytest.raises(CoordinationError):
        await sealed.lease.release(sealed.grant)


@pytest.mark.asyncio
async def test_claim_durable_false_lockout_blocks_even_the_owning_lease():
    """C2: the seal and the durable-false lockout are not substitutes.

    They cover different callers. The seal stops the public path; the lockout
    stops *every* path, the owning lease included. A refactor that treats either
    as sufficient reopens the other's window.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(envelope, stack)
    hold_id = (set(held_coordinations()) - held_before).pop()
    held = _held_coordination(hold_id)
    assert held is not None

    # The coordination hold and the durable-false hold share one opaque id...
    assert held.lease.unreleased_authority_hold is not None
    assert held.lease.unreleased_authority_hold.hold_id == hold_id
    # ...so the lockout must key on the evidence flag, not on id inequality.
    assert (
        coordination._authority_lockout(stack["connection"], owner=held.lease)
        == hold_id
    )
    # Even the real coordination capability cannot release it.
    capability = coordination._COORDINATION_RELEASE_CAPABILITIES[hold_id]
    with pytest.raises(CoordinationError) as excinfo:
        await held.lease._release_with_capability(held.grant, capability)
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False


@pytest.mark.asyncio
async def test_cancel_repeated_outer_cancellation_stays_bounded():
    """C5: repeated external cancels must not spin the retained-wait loop."""

    _, envelope = _attempt_envelope()
    gate = asyncio.Event()
    started = asyncio.Event()
    stack = _default_stack()
    stack["callback"] = RecordingCallback(gate=gate, started=started)

    task = asyncio.ensure_future(_run(envelope, stack))
    await started.wait()
    for _ in range(25):
        task.cancel()
        await asyncio.sleep(0)

    assert task.done() is False
    assert stack["connection"].unlock_calls == []
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The inner task was never cancelled; it ran to a definite result.
    assert stack["callback"].calls == 1
    assert stack["evidence"].calls == 1


@pytest.mark.asyncio
async def test_claim_model_premise_claim_commits_before_the_callback():
    """The whole process-death argument rests on this ordering.

    If the durable claim were written after the send, a crash mid-callback would
    leave nothing blocking a successor's blind repost, and "process death is an
    acceptable terminus" would stop being true.
    """

    _, envelope = _attempt_envelope()
    events: list[str] = []
    stack = _default_stack(events)
    await _run(envelope, stack)

    assert events.index("reserve") < events.index("callback_start")
    assert AUTOMATIC_CLAIM_RELEASE_AVAILABLE is False
    assert LEASE_TTL_SECONDS is None
    # The only deletion path runs through the evidence gate.
    source = COORDINATION_SOURCE.read_text(encoding="utf-8")
    assert source.count("release_if_matches(") == 2  # the port and its one call
    # The single deletion path is gated on exact-boolean terminal evidence.
    assert "_terminal_evidence_authorizes(evidence)" in source
    assert "CoordinationReasonCode.TERMINAL_EVIDENCE_REQUIRED" in source


# ===========================================================================
# §r20 — B33-B42 and the C1 truth table
# ===========================================================================


@pytest.mark.asyncio
async def test_claim_scope_assertion_blocks_a_send_after_delayed_ownership_loss():
    """B33: the coordinator's single pre-callback check is not enough.

    A lane can await account truth or a token refresh after entry, and ownership
    can vanish in that interval. Detecting it at release — after the order is out
    — is not the property the signed briefs ask for.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    connection = stack["connection"]
    sends: list[str] = []

    async def delayed_lane(scope: Any) -> MutationCallbackResult:
        await asyncio.sleep(0)
        connection.simulate_session_loss()
        await scope.assert_owned()  # must raise
        sends.append("POST")  # unreachable
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO"
        )

    stack["callback"] = delayed_lane
    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert sends == []
    # The unknown is still durable, and the claim still blocks the account.
    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_scope_assertion_is_required_before_every_send_in_a_batch():
    """B33: a same-cycle batch must re-assert per POST, not once."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    connection = stack["connection"]
    sends: list[str] = []

    async def two_post_lane(scope: Any) -> MutationCallbackResult:
        await scope.assert_owned()
        sends.append("POST#1")
        # Ownership disappears between the two POSTs of the same batch.
        connection.simulate_session_loss()
        await scope.assert_owned()  # the second assert must catch it
        sends.append("POST#2")  # unreachable
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO"
        )

    stack["callback"] = two_post_lane
    with pytest.raises(CoordinationError):
        await _run(envelope, stack)

    assert sends == ["POST#1"]
    # A lane that skipped the second assertion would have sent twice; that is
    # the whole point of putting the operation in the lane's hands.
    assert stack["evidence"].only.kind is DispatchEvidenceKind.CALLBACK_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_result",
    [
        "not-a-result",
        {"certainty": "definitive"},
        MutationCallbackResult(certainty="uncertain", broker_order_id="ODNO"),
        MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id=123
        ),
    ],
    ids=["raw_string", "dict", "raw_string_certainty", "non_string_broker_id"],
)
async def test_claim_invalid_callback_result_becomes_typed_uncertainty(
    bad_result: Any,
):
    """B35: an annotation is not a runtime guarantee.

    A raw-string certainty slips past every ``is MutationCertainty.X`` branch and
    lands in the durable record misclassified as an acknowledgement; a dict
    raises on attribute access *after* a possible send. Both must become typed
    uncertainty that is durable before anything is cleaned up.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()

    async def returns_bad(scope: Any) -> Any:
        return bad_result

    stack["callback"] = returns_bad
    with pytest.raises(TypeError):
        await _run(envelope, stack)

    evidence = stack["evidence"].only
    assert evidence.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    assert isinstance(evidence.certainty, MutationCertainty)
    assert evidence.broker_order_id is None
    # Never an acknowledgement, and never an escape before the durable writes.
    assert stack["persistence"].calls == 2
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_synchronous_send_then_raise_is_not_never_started():
    """B42: a callable can POST in a synchronous prelude and *then* raise.

    Calling that "the callback never started" writes no evidence and hands the
    writer authority back, even though an order may already be at the broker.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    posts: list[str] = []

    def sync_post_then_raise(scope: Any) -> Any:
        posts.append("POST")  # a synchronous broker SDK call
        raise RuntimeError("sdk exploded after sending")

    stack["callback"] = sync_post_then_raise
    with pytest.raises(RuntimeError, match="sdk exploded"):
        await _run(envelope, stack)

    assert posts == ["POST"]
    # The unknown became durable before anything was cleaned up.
    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert stack["evidence"].only.certainty is MutationCertainty.UNCERTAIN
    assert stack["persistence"].calls == 2
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_non_awaitable_callback_return_takes_the_evidence_path():
    """B42: a non-awaitable return is also post-invocation, not pre-callback."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()

    def non_awaitable(scope: Any) -> Any:
        return "not a coroutine"

    stack["callback"] = non_awaitable
    with pytest.raises(TypeError, match="awaitable"):
        await _run(envelope, stack)

    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_invocation_failure_with_broken_durability_keeps_the_hold():
    """B42: if either durable write fails, the durable-false hold must remain."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    posts: list[str] = []

    def sync_post_then_raise(scope: Any) -> Any:
        posts.append("POST")
        raise RuntimeError("sdk exploded after sending")

    stack["callback"] = sync_post_then_raise
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert posts == ["POST"]
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert len(set(held_coordinations()) - held_before) == 1


@pytest.mark.asyncio
async def test_claim_scope_is_dead_once_its_coordinated_section_ends():
    """B33: a captured scope cannot assert against a finished lease."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    captured: list[Any] = []

    async def capture(scope: Any) -> MutationCallbackResult:
        captured.append(scope)
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO"
        )

    stack["callback"] = capture
    await _run(envelope, stack)

    assert len(captured) == 1
    with pytest.raises(CoordinationError) as excinfo:
        await captured[0].assert_owned()
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST


def test_scope_view_exposes_exactly_one_operation_and_no_capability():
    """B33: the view is the read right, never the write right."""

    public_names = {name for name in dir(CoordinationScope) if not name.startswith("_")}
    assert public_names == {"assert_owned"}
    # The one private slot holds a closure, not the lease. On the supported and
    # direct surface there is nothing to reach; under Python reflection
    # ``_assert.__closure__`` can of course be traversed, exactly as any exported
    # function's ``__globals__`` can. The signed grade is accident prevention plus
    # static detection, not structural impossibility.
    private_names = {
        name
        for name in dir(CoordinationScope)
        if name.startswith("_") and not name.startswith("__")
    }
    assert private_names == {"_assert"}
    assert CoordinationScope.__slots__ == ("_assert",)
    for forbidden in (
        "lease",
        "grant",
        "connection",
        "release",
        "terminate",
        "backend_pid",
        "owner_token",
        "keys",
        "hold_id",
        "unlock",
    ):
        assert not hasattr(CoordinationScope, forbidden), forbidden


@pytest.mark.asyncio
async def test_claim_registry_swapped_during_reserve_never_reaches_the_callback():
    """B41: authority is derived once; every later check must match that entry."""

    _, envelope = _attempt_envelope()
    bound = _bound_registry(envelope)
    # A registry object that quietly becomes a *different* physical account the
    # second time it is read — exactly what an awaited reserve permits.
    swapped = _bound_registry(envelope, physical_account_id="OTHER-PHYSICAL-ACCOUNT")

    class MutatingRegistry:
        def __init__(self) -> None:
            self.reads = 0

        def __iter__(self) -> Any:
            self.reads += 1
            return iter(bound if self.reads <= 1 else swapped)

    stack = _default_stack()
    with pytest.raises(registry.LaneGuardError) as excinfo:
        await coordinate_mock_order_mutation(
            **{
                **_coordination_kwargs(
                    envelope,
                    lane_registry=bound,
                    persistence=stack["persistence"],
                    evidence=stack["evidence"],
                    intents=stack["intents"],
                    factory=stack["factory"],
                    callback=stack["callback"],
                ),
                "registry": MutatingRegistry(),
            }
        )

    assert excinfo.value.code == "canonical_lane_identity_mismatch"
    assert stack["callback"].calls == 0
    # The claim for account A is retained; account B was never touched.
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["first", "middle", "final"])
async def test_lock_cancellation_at_each_reverse_unlock_position(position: str):
    """B38: cover first/middle/final, and assert the confirmed prefix."""

    space = FakeLockSpace()
    keys = [11, 22, 33]  # reverse order is 33, 22, 11
    target = {"first": 33, "middle": 22, "final": 11}[position]
    connection = FakeLockConnection(
        space,
        pid=9601,
        termination_raises=RuntimeError("cannot terminate"),
        unlock_raises_on_key=target,
        unlock_raises_error=asyncio.CancelledError(),
    )
    lease = await acquire_physical_account_lease(
        keys=keys, connection_factory=ConnectionFactory(connection)
    )
    retained_before = set(_retained_authorities())

    with pytest.raises(CoordinationError):
        await lease.release(lease.grant)

    expected_prefix = {"first": (), "middle": (33,), "final": (33, 22)}[position]
    assert lease.unlocked_keys == expected_prefix
    assert lease.released is False
    assert connection.closed is False
    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    assert _retained_authorities()[new_holds.pop()].connection is connection


@pytest.mark.asyncio
async def test_lock_false_unlock_after_a_confirmed_prefix_keeps_the_progress():
    """B38: a mid-sequence false unlock must still record what *was* confirmed.

    Losing the prefix would describe the remaining authority as larger than it
    is, which is the same dishonesty as claiming a release that did not happen.
    """

    space = FakeLockSpace()
    connection = FakeLockConnection(
        space,
        pid=9651,
        unlock_false_on_key=22,  # reverse order is 33, 22, 11
        termination_raises=RuntimeError("cannot terminate"),
    )
    lease = await acquire_physical_account_lease(
        keys=[11, 22, 33], connection_factory=ConnectionFactory(connection)
    )
    retained_before = set(_retained_authorities())

    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(lease.grant)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert connection.unlock_calls == [33, 22]
    assert lease.unlocked_keys == (33,)  # the confirmed prefix, preserved
    assert lease.released is False
    assert connection.closed is False
    assert len(set(_retained_authorities()) - retained_before) == 1


@pytest.mark.asyncio
async def test_lock_rollback_survives_a_second_cancel_during_gated_termination():
    """B39: a cancel arriving *inside* rollback termination must not abandon it."""

    space = FakeLockSpace()
    gate = asyncio.Event()
    started = asyncio.Event()

    class GatedTerminationConnection(FakeLockConnection):
        async def terminate_backend_session(
            self, *, expected_pid: int, owner_token: str
        ):
            started.set()
            await gate.wait()
            raise RuntimeError("termination still unproven")

    connection = GatedTerminationConnection(
        space, pid=9701, raise_after_lock_on_key=962
    )
    retained_before = set(_retained_authorities())

    task = asyncio.ensure_future(
        acquire_physical_account_lease(
            keys=[961, 962], connection_factory=ConnectionFactory(connection)
        )
    )
    await started.wait()
    for _ in range(5):
        task.cancel()  # the second (and third) cancel
        await asyncio.sleep(0)

    assert task.done() is False  # rollback still running, not abandoned
    assert connection.closed is False
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    assert _retained_authorities()[new_holds.pop()].connection is connection
    assert connection.closed is False


@pytest.mark.asyncio
async def test_lock_cancellation_during_confirmed_rollback_unlock_is_retained():
    """B39: interrupting a confirmed partial rollback leaves it tracked."""

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=9801)
    await acquire_physical_account_lease(
        keys=[973], connection_factory=ConnectionFactory(blocker)
    )
    connection = FakeLockConnection(
        space,
        pid=9802,
        unlock_raises_on_key=972,
        unlock_raises_error=asyncio.CancelledError(),
        termination_raises=RuntimeError("cannot terminate"),
    )
    retained_before = set(_retained_authorities())

    # The interruption is re-delivered — not the earlier contention error.
    with pytest.raises(asyncio.CancelledError):
        await acquire_physical_account_lease(
            keys=[971, 972, 973], connection_factory=ConnectionFactory(connection)
        )

    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    assert _retained_authorities()[new_holds.pop()].connection is connection
    assert connection.closed is False


@pytest.mark.asyncio
async def test_lock_hold_id_exhaustion_still_retains_the_new_authority(monkeypatch):
    """B40: a collision storm must not make the newly unsafe authority vanish."""

    import gc

    space = FakeLockSpace()
    _, victim_connection, victim_hold = await _durable_true_hold(
        space, pid=9901, key=981
    )
    victim_owner = _retained_authorities()[victim_hold.hold_id].owner

    colliding = victim_hold.hold_id.removeprefix("hold:")
    monkeypatch.setattr(coordination.secrets, "token_hex", lambda _n: colliding)

    intruder = FakeLockConnection(
        space,
        pid=9902,
        raise_after_lock_on_key=983,
        termination_raises=RuntimeError("cannot terminate"),
    )
    retained_before = set(_retained_authorities())
    with pytest.raises(RuntimeError):
        await acquire_physical_account_lease(
            keys=[982, 983], connection_factory=ConnectionFactory(intruder)
        )

    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    quarantined = new_holds.pop()
    assert quarantined.startswith("hold:quarantine:")
    retained = _retained_authorities()[quarantined]
    assert retained.connection is intruder
    assert retained.grant.backend_pid == 9902
    gc.collect()
    assert _retained_authorities()[quarantined].connection is intruder
    assert intruder.closed is False

    # The victim is untouched, and a successor still contends on the held key.
    assert _retained_authorities()[victim_hold.hold_id].connection is victim_connection
    assert _retained_authorities()[victim_hold.hold_id].owner is victim_owner
    successor = FakeLockConnection(space, pid=9903)
    with pytest.raises(CoordinationError) as contended:
        await acquire_physical_account_lease(
            keys=[983], connection_factory=ConnectionFactory(successor)
        )
    assert contended.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED


@pytest.mark.asyncio
async def test_lock_c1_truth_table_all_five_rows():
    """C1: the flag must equal the negation of the two release blockers.

    Each row asserts the flag **and** demonstrates the behaviour it claims —
    asserting the flag alone would be a tautology.
    """

    space = FakeLockSpace()

    # T1 unsealed x durable-TRUE -> True, and the retry really works.
    lease, connection, hold = await _durable_true_hold(space, pid=10001, key=1011)
    assert _active_hold(hold.hold_id).recoverable_in_process is True
    await lease.release(lease.grant)
    assert lease.released is True

    # T2 unsealed x durable-FALSE -> False, and release is refused.
    conn2 = FakeLockConnection(space, pid=10002)
    lease2 = await acquire_physical_account_lease(
        keys=[1012], connection_factory=ConnectionFactory(conn2)
    )
    hold2 = lease2._retain_authority(
        reason_code=CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE,
        durable_evidence_written=False,
    )
    assert _active_hold(hold2.hold_id).recoverable_in_process is False
    with pytest.raises(CoordinationError) as blocked:
        await lease2.release(lease2.grant)
    assert blocked.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    assert conn2.unlock_calls == []
    assert conn2.closed is False

    # T3 sealed x durable-TRUE -> False, public release refused, and ONE id.
    _, envelope = _attempt_envelope()
    stack = _default_stack()
    sealed_conn = stack["connection"]
    sealed_conn._termination_raises = RuntimeError("cannot terminate")

    async def flip(scope: Any) -> MutationCallbackResult:
        sealed_conn._unlock_returns = False
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO"
        )

    stack["callback"] = flip
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(envelope, stack)
    handle_id = (set(held_coordinations()) - held_before).pop()
    sealed = _held_coordination(handle_id)
    assert sealed is not None
    sealed_hold = sealed.lease.unreleased_authority_hold
    assert sealed_hold is not None
    assert sealed_hold.durable_evidence_written is True
    assert sealed_hold.recoverable_in_process is False
    # One stuck authority, one opaque id an operator can follow.
    assert sealed_hold.hold_id == handle_id
    with pytest.raises(CoordinationError):
        await sealed.lease.release(sealed.grant)

    # T4 sealed x durable-FALSE -> False (covered in depth elsewhere).
    _, env4 = _attempt_envelope()
    stack4 = _default_stack()
    stack4["evidence"] = RecordingDispatchEvidence(fail=True)
    before4 = set(held_coordinations())
    with pytest.raises(CoordinationError):
        await _run(env4, stack4)
    id4 = (set(held_coordinations()) - before4).pop()
    assert _active_hold(id4).recoverable_in_process is False

    # T5 rollback with no owning lease -> False.
    rollback_conn = FakeLockConnection(
        space,
        pid=10005,
        raise_after_lock_on_key=1052,
        termination_raises=RuntimeError("cannot terminate"),
    )
    retained_before = set(_retained_authorities())
    with pytest.raises(RuntimeError):
        await acquire_physical_account_lease(
            keys=[1051, 1052], connection_factory=ConnectionFactory(rollback_conn)
        )
    rollback_id = (set(_retained_authorities()) - retained_before).pop()
    assert _active_hold(rollback_id).recoverable_in_process is False


def _active_hold(hold_id: str) -> Any:
    return next(h for h in unreleased_authority_holds() if h.hold_id == hold_id)


# ===========================================================================
# §r21 — U1-U9
# ===========================================================================


class _RaisingBrokerId(str):
    """A hostile ``str`` subclass: validation must not dispatch into it."""

    def strip(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("strip exploded")


@pytest.mark.asyncio
async def test_claim_hostile_broker_id_validation_still_reaches_both_writes():
    """U1/B43: validation runs after a possible send, so it cannot escape.

    An exact ``MutationCallbackResult`` can carry a ``str`` subclass whose
    ``strip()`` raises. That happens after the callback may have POSTed and
    before either durable write — the one window the AND gate exists to cover.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    posts: list[str] = []

    async def hostile(scope: Any) -> MutationCallbackResult:
        posts.append("POST")
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE,
            broker_order_id=_RaisingBrokerId("ODNO"),
        )

    stack["callback"] = hostile
    with pytest.raises(TypeError):
        await _run(envelope, stack)

    assert posts == ["POST"]
    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1
    evidence = stack["evidence"].only
    assert evidence.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    assert evidence.broker_order_id is None
    assert len(stack["intents"].rows) == 1


class _CancellingAckFactory(MockLineageFactory):
    """A J2B factory whose acknowledgement step is cancelled."""

    def acknowledge_order_attempt(
        self, envelope: LineageEnvelope, broker_order_id: str
    ) -> LineageEnvelope:
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_claim_cancelled_ack_attachment_still_reaches_both_writes():
    """U2/B44: a cancellation inside the ACK helper is uncertainty, not an exit."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    posts: list[str] = []

    async def sender(scope: Any) -> MutationCallbackResult:
        posts.append("POST")
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO"
        )

    stack["callback"] = sender
    with pytest.raises(asyncio.CancelledError):
        await coordinate_mock_order_mutation(
            **_coordination_kwargs(
                envelope,
                lane_registry=_bound_registry(envelope),
                persistence=stack["persistence"],
                evidence=stack["evidence"],
                intents=stack["intents"],
                factory=stack["factory"],
                callback=stack["callback"],
            ),
            lineage_factory=_CancellingAckFactory(),
        )

    assert posts == ["POST"]
    assert stack["evidence"].calls == 1
    evidence = stack["evidence"].only
    assert evidence.kind is DispatchEvidenceKind.ACK_ATTACHMENT_FAILED
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    assert evidence.broker_order_id is None
    # Nobody cancelled the coordinator; the ACK helper cancelled itself.
    assert evidence.outer_cancellation_requested is False
    assert stack["persistence"].calls == 2
    # Cleanup happened only after the gate closed, and the claim is retained.
    assert stack["connection"].closed is True
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["observer", "owner"])
async def test_lock_close_cancellation_after_absence_keeps_the_receipt(which: str):
    """U4/B37: once absence is proven, cleanup cannot un-prove it."""

    owner = _dedicated_connection_double([])
    observer = _dedicated_connection_double(
        [_FakeResult([{"terminated": True}]), _FakeResult([{"alive": 0}])]
    )
    if which == "observer":
        observer.close = AsyncMock(side_effect=asyncio.CancelledError())
    else:
        owner.close = AsyncMock(side_effect=asyncio.CancelledError())

    async def observer_factory() -> Any:
        return observer

    authority = SqlAlchemyLockAuthority(owner, observer_factory=observer_factory)
    holds_before = len(unreleased_authority_holds())

    receipt = await authority.terminate_backend_session(
        expected_pid=4242, owner_token="lockconn:abc"
    )

    # The proof stands, bound to the exact PID and owner token.
    assert receipt == BackendTerminationReceipt(
        backend_pid=4242, owner_token="lockconn:abc", terminated=True
    )
    # And no false active authority was manufactured by the failed cleanup.
    assert len(unreleased_authority_holds()) == holds_before


@pytest.mark.asyncio
async def test_lock_inner_cancellation_after_a_confirmed_unlock_prefix(monkeypatch):
    """B52: the load-bearing conjunction, in one test.

    Two half-tests do not cover this. One cancels on the *first* reverse unlock,
    so it has no confirmed prefix; the other establishes a prefix but cancels the
    **outer** acquisition task, which travels a different path. A defect that
    re-delivers an inner cancellation only when it lands on the first unlock, and
    swallows it once anything has been confirmed, passes both of them.

    So: confirm one unlock, then make the *next unlock await itself* raise
    ``CancelledError``.
    """

    import gc

    captured: dict[str, Any] = {}
    _capture_rollback_identities(monkeypatch, captured)

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=12003)
    await acquire_physical_account_lease(
        keys=[1203], connection_factory=ConnectionFactory(blocker)
    )
    gate = asyncio.Event()
    started = asyncio.Event()
    # Reverse rollback order is 1202 then 1201: 1202 confirms, 1201 blocks and
    # then raises the inner cancellation.
    connection = FakeLockConnection(
        space,
        pid=12002,
        unlock_gate_on_key=1201,
        unlock_gate=gate,
        unlock_gate_started=started,
        unlock_raises_on_key=1201,
        unlock_raises_error=asyncio.CancelledError(),
        termination_raises=RuntimeError("cannot terminate"),
    )
    retained_before = set(_retained_authorities())
    held_before = set(held_coordinations())

    task = asyncio.ensure_future(
        acquire_physical_account_lease(
            keys=[1201, 1202, 1203], connection_factory=ConnectionFactory(connection)
        )
    )
    await started.wait()
    # A non-empty confirmed prefix exists before the interruption arrives.
    assert connection.unlock_calls == [1202, 1201]
    # And the outer acquisition has not finished, so nothing was surrendered
    # while the rollback was still in flight.
    assert task.done() is False

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Exactly one strong hold, no close, no coordination handle, no claim work.
    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    hold_id = new_holds.pop()
    assert _retained_authorities()[hold_id].connection is connection
    assert connection.closed is False
    assert set(held_coordinations()) == held_before

    # The authority outlives every caller-side reference to it — and the retained
    # entry holds the exact ORIGINAL connection *and* the exact original grant,
    # both witnessed before any retention could have happened.
    weak_connection = captured["connection"]
    weak_grant = captured["grant"]
    del connection, task
    gc.collect()
    assert weak_connection() is not None
    assert weak_grant() is not None
    retained = _retained_authorities()[hold_id]
    assert retained.connection is weak_connection()
    assert retained.grant is weak_grant()

    successor = FakeLockConnection(space, pid=12004)
    with pytest.raises(CoordinationError) as contended:
        await acquire_physical_account_lease(
            keys=[1201], connection_factory=ConnectionFactory(successor)
        )
    assert contended.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED


@pytest.mark.asyncio
async def test_lock_outer_cancellation_during_a_gated_rollback_unlock_is_retained():
    """The sibling path: the cancellation comes from *outside*, not the unlock."""

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=11003)
    await acquire_physical_account_lease(
        keys=[1103], connection_factory=ConnectionFactory(blocker)
    )
    gate = asyncio.Event()
    started = asyncio.Event()
    connection = FakeLockConnection(
        space,
        pid=11002,
        unlock_gate_on_key=1101,
        unlock_gate=gate,
        unlock_gate_started=started,
        unlock_returns=None,
        termination_raises=RuntimeError("cannot terminate"),
    )
    retained_before = set(_retained_authorities())

    task = asyncio.ensure_future(
        acquire_physical_account_lease(
            keys=[1101, 1102, 1103], connection_factory=ConnectionFactory(connection)
        )
    )
    await started.wait()
    assert connection.unlock_calls == [1102, 1101]

    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    assert task.done() is False
    assert connection.closed is False

    connection._unlock_returns = False
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.closed is False
    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    assert _retained_authorities()[new_holds.pop()].connection is connection


@pytest.mark.asyncio
async def test_lock_collision_exhaustion_during_confirmed_partial_rollback(
    monkeypatch,
):
    """U6/B40 + r29 W4: the collision branch proves its own unique root."""

    import gc

    space = FakeLockSpace()
    _, victim_connection, victim_hold = await _durable_true_hold(
        space, pid=11101, key=1111
    )
    victim_owner = _retained_authorities()[victim_hold.hold_id].owner

    blocker = FakeLockConnection(space, pid=11102)
    await acquire_physical_account_lease(
        keys=[1114], connection_factory=ConnectionFactory(blocker)
    )
    colliding = victim_hold.hold_id.removeprefix("hold:")
    monkeypatch.setattr(coordination.secrets, "token_hex", lambda _n: colliding)

    # 1112/1113 acquire, 1114 contends -> confirmed-partial rollback, and the
    # unprovable unlock forces retention while every random id collides.
    intruder = FakeLockConnection(
        space,
        pid=11103,
        unlock_returns=False,
        termination_raises=RuntimeError("cannot terminate"),
    )
    captured: dict[str, Any] = {}
    _capture_rollback_identities(monkeypatch, captured)
    retained_before = set(_retained_authorities())
    active_before = set(_active_holds())
    # X3: snapshot the foreign world BEFORE the failure, by object identity and
    # order — history included. A count cannot see a foreign row replaced by an
    # equal-but-different object.
    foreign_before = dict(_retained_authorities())
    foreign_active_before = dict(_active_holds())
    foreign_history_before = list(coordination._AUTHORITY_HOLD_HISTORY)

    with pytest.raises(CoordinationError):
        await acquire_physical_account_lease(
            keys=[1112, 1113, 1114], connection_factory=ConnectionFactory(intruder)
        )

    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    quarantined = new_holds.pop()
    assert quarantined.startswith("hold:quarantine:")
    assert _retained_authorities()[quarantined].connection is intruder
    assert intruder.closed is False
    assert intruder.unlock_calls == [1113]

    # W4: prove the module-private map is the ONLY root. Both witnesses were
    # bound before retention, and the foreign snapshot deliberately excludes the
    # entry under test — including it would keep the object alive and turn this
    # into a false green.
    weak_connection = captured["connection"]
    weak_grant = captured["grant"]
    assert weak_connection() is intruder
    del intruder
    gc.collect()
    assert weak_connection() is not None
    assert weak_grant() is not None
    retained = _retained_authorities()[quarantined]
    assert retained.connection is weak_connection()
    assert retained.grant is weak_grant()
    assert retained.connection.closed is False
    assert retained.connection.unlock_calls == [1113]
    # Exactly one new active hold, and its active record exists.
    assert len(set(_active_holds()) - active_before) == 1
    assert quarantined in _active_holds()

    # And the physical key is genuinely still held.
    successor = FakeLockConnection(space, pid=11104)
    with pytest.raises(CoordinationError) as contended:
        await acquire_physical_account_lease(
            keys=[1112], connection_factory=ConnectionFactory(successor)
        )
    assert contended.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED

    # Every foreign entry is identical, by object, to the pre-failure snapshot —
    # retained, active, and the full history prefix in order.
    for other, entry in foreign_before.items():
        assert _retained_authorities()[other] is entry
    for other, row in foreign_active_before.items():
        assert _active_holds()[other] is row
    history_now = list(coordination._AUTHORITY_HOLD_HISTORY)
    assert [id(h) for h in history_now[: len(foreign_history_before)]] == [
        id(h) for h in foreign_history_before
    ]
    # Exactly one row was appended, and it is this branch's authority.
    assert len(history_now) == len(foreign_history_before) + 1
    assert history_now[-1] is _retained_authorities()[quarantined].raw_hold
    assert _retained_authorities()[victim_hold.hold_id].connection is victim_connection
    assert _retained_authorities()[victim_hold.hold_id].owner is victim_owner
    assert victim_connection.closed is False


@pytest.mark.asyncio
async def test_lock_rollback_task_error_still_retains_the_authority(monkeypatch):
    """U6/B40 + r29 W4: the task-error branch proves its own unique root.

    The previous shape had a false root: it snapshotted *all* retained entries —
    including the one under test — into a local, and that local `_RetainedAuthority`
    itself strongly owns the connection and grant. Deleting the caller's variables
    therefore proved nothing. Here the foreign snapshot is taken before the
    failure, a real foreign victim exists, and the entry under test is never
    placed in a local that outlives the collection.
    """

    import gc

    space = FakeLockSpace()
    # A real foreign victim, constructed BEFORE the failure under test.
    _, victim_connection, victim_hold = await _durable_true_hold(
        space, pid=11210, key=1120
    )
    victim_owner = _retained_authorities()[victim_hold.hold_id].owner
    foreign_before = dict(_retained_authorities())
    foreign_active_before = dict(_active_holds())
    foreign_history_before = list(coordination._AUTHORITY_HOLD_HISTORY)

    blocker = FakeLockConnection(space, pid=11201)
    await acquire_physical_account_lease(
        keys=[1122], connection_factory=ConnectionFactory(blocker)
    )
    connection = FakeLockConnection(space, pid=11202)
    captured: dict[str, Any] = {}

    async def exploding_rollback(
        conn: Any, acquired: Any, grant: Any, *, in_flight: Any = ()
    ) -> None:
        # Witness the exact originals before anything can retain them.
        captured["connection"] = weakref.ref(conn)
        captured["grant"] = weakref.ref(grant)
        # r35 U1: this frame is retained by the rollback task's exception
        # traceback, so its locals would keep the connection alive and the GC
        # assertion below would be satisfied by *this test* rather than by the
        # module. Drop them before raising, or the proof proves nothing.
        del conn, acquired, grant, in_flight
        raise RuntimeError("rollback itself failed")

    monkeypatch.setattr(
        coordination, "_rollback_partial_acquisition", exploding_rollback
    )
    retained_before = set(_retained_authorities())
    active_before = set(_active_holds())

    # Run the acquisition inside a helper so that frame — and every temporary it
    # holds, including the connection factory — is destroyed on return. Left in
    # the test's own frame, those temporaries are a *non-module* strong root and
    # the GC assertion below would pass for the wrong reason.
    async def attempt(factory: Any) -> None:
        with pytest.raises(CoordinationError) as excinfo:
            await acquire_physical_account_lease(
                keys=[1121, 1122], connection_factory=factory
            )
        # The exploding helper's traceback frames hold the connection in their
        # locals; clear them so they cannot root it either.
        _drop_exception_roots(excinfo.value)

    await attempt(ConnectionFactory(connection))

    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    hold_id = new_holds.pop()
    # Exactly one new active record, matching that hold.
    assert set(_active_holds()) - active_before == {hold_id}
    assert _retained_authorities()[hold_id].connection is connection
    assert connection.closed is False

    # X3: measured here, before the successor runs — the exploding helper is
    # still installed, so a later contended acquisition legitimately records its
    # own hold and would otherwise be miscounted as foreign corruption.
    history_after_failure = list(coordination._AUTHORITY_HOLD_HISTORY)
    assert [id(h) for h in history_after_failure[: len(foreign_history_before)]] == [
        id(h) for h in foreign_history_before
    ]
    assert len(history_after_failure) == len(foreign_history_before) + 1
    assert history_after_failure[-1] is _retained_authorities()[hold_id].raw_hold

    weak_connection = captured["connection"]
    weak_grant = captured["grant"]
    assert weak_connection() is connection
    # Drop every caller-side strong reference. Nothing that owns the connection
    # or grant may survive this point except the module-private map.
    del connection
    gc.collect()
    assert weak_connection() is not None
    assert weak_grant() is not None
    recovered = _retained_authorities()[hold_id]
    assert recovered.connection is weak_connection()
    assert recovered.grant is weak_grant()
    assert recovered.connection.closed is False
    assert recovered.connection.unlock_calls == []

    successor = FakeLockConnection(space, pid=11203)
    with pytest.raises(CoordinationError) as contended:
        await acquire_physical_account_lease(
            keys=[1121], connection_factory=ConnectionFactory(successor)
        )
    assert contended.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED

    # The foreign world is identical, by object, to the pre-failure snapshot.
    for other, entry in foreign_before.items():
        assert _retained_authorities()[other] is entry
    for other, row in foreign_active_before.items():
        assert _active_holds()[other] is row
    assert _retained_authorities()[victim_hold.hold_id].connection is victim_connection
    assert _retained_authorities()[victim_hold.hold_id].owner is victim_owner
    assert victim_connection.closed is False
    for other, row in foreign_active_before.items():
        assert _active_holds()[other] is row
    # The foreign prefix is still object-identical and in order after everything.
    history_now = list(coordination._AUTHORITY_HOLD_HISTORY)
    assert [id(h) for h in history_now[: len(foreign_history_before)]] == [
        id(h) for h in foreign_history_before
    ]
    # This branch's authority was recorded exactly once, and the retained entry
    # still points at that exact raw row.
    assert len([h for h in history_now if h.hold_id == hold_id]) == 1
    assert recovered.raw_hold is history_after_failure[-1]


@pytest.mark.asyncio
async def test_claim_scope_local_pinned_entry_check_is_independently_load_bearing():
    """U7/B41: exercise the check *inside* the scope, not the one before it.

    The earlier regression swaps the registry during reserve, so it fails at the
    post-reserve check and would stay green if the scope-local recheck were
    deleted. This one keeps A until the callback is running.
    """

    _, envelope = _attempt_envelope()
    bound = _bound_registry(envelope)
    swapped = _bound_registry(envelope, physical_account_id="OTHER-PHYSICAL-ACCOUNT")
    state = {"switched": False}

    class LateSwitchingRegistry:
        def __iter__(self) -> Any:
            return iter(swapped if state["switched"] else bound)

    stack = _default_stack()
    posts: list[str] = []

    async def lane(scope: Any) -> MutationCallbackResult:
        state["switched"] = True  # the registry changes mid-callback
        await scope.assert_owned()  # must catch it, before the POST
        posts.append("POST")  # unreachable
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO"
        )

    stack["callback"] = lane
    with pytest.raises(registry.LaneGuardError) as excinfo:
        await coordinate_mock_order_mutation(
            **{
                **_coordination_kwargs(
                    envelope,
                    lane_registry=bound,
                    persistence=stack["persistence"],
                    evidence=stack["evidence"],
                    intents=stack["intents"],
                    factory=stack["factory"],
                    callback=stack["callback"],
                ),
                "registry": LateSwitchingRegistry(),
            }
        )

    assert excinfo.value.code == "canonical_lane_identity_mismatch"
    assert posts == []
    # The unknown is durable and the claim for account A is retained.
    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_callback_scheduling_failure_takes_the_evidence_path(monkeypatch):
    """U8/B42: even ``ensure_future`` itself failing is post-send uncertainty."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    posts: list[str] = []
    real_ensure_future = asyncio.ensure_future
    armed = {"value": False}
    pending: dict[str, Any] = {}

    def one_shot_failure(coro_or_future: Any, **kwargs: Any) -> Any:
        if armed["value"]:
            armed["value"] = False
            if asyncio.iscoroutine(coro_or_future):
                coro_or_future.close()
            inner = pending.pop("inner", None)
            if asyncio.iscoroutine(inner):
                inner.close()
            raise RuntimeError("event loop refused to schedule")
        return real_ensure_future(coro_or_future, **kwargs)

    monkeypatch.setattr(coordination.asyncio, "ensure_future", one_shot_failure)

    async def sender(scope: Any) -> MutationCallbackResult:  # pragma: no cover
        return MutationCallbackResult(certainty=MutationCertainty.UNCERTAIN)

    def arming_callback(scope: Any) -> Any:
        posts.append("POST")  # a synchronous send already happened
        armed["value"] = True  # the scheduling of the rest now fails
        pending["inner"] = sender(scope)
        return pending["inner"]

    stack["callback"] = arming_callback
    with pytest.raises(RuntimeError, match="refused to schedule"):
        await _run(envelope, stack)

    assert posts == ["POST"]
    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert stack["persistence"].calls == 2
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_claim_synchronous_cancellation_takes_the_evidence_path():
    """U8/B42: a synchronous ``CancelledError`` is uncertainty, not a clean abort."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    posts: list[str] = []

    def sync_cancel(scope: Any) -> Any:
        posts.append("POST")
        raise asyncio.CancelledError()

    stack["callback"] = sync_cancel
    with pytest.raises(asyncio.CancelledError):
        await _run(envelope, stack)

    assert posts == ["POST"]
    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert stack["evidence"].only.certainty is MutationCertainty.UNCERTAIN
    assert stack["persistence"].calls == 2
    assert len(stack["intents"].rows) == 1


@pytest.mark.asyncio
async def test_lock_hold_history_and_active_view_never_disagree():
    """U9/B46: one hold id must not report True and False at the same instant."""

    space = FakeLockSpace()
    lease, connection, hold = await _durable_true_hold(space, pid=11301, key=1131)

    def flags(hold_id: str) -> tuple[Any, Any]:
        active = [h for h in unreleased_authority_holds() if h.hold_id == hold_id]
        history = [h for h in authority_hold_history() if h.hold_id == hold_id]
        assert active and history
        return active[-1].recoverable_in_process, history[-1].recoverable_in_process

    # While active and genuinely retryable, both views say the same thing.
    active_flag, history_flag = flags(hold.hold_id)
    assert active_flag is True
    assert history_flag == active_flag

    # After a proven release the hold leaves the active view; history keeps the
    # record and reports it as not retryable, which is now true.
    await lease.release(lease.grant)
    assert hold.hold_id not in {h.hold_id for h in unreleased_authority_holds()}
    resolved = [h for h in authority_hold_history() if h.hold_id == hold.hold_id]
    assert resolved and resolved[-1].recoverable_in_process is False
    assert connection.closed is True


# ===========================================================================
# §Scope and static safety
# ===========================================================================

_FORBIDDEN_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "aiohttp",
        "boto3",
        "certifi",
        "hmac",
        "httpcore",
        "httpx",
        "os",
        "requests",
        "socket",
        "ssl",
        "urllib",
        "urllib3",
        "websocket",
        "websockets",
    }
)

_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "app.core.config",
    "app.core.db",
    "app.models",
    "app.services.brokers",
    "app.services.kis_mock_runner",
    "app.tasks",
    "app.flows",
    "taskiq",
    "prefect",
    "celery",
    "apscheduler",
    "dotenv",
)


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_scope_module_imports_no_broker_transport_signing_or_credential_loader():
    for module in _imported_modules(COORDINATION_SOURCE):
        assert module.split(".")[0] not in _FORBIDDEN_IMPORT_ROOTS, module
        for prefix in _FORBIDDEN_IMPORT_PREFIXES:
            assert not module.startswith(prefix), module


def test_scope_test_file_imports_no_broker_or_network_surface():
    modules = _imported_modules(TEST_SOURCE)
    network_roots = _FORBIDDEN_IMPORT_ROOTS - {"os"}
    for module in modules:
        assert module.split(".")[0] not in network_roots, module
    # The only broker-namespaced import allowed is J2B's pure client-order-ID
    # constraint module: constants and a regex, no transport of any kind.
    broker_imports = {m for m in modules if m.startswith("app.services.brokers")}
    assert broker_imports <= {"app.services.brokers.client_order_ids"}


def test_scope_module_issues_only_read_and_lock_sql_never_a_ledger_write():
    source_text = COORDINATION_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    # Only the module's pinned ``*_SQL`` constants count as SQL; a docstring
    # that happens to begin with the word "Insert" is prose, not a statement.
    sql_literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not any(name.endswith("_SQL") for name in names):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            sql_literals.append(value.value.strip())
        elif isinstance(value, ast.JoinedStr | ast.BinOp):  # pragma: no cover
            raise AssertionError("SQL constants must stay plain literals")
    assert sql_literals, "expected the pinned lock statements to be present"
    for statement in sql_literals:
        assert statement.upper().startswith("SELECT"), statement
    upper_source = source_text.upper()
    for forbidden in ("INSERT INTO", "DELETE FROM", "UPDATE REVIEW.", "CREATE TABLE"):
        assert forbidden not in upper_source


# --- write fence: what this job wrote, not who may import it ----------------


def paths_within_write_fence(paths: object) -> bool:
    """The J3A write fence, as a pure predicate over changed paths."""

    considered = {
        path
        for path in paths  # type: ignore[union-attr]
        if not str(path).startswith(FENCE_EXEMPT_PREFIXES)
    }
    return considered <= set(J3A_WRITE_FENCE)


def parse_porcelain_z(payload: str) -> set[str]:
    """Parse ``git status --porcelain=v1 -z`` output into plain repository paths.

    Each record is ``XY <path>`` terminated by NUL, and a rename/copy record is
    followed by its origin path as a separate NUL-terminated field.  The two
    status characters plus one space are stripped **by position** — never by
    guessing from the command line that produced the output, which is how a
    legitimate ``?? app/...`` entry turns into a false red.  NUL delimiting also
    keeps spaces, quotes, and non-ASCII path bytes intact.
    """

    paths: set[str] = set()
    fields = [field for field in payload.split("\0") if field]
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        paths.add(path)
        if status[0] in {"R", "C"} and index < len(fields):
            paths.add(fields[index])
            index += 1
    return paths


def _git(*args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    proc = subprocess.run(
        [git, "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False
    )
    return None if proc.returncode != 0 else proc.stdout


def _changed_paths_since_base() -> set[str] | None:
    diff = _git("diff", "--name-only", "-z", f"{J3A_FENCE_BASE_SHA}..HEAD")
    status = _git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if diff is None or status is None:
        return None
    changed = {field for field in diff.split("\0") if field}
    changed |= parse_porcelain_z(status)
    return changed


def test_scope_porcelain_parser_normalizes_owned_untracked_paths():
    payload = "".join(
        f"{entry}\0"
        for entry in (
            "?? app/services/mock_integration/coordination.py",
            "?? tests/services/mock_integration/test_coordination.py",
            " M docs/contracts/rob-1262-coordination-port.md",
        )
    )
    parsed = parse_porcelain_z(payload)
    assert parsed == set(J3A_WRITE_FENCE)
    assert paths_within_write_fence(parsed) is True


def test_scope_porcelain_parser_keeps_out_of_fence_and_renamed_paths_visible():
    payload = "".join(
        f"{entry}\0"
        for entry in (
            "?? app/services/mock_lane_registry.py",
            "R  app/services/new_name.py",
            "app/services/old_name.py",
        )
    )
    parsed = parse_porcelain_z(payload)
    assert parsed == {
        "app/services/mock_lane_registry.py",
        "app/services/new_name.py",
        "app/services/old_name.py",
    }
    assert paths_within_write_fence(parsed) is False
    # A path with a space survives NUL parsing intact.
    assert parse_porcelain_z("?? docs/a file.md\0") == {"docs/a file.md"}


def test_scope_write_fence_predicate_reds_on_an_out_of_fence_path():
    assert paths_within_write_fence(set(J3A_WRITE_FENCE)) is True
    assert paths_within_write_fence({".smoke-out/rob179-feed-research-evidence.json"})
    for out_of_fence in (
        "app/services/order_send_intent_service.py",
        "app/services/mock_lane_registry.py",
        "app/services/mock_integration/lineage.py",
        "app/services/brokers/client_order_ids.py",
        "app/services/kis_mock_runner/singleton.py",
        "scripts/b0x/kr/run.py",
        "alembic/versions/deadbeef_add_coordination.py",
        "app/models/review.py",
    ):
        assert paths_within_write_fence({out_of_fence}) is False, out_of_fence


def test_scope_actual_job_diff_stays_inside_the_write_fence():
    changed = _changed_paths_since_base()
    if changed is None:
        pytest.skip("git is unavailable; the write fence is verified in the report")
    assert paths_within_write_fence(changed), sorted(
        path for path in changed if not paths_within_write_fence({path})
    )


@pytest.mark.asyncio
async def test_scope_authorized_future_consumer_may_import_and_use_the_port():
    """A later J3B/J3C integration is an approved consumer, not a fence breach.

    The fence governs what *this job wrote*; it must never make an authorized
    downstream import go red, because those lanes cannot edit this file.
    """

    from app.services.mock_integration.coordination import (  # noqa: PLC0415
        coordinate_mock_order_mutation as consumer_entrypoint,
    )

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    result = await consumer_entrypoint(
        **_coordination_kwargs(
            envelope,
            lane_registry=_bound_registry(envelope),
            persistence=stack["persistence"],
            evidence=stack["evidence"],
            intents=stack["intents"],
            factory=stack["factory"],
            callback=stack["callback"],
        ),
        # A broker lane supplies its own extra keys; J3A never chooses them.
        additional_advisory_keys=(4242,),
    )
    assert result.lease_keys == tuple(sorted({result.scope.advisory_key, 4242}))
    assert stack["evidence"].only.kind is DispatchEvidenceKind.ACKNOWLEDGED


def test_scope_lease_is_documented_as_not_broker_enforced_fencing_in_three_places():
    source = COORDINATION_SOURCE.read_text(encoding="utf-8")

    # 1. module documentation
    module_doc = ast.get_docstring(ast.parse(source)) or ""
    assert "NOT BROKER-ENFORCED FENCING" in module_doc

    # 2. source comment/docstring on the lease itself
    lease_doc = PostgresAdvisoryKeysetLease.__doc__ or ""
    assert "NOT BROKER-ENFORCED FENCING" in lease_doc
    assert (
        "not broker-enforced fencing" in NOT_BROKER_ENFORCED_FENCING_STATEMENT.lower()
    )

    # 3. lane matrix — every canonical lane, no exceptions
    assert set(LANE_FENCING_MATRIX) == set(registry.CANONICAL_LANE_IDS)
    assert set(LANE_FENCING_MATRIX.values()) == {FENCING_NOT_BROKER_ENFORCED}
    assert CONTRACT_DOC.exists()
    doc_text = CONTRACT_DOC.read_text(encoding="utf-8")
    assert "NOT broker-enforced fencing" in doc_text
    for lane_id in registry.CANONICAL_LANE_IDS:
        assert lane_id in doc_text
    with pytest.raises(TypeError):
        LANE_FENCING_MATRIX["kr.kis.mock"] = "broker_enforced"  # type: ignore[index]


# ===========================================================================
# Real PostgreSQL attestation — run-owned test database only
# ===========================================================================


async def _open_observer_connection() -> Any:
    from app.core import db

    return await db.engine.connect()


async def _open_run_owned_authority() -> SqlAlchemyLockAuthority:
    from app.core import db

    return SqlAlchemyLockAuthority(
        await db.engine.connect(), observer_factory=_open_observer_connection
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lock_real_postgres_termination_receipt_proves_the_backend_is_gone(
    db_session,
):
    """A real, independently observed termination receipt bound to the exact PID."""

    from sqlalchemy import text as sql_text

    from app.core import db

    lease = await acquire_physical_account_lease(
        keys=[-424242424242], connection_factory=_open_run_owned_authority
    )
    grant = lease.grant
    receipt = await lease._connection.terminate_backend_session(
        expected_pid=grant.backend_pid, owner_token=grant.connection_token
    )
    assert receipt.terminated is True
    assert receipt.backend_pid == grant.backend_pid
    assert receipt.owner_token == grant.connection_token

    # Independently confirm the backend really is gone, and with it the lock.
    observer = await db.engine.connect()
    try:
        alive = await observer.execute(
            sql_text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": grant.backend_pid},
        )
        assert int(alive.scalar_one()) == 0
    finally:
        await observer.close()

    successor = await acquire_physical_account_lease(
        keys=[-424242424242], connection_factory=_open_run_owned_authority
    )
    await successor.release(successor.grant)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lock_real_postgres_attests_a_negative_key_and_releases_exactly_once(
    db_session,
):
    _, envelope = _attempt_envelope()
    scope = physical_account_scope_for_entry(
        _fully_bound_entry(envelope, "kr.kis.mock")
    )
    lease = await acquire_physical_account_lease(
        keys=[scope.advisory_key], connection_factory=_open_run_owned_authority
    )
    assert lease.grant.keys == (scope.advisory_key,)
    assert lease.grant.backend_pid > 0
    assert lease.grant.database_oid > 0
    await lease.assert_owned(lease.grant)
    await lease.release(lease.grant)
    assert lease.unlocked_keys == (scope.advisory_key,)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lock_real_postgres_second_session_is_contended(db_session):
    _, envelope = _attempt_envelope(attempt_overrides={"cycle_id": "pg-contend"})
    scope = physical_account_scope_for_entry(
        _fully_bound_entry(
            envelope, "kr.kis.mock", physical_account_id="rob-1262-pg-contention"
        )
    )
    holder = await acquire_physical_account_lease(
        keys=[scope.advisory_key], connection_factory=_open_run_owned_authority
    )
    try:
        with pytest.raises(CoordinationError) as excinfo:
            await acquire_physical_account_lease(
                keys=[scope.advisory_key],
                connection_factory=_open_run_owned_authority,
            )
        assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED
    finally:
        await holder.release(holder.grant)

    successor = await acquire_physical_account_lease(
        keys=[scope.advisory_key], connection_factory=_open_run_owned_authority
    )
    await successor.release(successor.grant)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lock_real_postgres_multi_key_rollback_leaves_nothing_held(db_session):
    keys = [-987654321012345, 12345678901234]
    blocker = await acquire_physical_account_lease(
        keys=[keys[1]], connection_factory=_open_run_owned_authority
    )
    try:
        with pytest.raises(CoordinationError) as excinfo:
            await acquire_physical_account_lease(
                keys=keys, connection_factory=_open_run_owned_authority
            )
        assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED
        rolled_back = await acquire_physical_account_lease(
            keys=[keys[0]], connection_factory=_open_run_owned_authority
        )
        await rolled_back.release(rolled_back.grant)
    finally:
        await blocker.release(blocker.grant)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_real_order_send_intent_service_round_trip(db_session):
    _, envelope = _attempt_envelope(attempt_overrides={"cycle_id": "pg-claim"})
    scope = physical_account_scope_for_entry(
        _fully_bound_entry(
            envelope, "kr.kis.mock", physical_account_id="rob-1262-pg-claim"
        )
    )
    adapter = DurableSendClaimAdapter(OrderSendIntentService(db_session))
    idempotency_key = envelope.order_attempt.idempotency_key

    assert await adapter.account_has_unresolved_claim(scope) is False
    claim = await adapter.reserve(
        scope=scope, idempotency_key=idempotency_key, symbol="005930", side="buy"
    )
    try:
        assert await adapter.account_has_unresolved_claim(scope) is True
        with pytest.raises(CoordinationError) as excinfo:
            await adapter.reserve(
                scope=scope,
                idempotency_key=idempotency_key,
                symbol="005930",
                side="buy",
            )
        assert excinfo.value.reason_code is (
            CoordinationReasonCode.DURABLE_CLAIM_CONFLICT
        )
        with pytest.raises(CoordinationError):
            await adapter.release_with_terminal_evidence(
                claim, coordination.TerminalClaimEvidence()
            )
        assert await adapter.account_has_unresolved_claim(scope) is True
    finally:
        deleted = await adapter.release_with_terminal_evidence(
            claim,
            coordination.TerminalClaimEvidence(
                lane_native_terminal_evidence=True,
                account_position_reconciled=True,
                remainder_known=True,
            ),
        )
    assert deleted == 1
    assert await adapter.account_has_unresolved_claim(scope) is False


# ===========================================================================
# §V2–V5 — cross-generation ids, awaitable normalization, retention, AND gate
# ===========================================================================


@pytest.mark.asyncio
async def test_lock_hold_id_is_never_reused_after_its_generation_is_resolved(
    monkeypatch,
):
    """V2/B48: a retired opaque id must not be handed to a later owner.

    Collision refusal that only consults the *live* maps is not enough. Once a
    hold resolves it leaves those maps, so the next allocation with the same
    random token would reissue the retired id — and every stale reference to
    the old generation would silently resolve to a different account's owner.
    """

    space = FakeLockSpace()
    first_conn = FakeLockConnection(
        space, pid=9611, termination_raises=RuntimeError("cannot terminate")
    )
    first_lease = await acquire_physical_account_lease(
        keys=[9611], connection_factory=ConnectionFactory(first_conn)
    )
    first_conn._unlock_returns = False
    with pytest.raises(CoordinationError):
        await first_lease.release(first_lease.grant)
    first = first_lease.unreleased_authority_hold
    assert first is not None

    # Resolve that generation: it leaves every live map.
    first_conn._unlock_returns = None
    await first_lease.release(first_lease.grant)
    assert first.hold_id not in _retained_authorities()
    assert held_coordination(first.hold_id) is None

    # Now force the allocator to draw exactly the retired token again.
    monkeypatch.setattr(
        coordination.secrets,
        "token_hex",
        lambda _n: first.hold_id.removeprefix("hold:"),
    )
    second_conn = FakeLockConnection(
        space, pid=9612, termination_raises=RuntimeError("cannot terminate")
    )
    second_lease = await acquire_physical_account_lease(
        keys=[9612], connection_factory=ConnectionFactory(second_conn)
    )
    second_conn._unlock_returns = False
    with pytest.raises(CoordinationError):
        await second_lease.release(second_lease.grant)
    second = second_lease.unreleased_authority_hold
    assert second is not None

    # The new owner got a different id...
    assert second.hold_id != first.hold_id
    # ...the retired generation's rows never became recoverable...
    retired = [h for h in authority_hold_history() if h.hold_id == first.hold_id]
    assert retired
    assert all(row.recoverable_in_process is False for row in retired)
    # ...and a stale lookup of the retired id resolves to nobody, least of all
    # to the new owner.
    assert held_coordination(first.hold_id) is None
    assert first.hold_id not in _retained_authorities()
    assert _retained_authorities()[second.hold_id].connection is second_conn


@pytest.mark.asyncio
async def test_claim_hostile_completed_future_from_the_callback_is_contained():
    """V3/B49: the caller's awaitable is never the thing whose outcome we read.

    ``asyncio.ensure_future`` returns a *Future argument unchanged*, so reading
    the outcome off it means calling the caller's own ``result()``/
    ``exception()``. A callback that returns a hostile completed Future would
    then raise from outside the guarded region — after a POST already went out,
    leaving the send with no durable evidence at all. Normalizing through a
    module-owned task is what makes that unreachable, so this asserts the
    containment directly: those accessors are never invoked.
    """

    class HostileFuture(asyncio.Future):
        accesses = 0

        def result(self) -> Any:
            type(self).accesses += 1
            raise RuntimeError("hostile result access")

        def exception(self, *args: Any, **kwargs: Any) -> Any:
            type(self).accesses += 1
            raise RuntimeError("hostile exception access")

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    posts: list[str] = []

    def hostile_callback(scope: Any) -> Any:
        posts.append("POST")  # the broker already has it
        hostile = HostileFuture()
        hostile.set_result(
            MutationCallbackResult(
                certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO-0000009"
            )
        )
        return hostile

    stack["callback"] = hostile_callback
    held_before = {h.hold_id for h in unreleased_authority_holds()}
    result = await _run(envelope, stack)

    assert posts == ["POST"]
    assert HostileFuture.accesses == 0
    assert result.certainty is MutationCertainty.DEFINITIVE
    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.outer_cancellation_requested is False
    assert stack["connection"].closed is True
    assert {h.hold_id for h in unreleased_authority_holds()} == held_before


@pytest.mark.asyncio
async def test_claim_callback_that_cancels_itself_is_not_an_outer_cancellation():
    """V3/B49: an inner self-cancel is the callback's failure, not the caller's.

    Labelling it as an outer cancellation would tell the operator that somebody
    asked for this send to stop, when in fact the lane's own coroutine died
    mid-flight with the order already at the broker.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    posts: list[str] = []

    async def self_cancelling(scope: Any) -> Any:
        await scope.assert_owned()
        posts.append("POST")
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await asyncio.sleep(0)
        raise AssertionError("unreachable")

    stack["callback"] = self_cancelling
    held_before = {h.hold_id for h in unreleased_authority_holds()}
    with pytest.raises(asyncio.CancelledError):
        await _run(envelope, stack)

    assert posts == ["POST"]
    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1
    evidence = stack["evidence"].only
    assert evidence.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    assert evidence.outer_cancellation_requested is False
    assert {h.hold_id for h in unreleased_authority_holds()} == held_before


@pytest.mark.asyncio
async def test_claim_hostile_outcome_with_broken_durability_retains_the_authority():
    """V3/B49 × B1: no durable evidence means the account stays blocked."""

    class HostileFuture(asyncio.Future):
        def result(self) -> Any:
            raise RuntimeError("hostile result access")

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)

    def hostile_callback(scope: Any) -> Any:
        hostile = HostileFuture()
        hostile.set_result(
            MutationCallbackResult(certainty=MutationCertainty.DEFINITIVE)
        )
        return hostile

    stack["callback"] = hostile_callback
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert excinfo.value.hold_id == hold_id
    authority_hold = _held_coordination(hold_id).lease.unreleased_authority_hold
    assert authority_hold is not None
    assert authority_hold.durable_evidence_written is False
    assert stack["connection"].closed is False


@pytest.mark.asyncio
async def test_claim_self_cancelling_callback_with_broken_durability_is_retained():
    """V3/B49 × B1: same for the inner-cancellation branch."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)

    async def self_cancelling(scope: Any) -> Any:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await asyncio.sleep(0)
        raise AssertionError("unreachable")

    stack["callback"] = self_cancelling
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert excinfo.value.hold_id == hold_id
    authority_hold = _held_coordination(hold_id).lease.unreleased_authority_hold
    assert authority_hold is not None
    assert authority_hold.durable_evidence_written is False
    assert stack["connection"].closed is False


@pytest.mark.asyncio
async def test_lock_retained_rollback_authority_survives_losing_every_caller_ref():
    """V4/U6 (rollback branch): retention must be rooted, not incidental.

    If the only thing keeping the connection alive were the caller's local
    variable, the backend session would be collected and PostgreSQL would drop
    the advisory lock — silently unblocking an account we were unable to prove
    safe.
    """

    import gc

    space = FakeLockSpace()
    retained_before = set(_retained_authorities())
    connection = FakeLockConnection(
        space,
        pid=9701,
        raise_after_lock_on_key=9702,
        unlock_returns=False,
        termination_raises=RuntimeError("cannot terminate"),
    )
    with pytest.raises(RuntimeError):
        await acquire_physical_account_lease(
            keys=[9701, 9702], connection_factory=ConnectionFactory(connection)
        )
    new_holds = set(_retained_authorities()) - retained_before
    assert len(new_holds) == 1
    hold_id = new_holds.pop()

    del connection
    gc.collect()

    # Recoverable only through the private retained seam.
    retained = _retained_authorities()[hold_id]
    assert isinstance(retained.connection, FakeLockConnection)
    assert retained.connection.closed is False

    successor = FakeLockConnection(space, pid=9703)
    with pytest.raises(CoordinationError) as contended:
        await acquire_physical_account_lease(
            keys=[9701], connection_factory=ConnectionFactory(successor)
        )
    assert contended.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED


@pytest.mark.asyncio
async def test_lock_retained_release_failure_authority_survives_losing_every_ref():
    """V4/U6 (release-failure branch): same obligation, other retention path."""

    import gc

    space = FakeLockSpace()
    victim_lease, victim_conn, victim_hold = await _durable_true_hold(
        space, pid=9801, key=9801
    )
    victim_retained = _retained_authorities()[victim_hold.hold_id]

    lease, connection, hold = await _durable_true_hold(space, pid=9802, key=9802)
    del lease, connection, hold
    stuck = [
        hold_id
        for hold_id, entry in _retained_authorities().items()
        if hold_id != victim_hold.hold_id and entry.grant.keys == (9802,)
    ]
    assert len(stuck) == 1
    hold_id = stuck[0]
    gc.collect()

    retained = _retained_authorities()[hold_id]
    assert isinstance(retained.connection, FakeLockConnection)
    assert retained.connection.closed is False

    successor = FakeLockConnection(space, pid=9803)
    with pytest.raises(CoordinationError) as contended:
        await acquire_physical_account_lease(
            keys=[9802], connection_factory=ConnectionFactory(successor)
        )
    assert contended.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED

    # The foreign hold is untouched by any of it.
    assert _retained_authorities()[victim_hold.hold_id] is victim_retained
    assert victim_conn.closed is False
    assert victim_lease.unreleased_authority_hold == victim_hold


@pytest.mark.parametrize(
    "certainty", [MutationCertainty.DEFINITIVE, MutationCertainty.UNCERTAIN]
)
@pytest.mark.asyncio
async def test_claim_both_durable_writes_precede_any_cleanup(certainty):
    """V5/U8: the AND gate holds for every send outcome, not just the happy one."""

    _, envelope = _attempt_envelope()
    events: list[str] = []
    stack = _default_stack(events)
    stack["callback"] = RecordingCallback(
        events=events, result=MutationCallbackResult(certainty=certainty)
    )
    held_before = {h.hold_id for h in unreleased_authority_holds()}
    await _run(envelope, stack)

    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1
    # Both durable writes land, in order, strictly before the lease is given up.
    assert events.index("persist_post") < events.index("evidence")
    assert events.index("evidence") < events.index("lease_unlock")
    assert events.index("lease_unlock") < events.index("lease_closed")
    assert {h.hold_id for h in unreleased_authority_holds()} == held_before
    # The binary claim is the durable record of the send; nothing releases it.
    assert stack["intents"].rows != {}
    assert stack["intents"].release_if_matches_calls == []


@pytest.mark.parametrize(
    "certainty", [MutationCertainty.DEFINITIVE, MutationCertainty.UNCERTAIN]
)
@pytest.mark.asyncio
async def test_claim_second_lineage_persist_failure_halts_before_cleanup(certainty):
    """V5/U8: half the AND gate is not the AND gate."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["persistence"] = RecordingPersistence(fail_from_call=1)
    stack["callback"] = RecordingCallback(
        result=MutationCallbackResult(certainty=certainty)
    )
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert excinfo.value.hold_id == hold_id
    authority_hold = _held_coordination(hold_id).lease.unreleased_authority_hold
    assert authority_hold is not None
    assert authority_hold.durable_evidence_written is False
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert stack["intents"].rows != {}


@pytest.mark.parametrize(
    "certainty", [MutationCertainty.DEFINITIVE, MutationCertainty.UNCERTAIN]
)
@pytest.mark.asyncio
async def test_claim_dispatch_evidence_failure_halts_before_cleanup(certainty):
    """V5/U8: the other half, independently."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    stack["callback"] = RecordingCallback(
        result=MutationCallbackResult(certainty=certainty)
    )
    held_before = set(held_coordinations())
    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert excinfo.value.hold_id == hold_id
    authority_hold = _held_coordination(hold_id).lease.unreleased_authority_hold
    assert authority_hold is not None
    assert authority_hold.durable_evidence_written is False
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert stack["intents"].rows != {}


# ===========================================================================
# §B53 — a supplied hold id is a same-generation handoff, never a chosen id
# ===========================================================================


async def _resolved_failed_hold(space: FakeLockSpace, *, pid: int, key: int) -> str:
    """Create a durable-true hold, then resolve it, and return its retired id."""

    lease, connection, hold = await _durable_true_hold(space, pid=pid, key=key)
    connection._unlock_returns = None
    await lease.release(lease.grant)
    assert hold.hold_id not in _retained_authorities()
    assert held_coordination(hold.hold_id) is None
    return hold.hold_id


def _fresh_lease_for(space: FakeLockSpace, *, pid: int, key: int) -> Any:
    return FakeLockConnection(
        space, pid=pid, termination_raises=RuntimeError("cannot terminate")
    )


@pytest.mark.asyncio
async def test_lock_supplied_retired_failed_hold_id_is_refused_before_recording():
    """B53: a retired id supplied through the private seam is fail-closed.

    `_ISSUED_HOLD_IDS` is not an allocator property, it is an *id* property. A
    supplied id that skipped the allocator would otherwise walk straight around
    it, and the retired generation's history row would start reporting the new
    owner's reachability as its own.
    """

    space = FakeLockSpace()
    retired = await _resolved_failed_hold(space, pid=8801, key=8801)
    connection = _fresh_lease_for(space, pid=8802, key=8802)
    lease = await acquire_physical_account_lease(
        keys=[8802], connection_factory=ConnectionFactory(connection)
    )
    history_before = len(authority_hold_history())
    active_before = set(_retained_authorities())

    with pytest.raises(CoordinationError) as excinfo:
        lease._retain_authority(
            reason_code=CoordinationReasonCode.LEASE_LOST,
            durable_evidence_written=True,
            hold_id=retired,
        )
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    # The refusal happens *before* any record is written. A rejection that has
    # to be undone afterwards is not a rejection.
    assert len(authority_hold_history()) == history_before
    assert set(_retained_authorities()) == active_before
    assert retired not in _retained_authorities()


@pytest.mark.asyncio
async def test_lock_supplied_unissued_id_is_refused():
    """B53: an id the allocator never issued is not a handoff at all."""

    space = FakeLockSpace()
    connection = _fresh_lease_for(space, pid=8811, key=8811)
    lease = await acquire_physical_account_lease(
        keys=[8811], connection_factory=ConnectionFactory(connection)
    )
    history_before = len(authority_hold_history())

    with pytest.raises(CoordinationError) as excinfo:
        lease._retain_authority(
            reason_code=CoordinationReasonCode.LEASE_LOST,
            durable_evidence_written=True,
            hold_id="hold:deadbeefdeadbeef",
        )
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert len(authority_hold_history()) == history_before


@pytest.mark.asyncio
async def test_lock_completed_success_coordination_id_cannot_be_supplied_again():
    """B53: a *successful* coordination retires its id just as firmly."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    seen: dict[str, str] = {}
    before = set(held_coordinations())

    async def capture(scope: Any) -> Any:
        ids = set(held_coordinations()) - before
        assert len(ids) == 1
        seen["hold_id"] = ids.pop()
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO-0000011"
        )

    stack["callback"] = capture
    await _run(envelope, stack)
    completed = seen["hold_id"]
    # The successful coordination surrendered its handle...
    assert held_coordination(completed) is None

    space = FakeLockSpace()
    connection = _fresh_lease_for(space, pid=8821, key=8821)
    lease = await acquire_physical_account_lease(
        keys=[8821], connection_factory=ConnectionFactory(connection)
    )
    history_before = len(authority_hold_history())
    with pytest.raises(CoordinationError) as excinfo:
        lease._retain_authority(
            reason_code=CoordinationReasonCode.LEASE_LOST,
            durable_evidence_written=True,
            hold_id=completed,
        )
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    assert len(authority_hold_history()) == history_before


@pytest.mark.asyncio
async def test_lock_completed_success_coordination_id_is_never_reallocated(
    monkeypatch,
):
    """B53: issuing must be recorded at *allocation*, not only on failure.

    A defect that adds ids to the process-lifetime set only when a failed hold is
    recorded leaves every successful coordination id free to be drawn again.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    seen: dict[str, str] = {}
    before = set(held_coordinations())

    async def capture(scope: Any) -> Any:
        seen["hold_id"] = (set(held_coordinations()) - before).pop()
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO-0000012"
        )

    stack["callback"] = capture
    await _run(envelope, stack)
    completed = seen["hold_id"]

    # Force the allocator to draw exactly that retired token again.
    monkeypatch.setattr(
        coordination.secrets, "token_hex", lambda _n: completed.removeprefix("hold:")
    )
    space = FakeLockSpace()
    connection = _fresh_lease_for(space, pid=8831, key=8831)
    lease = await acquire_physical_account_lease(
        keys=[8831], connection_factory=ConnectionFactory(connection)
    )
    connection._unlock_returns = False
    with pytest.raises(CoordinationError):
        await lease.release(lease.grant)
    fresh = lease.unreleased_authority_hold
    assert fresh is not None
    assert fresh.hold_id != completed


@pytest.mark.asyncio
async def test_lock_legitimate_active_preallocation_handoff_stays_green():
    """B53: the supported case — one stuck account keeps one opaque id."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert excinfo.value.hold_id == hold_id
    authority_hold = _held_coordination(hold_id).lease.unreleased_authority_hold
    assert authority_hold is not None
    # The coordination handle and the authority record share the one id.
    assert authority_hold.hold_id == hold_id
    assert authority_hold.durable_evidence_written is False
    # Exactly one history row was written for that id, ever.
    assert len([h for h in authority_hold_history() if h.hold_id == hold_id]) == 1


@pytest.mark.asyncio
async def test_lock_retired_history_row_never_borrows_a_live_owners_truth():
    """B53: projection is bound to the exact active record, not to the id string.

    Deliberately white-box. The handoff gate above makes id aliasing unreachable
    from outside, so this constructs the state the projection guard exists to
    refuse rather than pretending a public path reaches it. It pins the
    invariant: if a later change ever lets one id name two generations, the older
    record must still not report the newer owner's reachability.
    """

    space = FakeLockSpace()
    lease, connection, live_hold = await _durable_true_hold(space, pid=8841, key=8841)
    raw_active = coordination._ACTIVE_AUTHORITY_HOLDS[live_hold.hold_id]
    assert _hold_view(raw_active).recoverable_in_process is True

    # A different record object carrying the same id — what aliasing would make.
    stale = replace(raw_active, recoverable_in_process=True)
    assert stale is not raw_active and stale.hold_id == raw_active.hold_id

    assert _hold_view(stale).recoverable_in_process is False
    # The genuine active record is unaffected by the question being asked.
    assert _hold_view(raw_active).recoverable_in_process is True
    assert _retained_authorities()[live_hold.hold_id].connection is connection
    assert lease.unreleased_authority_hold == _hold_view(raw_active)


# ===========================================================================
# §B54 — outer cancellation and outcome-access failure are independent facts
# ===========================================================================


async def _run_with_outer_cancel_and_failing_outcome(
    stack: dict[str, Any], monkeypatch: Any
) -> tuple[BaseException | None, Any]:
    """Cancel the coordinator, then make reading the callback outcome raise."""

    _, envelope = _attempt_envelope()
    gate, started = asyncio.Event(), asyncio.Event()
    stack["callback"] = RecordingCallback(gate=gate, started=started)

    def hostile_outcome(task: Any) -> Any:
        raise RuntimeError("custom Task outcome access failed")

    monkeypatch.setattr(coordination, "_callback_outcome", hostile_outcome)
    task = asyncio.ensure_future(_run(envelope, stack))
    await started.wait()
    task.cancel()  # a real outer cancellation
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    surfaced: BaseException | None = None
    try:
        await task
    except BaseException as exc:
        surfaced = exc
    return surfaced, stack["callback"].scopes[0]


@pytest.mark.asyncio
async def test_claim_outcome_access_failure_keeps_the_captured_outer_cancellation(
    monkeypatch,
):
    """B54: two independent facts, and neither may erase the other.

    ``_await_retained_task`` establishes whether the caller cancelled us. If
    reading the callback outcome then fails, containing that failure must replace
    the *result* only — writing ``outer_cancellation_requested=False`` into
    durable evidence for a send the caller demonstrably asked to stop is a lie
    about the one thing an operator needs.
    """

    stack = _default_stack()
    surfaced, scope = await _run_with_outer_cancel_and_failing_outcome(
        stack, monkeypatch
    )

    # Precedence: outcome error outranks the outer cancellation, so the
    # CancelledError does not cover it.
    assert isinstance(surfaced, RuntimeError)
    assert "outcome access failed" in str(surfaced)

    # Both durable writes closed before anything was surrendered.
    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1
    evidence = stack["evidence"].only
    assert evidence.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert evidence.certainty is MutationCertainty.UNCERTAIN
    assert evidence.outer_cancellation_requested is True

    # The captured scope is permanently dead.
    with pytest.raises(CoordinationError) as expired:
        await scope.assert_owned()
    assert expired.value.reason_code is CoordinationReasonCode.LEASE_LOST


@pytest.mark.asyncio
async def test_claim_scope_is_dead_after_a_self_cancelling_callback():
    """B54: the self-cancel branch expires the scope too, not only the happy path."""

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    captured: dict[str, Any] = {}
    posts: list[str] = []

    async def self_cancelling(scope: Any) -> Any:
        captured["scope"] = scope
        await scope.assert_owned()
        posts.append("POST")
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await asyncio.sleep(0)
        raise AssertionError("unreachable")

    stack["callback"] = self_cancelling
    with pytest.raises(asyncio.CancelledError):
        await _run(envelope, stack)

    assert posts == ["POST"]
    assert stack["evidence"].only.outer_cancellation_requested is False
    with pytest.raises(CoordinationError) as expired:
        await captured["scope"].assert_owned()
    assert expired.value.reason_code is CoordinationReasonCode.LEASE_LOST


@pytest.mark.parametrize("failing", ["lineage", "evidence"])
@pytest.mark.asyncio
async def test_claim_outcome_access_failure_durable_write_precedence(
    failing, monkeypatch
):
    """B54: a durable-write failure outranks the outcome error, and holds fast."""

    stack = _default_stack()
    if failing == "lineage":
        stack["persistence"] = RecordingPersistence(fail_from_call=1)
    else:
        stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())

    surfaced, scope = await _run_with_outer_cancel_and_failing_outcome(
        stack, monkeypatch
    )

    # Precedence: lineage_persistence_unavailable beats the outcome error, which
    # in turn beats the outer cancellation.
    assert isinstance(surfaced, CoordinationError)
    assert surfaced.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )

    # Both writes were attempted; one failing never skips the other.
    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1

    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert surfaced.hold_id == hold_id
    held = _held_coordination(hold_id)
    authority_hold = held.lease.unreleased_authority_hold
    assert authority_hold is not None
    # Same opaque id on both records, durable-false, and nothing cleaned up.
    assert authority_hold.hold_id == hold_id
    assert authority_hold.durable_evidence_written is False
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert stack["intents"].rows != {}
    assert stack["intents"].release_if_matches_calls == []

    # The public handle still refuses to release it.
    with pytest.raises(CoordinationError) as refused:
        await held.lease.release(held.grant)
    assert refused.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )

    with pytest.raises(CoordinationError):
        await scope.assert_owned()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["lineage", "evidence"])
@pytest.mark.asyncio
async def test_claim_self_cancelled_callback_retained_across_both_write_failures(
    failing,
):
    """W3/B54: the self-cancel case crossed with *each* durable-write failure.

    Hard-coding one failure kind leaves the other half of this exact case
    unproven. And on every path that releases the lease, a captured scope fails
    anyway because the lease itself refuses — so asserting a dead scope only
    proves something about the gate here, where the authority is deliberately
    retained and the lease is *not* released.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    if failing == "lineage":
        stack["persistence"] = RecordingPersistence(fail_from_call=1)
    else:
        stack["evidence"] = RecordingDispatchEvidence(fail=True)
    captured: dict[str, Any] = {}
    posts: list[str] = []
    held_before = set(held_coordinations())

    async def self_cancelling(scope: Any) -> Any:
        captured["scope"] = scope
        await scope.assert_owned()
        posts.append("POST")
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await asyncio.sleep(0)
        raise AssertionError("unreachable")

    stack["callback"] = self_cancelling
    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert posts == ["POST"]
    # Precedence: the durable failure outranks the callback self-cancellation.
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    # Both post-send writes were attempted; one failing never skips the other.
    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1

    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert excinfo.value.hold_id == hold_id
    held = _held_coordination(hold_id)
    authority_hold = held.lease.unreleased_authority_hold
    assert authority_hold is not None
    # One opaque id on both records, durable-false, claim kept, zero cleanup.
    assert authority_hold.hold_id == hold_id
    assert authority_hold.durable_evidence_written is False
    assert len(stack["intents"].rows) == 1
    assert stack["intents"].release_if_matches_calls == []
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False

    # The lease is retained, so it would still attest — the scope is dead only
    # because the gate closed it.
    assert held.lease.released is False
    await held.lease.assert_owned(held.grant)
    with pytest.raises(CoordinationError) as expired:
        await captured["scope"].assert_owned()
    assert expired.value.reason_code is CoordinationReasonCode.LEASE_LOST

    # Both the public handle and the exact private capability refuse release.
    for attempt in ("public", "private"):
        with pytest.raises(CoordinationError) as refused:
            if attempt == "public":
                await held.lease.release(held.grant)
            else:
                await held.lease._release_with_capability(
                    held.grant,
                    coordination._COORDINATION_RELEASE_CAPABILITIES[hold_id],
                )
        assert refused.value.reason_code is (
            CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
        ), attempt
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert held_coordination(hold_id) is not None


# ===========================================================================
# §B56 — the two exact post-send stimuli crossed with all three outcomes
# ===========================================================================


def _install_scheduling_failure_stimulus(
    stack: dict[str, Any], monkeypatch: Any, posts: list[str]
) -> type[BaseException]:
    """Stimulus 1: a synchronous POST, then ``ensure_future`` itself refuses."""

    real_ensure_future = asyncio.ensure_future
    armed = {"value": False}
    pending: dict[str, Any] = {}

    def one_shot_failure(coro_or_future: Any, **kwargs: Any) -> Any:
        if armed["value"]:
            armed["value"] = False
            if asyncio.iscoroutine(coro_or_future):
                coro_or_future.close()
            inner = pending.pop("inner", None)
            if asyncio.iscoroutine(inner):
                inner.close()
            raise RuntimeError("event loop refused to schedule")
        return real_ensure_future(coro_or_future, **kwargs)

    monkeypatch.setattr(coordination.asyncio, "ensure_future", one_shot_failure)

    async def sender(scope: Any) -> MutationCallbackResult:  # pragma: no cover
        return MutationCallbackResult(certainty=MutationCertainty.UNCERTAIN)

    def arming_callback(scope: Any) -> Any:
        posts.append("POST")
        armed["value"] = True
        pending["inner"] = sender(scope)
        return pending["inner"]

    stack["callback"] = arming_callback
    return RuntimeError


def _install_sync_cancellation_stimulus(
    stack: dict[str, Any], monkeypatch: Any, posts: list[str]
) -> type[BaseException]:
    """Stimulus 2: a synchronous POST, then a synchronous ``CancelledError``."""

    def sync_cancel(scope: Any) -> Any:
        posts.append("POST")
        raise asyncio.CancelledError()

    stack["callback"] = sync_cancel
    return asyncio.CancelledError


_B56_STIMULI = {
    "scheduling_failure": _install_scheduling_failure_stimulus,
    "sync_cancellation": _install_sync_cancellation_stimulus,
}


@pytest.mark.parametrize("stimulus", sorted(_B56_STIMULI))
@pytest.mark.asyncio
async def test_claim_stimulus_success_orders_both_writes_before_any_cleanup(
    stimulus, monkeypatch
):
    """B56: for each exact stimulus, prove the *post-send* write ordering.

    Asserting on the first ``persist`` event proves nothing: that is the pre-send
    write, and it is already ordered before everything. The mandatory post-send
    write is a separate event, and it is the one that must precede cleanup.
    """

    _, envelope = _attempt_envelope()
    events: list[str] = []
    stack = _default_stack(events)
    posts: list[str] = []
    captured: dict[str, Any] = {}
    registered_at: dict[str, bool] = {}
    held_before = set(held_coordinations())

    # W5: "remove" only fires if the handle genuinely left the map, and the
    # observer records whether it was still registered at each physical release
    # boundary. Without that, a defect that deletes the entry *before* releasing
    # still produces the same event sequence.
    def observe(step: str) -> None:
        hold_id = captured.get("hold_id")
        if hold_id is not None:
            registered_at[step] = (
                hold_id in coordination._HELD_COORDINATION,
                hold_id in coordination._COORDINATION_RELEASE_CAPABILITIES,
            )

    stack["connection"]._release_observer = observe
    real_release = coordination._release_and_unregister

    async def removing(held: Any) -> None:
        captured["hold_id"] = held.hold_id
        await real_release(held)
        assert held.hold_id not in coordination._HELD_COORDINATION

    monkeypatch.setattr(coordination, "_release_and_unregister", removing)
    expected = _B56_STIMULI[stimulus](stack, monkeypatch, posts)

    with pytest.raises(expected):
        await _run(envelope, stack)

    assert posts == ["POST"]
    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.kind is DispatchEvidenceKind.CALLBACK_FAILED
    assert stack["evidence"].only.certainty is MutationCertainty.UNCERTAIN

    # The exact post-send order, anchored on the *second* lineage write, and
    # carried all the way through removal.
    order = [
        events.index("persist_post"),
        events.index("evidence"),
        events.index("lease_unlock"),
        events.index("lease_closed"),
    ]
    assert order == sorted(order)
    assert events.index("persist_pre") < events.index("persist_post")

    # r30: the order is persist_post < evidence < unlock < remove < close.
    # Removal happens on a PROVEN reverse unlock and *before* the fallible
    # close — a close failure must never turn an already-proven unlock back into
    # a false active hold (B23). Both maps are observed at both boundaries:
    #   at unlock  -> the exact handle and its capability are still present, so
    #                 removal cannot precede the proof;
    #   at close   -> both are already gone, so removal is neither skipped,
    #                 deferred behind a fallible step, nor half-done.
    assert registered_at == {
        "lease_unlock": (True, True),
        "lease_closed": (False, False),
    }

    # The handle and its private release capability are both gone.
    hold_id = captured["hold_id"]
    assert held_coordination(hold_id) is None
    assert hold_id not in coordination._HELD_COORDINATION
    assert hold_id not in coordination._COORDINATION_RELEASE_CAPABILITIES
    assert set(held_coordinations()) == held_before

    # The durable claim is never released by coordination.
    assert len(stack["intents"].rows) == 1
    assert stack["intents"].release_if_matches_calls == []


@pytest.mark.parametrize("failing", ["lineage", "evidence"])
@pytest.mark.parametrize("stimulus", sorted(_B56_STIMULI))
@pytest.mark.asyncio
async def test_claim_stimulus_durable_write_failure_locks_the_account(
    stimulus, failing, monkeypatch
):
    """B56: each stimulus crossed with each individual durable-write failure.

    A stimulus-specific early release, or a short-circuit that skips the
    dispatch-evidence attempt once the second lineage write failed, is invisible
    to a success-only stimulus test and to a stimulus-free failure test.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    if failing == "lineage":
        stack["persistence"] = RecordingPersistence(fail_from_call=1)
    else:
        stack["evidence"] = RecordingDispatchEvidence(fail=True)
    posts: list[str] = []
    _B56_STIMULI[stimulus](stack, monkeypatch, posts)
    held_before = set(held_coordinations())

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    assert posts == ["POST"]
    # The durable-write failure outranks the stimulus error.
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    # Both writes were attempted; one failing never skips the other.
    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1

    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert excinfo.value.hold_id == hold_id
    held = _held_coordination(hold_id)
    authority_hold = held.lease.unreleased_authority_hold
    assert authority_hold is not None
    # One opaque id across both records, durable-false, and zero cleanup.
    assert authority_hold.hold_id == hold_id
    assert authority_hold.durable_evidence_written is False
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert len(stack["intents"].rows) == 1
    assert stack["intents"].release_if_matches_calls == []
    assert held_coordination(hold_id) is not None

    # W5: refusal must hold for the *exact private capability* as well, not only
    # for the generic public handle — that private path is the one an owning
    # coordination would actually use.
    before_refusals = (
        dict(_retained_authorities()),
        dict(_active_holds()),
        dict(stack["intents"].rows),
    )
    capability = coordination._COORDINATION_RELEASE_CAPABILITIES[hold_id]
    for attempt in ("public", "private"):
        with pytest.raises(CoordinationError) as refusal:
            if attempt == "public":
                await held.lease.release(held.grant)
            else:
                await held.lease._release_with_capability(held.grant, capability)
        assert refusal.value.reason_code is (
            CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
        ), attempt
    # Nothing moved across either refusal.
    assert dict(_retained_authorities()) == before_refusals[0]
    assert dict(_active_holds()) == before_refusals[1]
    assert dict(stack["intents"].rows) == before_refusals[2]
    assert stack["connection"].unlock_calls == []
    assert stack["connection"].closed is False
    assert held_coordination(hold_id) is not None


# ===========================================================================
# §W2 — the supplied-ID handoff binds lease, grant, connection, and capability
# ===========================================================================


async def _live_preallocated_handle(space: FakeLockSpace, *, pid: int, key: int) -> Any:
    """A registered coordination handle whose id has no authority record yet."""

    connection = FakeLockConnection(
        space, pid=pid, termination_raises=RuntimeError("cannot terminate")
    )
    lease = await acquire_physical_account_lease(
        keys=[key], connection_factory=ConnectionFactory(connection)
    )
    _, envelope = _attempt_envelope()
    claim = coordination.DurableClaim(
        row_id=1,
        claim_account_scope="mockpa:v1:w2",
        idempotency_key="w2-key",
        side="buy",
    )
    held = coordination._register_held_coordination(
        lease=lease, grant=lease.grant, claim=claim, envelope=envelope
    )
    capability = coordination._COORDINATION_RELEASE_CAPABILITIES[held.hold_id]
    return lease, connection, held, capability


def _exact_state() -> dict[str, Any]:
    """Every map and the history list, captured by object identity and order.

    r32 X1/X3: counts cannot detect replacement of a row by an equal-but-
    different object, so nothing here is a length.
    """

    return {
        "history": list(coordination._AUTHORITY_HOLD_HISTORY),
        "active": dict(_active_holds()),
        "retained": dict(_retained_authorities()),
        "held": dict(coordination._HELD_COORDINATION),
        "capabilities": dict(coordination._COORDINATION_RELEASE_CAPABILITIES),
    }


def _assert_exact_state(before: dict[str, Any], label: str) -> None:
    after = _exact_state()
    assert [id(h) for h in after["history"]] == [id(h) for h in before["history"]], (
        f"{label}: history rows changed by object or order"
    )
    for key in ("active", "retained", "held", "capabilities"):
        assert set(after[key]) == set(before[key]), f"{label}: {key} keys changed"
        for k in before[key]:
            assert after[key][k] is before[key][k], f"{label}: {key}[{k}] not identical"


def _record_state() -> tuple[int, dict[str, Any], dict[str, Any]]:
    return (
        len(authority_hold_history()),
        dict(_retained_authorities()),
        dict(_active_holds()),
    )


@pytest.mark.asyncio
async def test_lock_supplied_id_requires_every_exact_identity():
    """X1: an id is not a right — lease, grant, connection and capability all bind.

    Each cell below is a *first* transition on a genuinely live, genuinely issued
    coordination id, so each isolates exactly one identity condition. Every
    rejection is asserted against an exact object-level snapshot of history,
    active, retained, held and capability state, because a count cannot see a row
    being replaced by an equal-but-different object.
    """

    space = FakeLockSpace()
    lease, connection, held, capability = await _live_preallocated_handle(
        space, pid=7301, key=7301
    )
    other_conn = FakeLockConnection(space, pid=7302)
    other_lease = await acquire_physical_account_lease(
        keys=[7302], connection_factory=ConnectionFactory(other_conn)
    )
    foreign_capability = object()

    cases = {
        # r32 X1: a missing connection is a rejection, not shorthand. It would
        # otherwise append history and create an active row while writing no
        # retained root — an authority nobody is holding.
        "missing_connection": {
            "grant": held.grant,
            "connection": None,
            "capability": capability,
        },
        "wrong_connection": {
            "grant": held.grant,
            "connection": other_conn,
            "capability": capability,
        },
        "wrong_grant": {
            "grant": other_lease.grant,
            "connection": connection,
            "capability": capability,
        },
        "equal_copy_grant": {
            "grant": replace(held.grant),
            "connection": connection,
            "capability": capability,
        },
        "missing_capability": {
            "grant": held.grant,
            "connection": connection,
            "capability": None,
        },
        "foreign_capability": {
            "grant": held.grant,
            "connection": connection,
            "capability": foreign_capability,
        },
    }
    for name, kwargs in cases.items():
        before = _exact_state()
        with pytest.raises(CoordinationError) as excinfo:
            coordination._record_unreleased_authority(
                kwargs["grant"],
                owner=lease,
                reason_code=CoordinationReasonCode.LEASE_LOST,
                termination_proven=False,
                durable_evidence_written=True,
                connection=kwargs["connection"],
                hold_id=held.hold_id,
                capability=kwargs["capability"],
            )
        assert excinfo.value.reason_code is (
            CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
        ), name
        _assert_exact_state(before, name)

    # The legitimate exact handoff is still accepted, once.
    before = _exact_state()
    hold = coordination._record_unreleased_authority(
        held.grant,
        owner=lease,
        reason_code=CoordinationReasonCode.LEASE_LOST,
        termination_proven=False,
        durable_evidence_written=True,
        connection=connection,
        hold_id=held.hold_id,
        capability=capability,
    )
    assert hold.hold_id == held.hold_id
    assert len(coordination._AUTHORITY_HOLD_HISTORY) == len(before["history"]) + 1
    assert _retained_authorities()[held.hold_id].connection is connection

    # A second transition on the same id is refused: it is no longer the first.
    before = _exact_state()
    with pytest.raises(CoordinationError):
        coordination._record_unreleased_authority(
            held.grant,
            owner=lease,
            reason_code=CoordinationReasonCode.LEASE_LOST,
            termination_proven=False,
            durable_evidence_written=True,
            connection=connection,
            hold_id=held.hold_id,
            capability=capability,
        )
    _assert_exact_state(before, "second_transition")


@pytest.mark.asyncio
async def test_lock_same_owner_retired_id_cannot_be_reused_for_a_second_handoff():
    """X1 cell 6: resolving the first record does not re-open the id.

    The live preallocated handle is deliberately kept, so the only thing that
    changed is that this id already carried an authority record once. That alone
    must close it forever — otherwise the same owner could recycle its own id and
    the retired generation's history row would start describing a new authority.
    """

    space = FakeLockSpace()
    lease, connection, held, capability = await _live_preallocated_handle(
        space, pid=7311, key=7311
    )
    first = coordination._record_unreleased_authority(
        held.grant,
        owner=lease,
        reason_code=CoordinationReasonCode.LEASE_LOST,
        termination_proven=False,
        durable_evidence_written=True,
        connection=connection,
        hold_id=held.hold_id,
        capability=capability,
    )
    # Retire only the *authority* record. `_resolve_authority_hold` also
    # unregisters the coordination handle, which would turn this into a
    # missing-handle rejection instead of the historical-reuse rejection under
    # test. So the two authority maps are cleared directly and the live
    # preallocated handle is deliberately left in place — exactly the state r32
    # describes, and the only thing that has changed about this id is that it
    # once carried an authority record.
    del coordination._ACTIVE_AUTHORITY_HOLDS[first.hold_id]
    del coordination._RETAINED_AUTHORITIES[first.hold_id]
    assert held.hold_id not in _active_holds()
    assert held.hold_id not in _retained_authorities()
    assert coordination._HELD_COORDINATION[held.hold_id] is held
    assert held.hold_id in coordination._COORDINATION_RELEASE_CAPABILITIES
    assert any(h.hold_id == held.hold_id for h in coordination._AUTHORITY_HOLD_HISTORY)

    before = _exact_state()
    with pytest.raises(CoordinationError) as excinfo:
        coordination._record_unreleased_authority(
            held.grant,
            owner=lease,
            reason_code=CoordinationReasonCode.LEASE_LOST,
            termination_proven=False,
            durable_evidence_written=True,
            connection=connection,
            hold_id=held.hold_id,
            capability=capability,
        )
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE
    )
    _assert_exact_state(before, "same_owner_retired_reuse")


@pytest.mark.asyncio
async def test_lock_sealed_release_failure_supplies_its_real_capability():
    """W2: the sealed path must satisfy the record gate, not bypass it.

    `_terminate_or_hold` shares the coordination id so one stuck account keeps
    one thread. If it did not also supply the capability it was sealed with, the
    record gate would have to accept `capability=None` — and then any caller
    holding only the id could take the handoff.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["evidence"] = RecordingDispatchEvidence(fail=True)
    held_before = set(held_coordinations())

    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    # If the sealed path stopped supplying its capability, the record gate would
    # refuse the handoff and this would surface as lock_authority_unavailable
    # instead — so the reason code is the load-bearing assertion here.
    assert excinfo.value.reason_code is (
        CoordinationReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE
    )
    stuck = set(held_coordinations()) - held_before
    assert len(stuck) == 1
    hold_id = stuck.pop()
    assert excinfo.value.hold_id == hold_id
    held = _held_coordination(hold_id)
    authority_hold = held.lease.unreleased_authority_hold
    assert authority_hold is not None
    # The id was shared, which is only possible if the capability matched.
    assert authority_hold.hold_id == hold_id
    assert authority_hold.durable_evidence_written is False
    assert _retained_authorities()[hold_id].connection is stack["connection"]


@pytest.mark.asyncio
async def test_lock_duplicate_guard_discriminates_connection_grant_and_record():
    """W4: owner+grant is not an authority. The connection and the active record count.

    `_authority_already_retained` decides whether the outer fallback may skip
    recording. If it says "already retained" for a row describing a *different
    physical session*, the real connection is left unrooted while the maps claim
    the account is covered — the exact failure the fallback exists to prevent.
    """

    space = FakeLockSpace()
    connection = FakeLockConnection(space, pid=7401)
    lease = await acquire_physical_account_lease(
        keys=[7401], connection_factory=ConnectionFactory(connection)
    )
    other = FakeLockConnection(space, pid=7402)

    # A row with the right owner and grant but the WRONG connection.
    coordination._record_unreleased_authority(
        lease.grant,
        owner=connection,
        reason_code=CoordinationReasonCode.LEASE_LOST,
        termination_proven=False,
        durable_evidence_written=True,
        connection=other,
    )
    assert (
        coordination._authority_already_retained(
            owner=connection, grant=lease.grant, connection=connection
        )
        is False
    )
    # ...and it does answer True for the row that really is this authority.
    hold = coordination._record_unreleased_authority(
        lease.grant,
        owner=connection,
        reason_code=CoordinationReasonCode.LEASE_LOST,
        termination_proven=False,
        durable_evidence_written=True,
        connection=connection,
    )
    assert (
        coordination._authority_already_retained(
            owner=connection, grant=lease.grant, connection=connection
        )
        is True
    )

    # An equal-but-not-identical grant is a different acquisition.
    equal_copy = replace(lease.grant)
    assert equal_copy == lease.grant and equal_copy is not lease.grant
    assert (
        coordination._authority_already_retained(
            owner=connection, grant=equal_copy, connection=connection
        )
        is False
    )

    # X2: an *impostor* active row — equal by value, not the exact object the
    # entry was written with, and not any history object. Presence, equality, or
    # a matching id string would all accept it and suppress the fallback that
    # roots the real authority.
    genuine = _active_holds()[hold.hold_id]
    impostor = replace(genuine)
    assert impostor == genuine and impostor is not genuine
    assert not any(h is impostor for h in coordination._AUTHORITY_HOLD_HISTORY)
    coordination._ACTIVE_AUTHORITY_HOLDS[hold.hold_id] = impostor
    assert (
        coordination._authority_already_retained(
            owner=connection, grant=lease.grant, connection=connection
        )
        is False
    )
    # Restoring the exact record makes it true again, so the difference is the
    # identity of the row and nothing else.
    coordination._ACTIVE_AUTHORITY_HOLDS[hold.hold_id] = genuine
    assert (
        coordination._authority_already_retained(
            owner=connection, grant=lease.grant, connection=connection
        )
        is True
    )

    # A retained row whose active record is gone is not a recorded authority.
    del coordination._ACTIVE_AUTHORITY_HOLDS[hold.hold_id]
    assert (
        coordination._authority_already_retained(
            owner=connection, grant=lease.grant, connection=connection
        )
        is False
    )


@pytest.mark.asyncio
async def test_lock_rollback_retaining_a_wrong_connection_does_not_suppress_fallback(
    monkeypatch,
):
    """W4: a helper that retains the wrong session must not disarm the fallback."""

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=11401)
    await acquire_physical_account_lease(
        keys=[1142], connection_factory=ConnectionFactory(blocker)
    )
    connection = FakeLockConnection(
        space, pid=11402, termination_raises=RuntimeError("cannot terminate")
    )
    impostor = FakeLockConnection(space, pid=11403)

    async def retain_wrong_then_explode(
        conn: Any, acquired: Any, grant: Any, *, in_flight: Any = ()
    ) -> None:
        coordination._record_unreleased_authority(
            grant,
            owner=conn,
            reason_code=CoordinationReasonCode.LEASE_LOST,
            termination_proven=False,
            durable_evidence_written=True,
            connection=impostor,  # a DIFFERENT physical session
        )
        raise RuntimeError("rollback failed after retaining the wrong session")

    monkeypatch.setattr(
        coordination, "_rollback_partial_acquisition", retain_wrong_then_explode
    )
    retained_before = set(_retained_authorities())

    with pytest.raises(CoordinationError):
        await acquire_physical_account_lease(
            keys=[1141, 1142], connection_factory=ConnectionFactory(connection)
        )

    new_holds = set(_retained_authorities()) - retained_before
    # Two records: the impostor row the helper wrote, and the fallback's own row
    # for the connection that actually holds the key.
    assert len(new_holds) == 2
    rooted = [
        _retained_authorities()[h]
        for h in new_holds
        if _retained_authorities()[h].connection is connection
    ]
    assert len(rooted) == 1
    assert connection.closed is False


@pytest.mark.asyncio
async def test_claim_sealed_release_failure_records_under_the_coordination_id():
    """W2: `_terminate_or_hold` must satisfy the record gate, not bypass it.

    This is the *release-failure* path, not the durable-write-failure path: the
    two post-send writes land, the coordination-owned release then cannot prove
    a full reverse unlock, and the lease records its hold under the coordination
    id so one stuck account keeps one thread. That handoff is only permitted if
    the exact sealed capability is supplied with it.
    """

    _, envelope = _attempt_envelope()
    stack = _default_stack()
    stack["connection"]._unlock_returns = False
    stack["connection"]._termination_raises = RuntimeError("cannot terminate")
    seen: dict[str, str] = {}
    before = set(held_coordinations())

    async def capture(scope: Any) -> Any:
        seen["hold_id"] = (set(held_coordinations()) - before).pop()
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO-0000021"
        )

    stack["callback"] = capture
    with pytest.raises(CoordinationError) as excinfo:
        await _run(envelope, stack)

    coordination_id = seen["hold_id"]
    # Both durable writes landed; the failure is the release itself.
    assert stack["persistence"].calls == 2
    assert stack["evidence"].calls == 1
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST

    # The authority hold shares the coordination id — only possible if the
    # exact sealed capability was handed to the record gate.
    rows = [h for h in authority_hold_history() if h.hold_id == coordination_id]
    assert len(rows) == 1
    assert coordination_id in _retained_authorities()
    assert _retained_authorities()[coordination_id].connection is stack["connection"]
    assert stack["connection"].closed is False


@pytest.mark.asyncio
async def test_lock_rollback_that_retains_then_raises_makes_only_one_hold(monkeypatch):
    """B55/W4: the outer fallback must not double-count one stuck account.

    The helper can retain the exact authority and *then* fail on its way out. If
    the caller's fallback records unconditionally, one stuck account appears
    twice, and every count that follows it — holds, successors, operator triage —
    is wrong.
    """

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=11301)
    await acquire_physical_account_lease(
        keys=[1132], connection_factory=ConnectionFactory(blocker)
    )
    connection = FakeLockConnection(
        space, pid=11302, termination_raises=RuntimeError("cannot terminate")
    )

    async def retain_then_explode(
        conn: Any, acquired: Any, grant: Any, *, in_flight: Any = ()
    ) -> None:
        coordination._record_unreleased_authority(
            grant,
            owner=conn,
            reason_code=CoordinationReasonCode.LEASE_LOST,
            termination_proven=False,
            durable_evidence_written=True,
            connection=conn,
        )
        raise RuntimeError("rollback failed after retaining")

    monkeypatch.setattr(
        coordination, "_rollback_partial_acquisition", retain_then_explode
    )
    retained_before = set(_retained_authorities())

    with pytest.raises(CoordinationError):
        await acquire_physical_account_lease(
            keys=[1131, 1132], connection_factory=ConnectionFactory(connection)
        )

    new_holds = set(_retained_authorities()) - retained_before
    # Exactly one — the helper's record, adopted rather than duplicated.
    assert len(new_holds) == 1
    hold_id = new_holds.pop()
    retained = _retained_authorities()[hold_id]
    assert retained.connection is connection
    assert connection.closed is False
    # And exactly one history row for it, not two.
    assert len([h for h in authority_hold_history() if h.hold_id == hold_id]) == 1
