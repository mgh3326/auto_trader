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
    DispatchEvidence,
    DispatchEvidenceKind,
    DispatchEvidencePort,
    DurableSendClaimAdapter,
    HeldCoordination,
    MutationCallbackResult,
    MutationCertainty,
    OrderSendIntentReservationPort,
    PostgresAdvisoryKeysetLease,
    SqlAlchemyLockAuthority,
    acquire_physical_account_lease,
    authority_hold_history,
    coordinate_mock_order_mutation,
    describe_claim_followup,
    held_coordination,
    held_coordinations,
    ordered_advisory_keyset,
    physical_account_scope_for_entry,
    require_dispatch_evidence_port,
    retained_authority_connections,
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

    def try_lock(self, key: int, pid: int) -> bool:
        owner = self.held.get(key)
        if owner is None or owner == pid:
            self.held[key] = pid
            return True
        return False

    def unlock(self, key: int, pid: int) -> bool:
        if self.held.get(key) == pid:
            del self.held[key]
            return True
        return False

    def terminate(self, pid: int) -> None:
        """Backend-session termination releases every advisory lock it held."""

        for key in [key for key, owner in self.held.items() if owner == pid]:
            del self.held[key]

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
            raise RuntimeError("simulated PostgreSQL authority failure")
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
            if self._events is not None:
                self._events.append("lease_unlock")
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

        self.closed = True
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
    ) -> None:
        self.persisted: list[LineageEnvelope] = []
        self.calls = 0
        self._events = events
        self._fail_from_call = fail_from_call
        self._cancel_from_call = cancel_from_call
        self._gate = gate
        self._started = started

    async def persist(self, envelope: LineageEnvelope, /) -> None:
        self.calls += 1
        if self._started is not None:
            self._started.set()
        if self._gate is not None and self.calls == 1:
            await self._gate.wait()
        if self._cancel_from_call is not None and self.calls > self._cancel_from_call:
            raise asyncio.CancelledError()
        if self._fail_from_call is not None and self.calls > self._fail_from_call:
            raise RuntimeError("simulated lane persistence backend failure")
        self.persisted.append(envelope)
        if self._events is not None:
            self._events.append("persist")


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
    ) -> None:
        self.calls = 0
        self._result = result or MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="ODNO-0000001"
        )
        self._error = error
        self._events = events
        self._gate = gate
        self._started = started

    async def __call__(self) -> MutationCallbackResult:
        self.calls += 1
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
        repr(result.lease_grant),
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
    assert hold.connection_token == lease.grant.connection_token
    assert len(unreleased_authority_holds()) == holds_before + 1
    assert unreleased_authority_holds()[-1] is hold


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
    active_before = set(retained_authority_connections())
    history_before = len(authority_hold_history())

    with pytest.raises(CoordinationError):
        await lease.release(lease.grant)
    assert lease.released is False

    # Exactly one active strong hold appears.
    new_active = set(retained_authority_connections()) - active_before
    assert len(new_active) == 1
    hold_id = new_active.pop()
    assert retained_authority_connections()[hold_id].connection is connection
    assert lease.unreleased_authority_hold is not None
    assert lease.unreleased_authority_hold.hold_id == hold_id

    # A stale or foreign grant keeps failing during recovery, and clears nothing.
    with pytest.raises(CoordinationError) as excinfo:
        await lease.release(replace(lease.grant, connection_token="lockconn:forged"))
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert hold_id in retained_authority_connections()

    # A second retry that still cannot prove anything keeps the hold.
    with pytest.raises(CoordinationError):
        await lease.release(lease.grant)
    assert hold_id in retained_authority_connections()

    # The exact grant finally proves a full reverse unlock.
    connection._unlock_returns = None
    await lease.release(lease.grant)
    assert lease.released is True
    assert lease.unlocked_keys == (-7,)
    assert connection.closed is True

    # B20: the resolved hold is gone from BOTH active views, and the retained
    # connection is no longer reachable through them.
    assert hold_id not in retained_authority_connections()
    assert hold_id not in {hold.hold_id for hold in unreleased_authority_holds()}
    assert lease.unreleased_authority_hold is None
    assert all(
        retained.connection is not connection
        for retained in retained_authority_connections().values()
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

    active = retained_authority_connections()
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
    retained_before = set(retained_authority_connections())

    with pytest.raises(CoordinationError):
        await acquire_physical_account_lease(
            keys=[-7, 5, 11], connection_factory=ConnectionFactory(connection)
        )

    new_holds = set(retained_authority_connections()) - retained_before
    assert len(new_holds) == 1
    hold_id = new_holds.pop()
    retained = retained_authority_connections()[hold_id]

    # The real connection object is reachable, with the real identity.
    assert retained.connection is connection
    assert retained.grant.backend_pid == 3003
    assert retained.grant.database_oid == connection._database_oid
    assert retained.grant.keys == (-7, 5, 11)
    assert retained.grant.event_loop is asyncio.get_running_loop()

    # It survives a collection, and was never closed or pool-returned.
    gc.collect()
    assert retained_authority_connections()[hold_id].connection is connection
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
    assert retained_authority_connections().get("hold:forged") is None


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
    retained_before = set(retained_authority_connections())

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
    new_holds = set(retained_authority_connections()) - retained_before
    assert len(new_holds) == 1
    retained = retained_authority_connections()[new_holds.pop()]
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
    retained_before = set(retained_authority_connections())

    with pytest.raises(RuntimeError):
        await acquire_physical_account_lease(
            keys=[-7, 5], connection_factory=ConnectionFactory(connection)
        )

    # A positive exact-PID receipt resolves the ambiguity without a hold.
    assert connection.termination_calls[0][0] == 3003
    assert connection.terminated is True
    assert connection.unlock_calls == []
    assert space.held == {}
    assert set(retained_authority_connections()) == retained_before


@pytest.mark.asyncio
async def test_lock_explicit_false_stays_a_known_not_acquired_path():
    """Over-fail-closed is also a defect: a definite ``False`` is not ambiguity."""

    space = FakeLockSpace()
    blocker = FakeLockConnection(space, pid=2002)
    await acquire_physical_account_lease(
        keys=[11], connection_factory=ConnectionFactory(blocker)
    )
    connection = FakeLockConnection(space, pid=3003)
    retained_before = set(retained_authority_connections())

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
    assert set(retained_authority_connections()) == retained_before


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
    assert hold.backend_pid == 4242
    assert retained_authority_connections()[hold.hold_id].connection is connection


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
        "persist",
        "reserve",
        "callback_start",
        "callback_end",
        "persist",
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
    held = held_coordination(hold_id)
    assert isinstance(held, HeldCoordination)
    assert held.grant is held.lease.grant
    assert held.claim.claim_account_scope.startswith("mockpa:v1:")
    import gc

    gc.collect()
    assert held_coordination(hold_id) is held

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
    held = held_coordination(hold_id)
    assert isinstance(held, HeldCoordination)
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
    assert true_hold.hold_id not in retained_authority_connections()

    # The durable-false hold is completely unaffected and still unreleasable.
    assert false_hold_id in held_coordinations()
    stuck = held_coordination(false_hold_id)
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
    assert stack["connection"].unlock_calls == [result.lease_grant.keys[0]]
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

    async def bare_shield_mutant() -> None:
        inner = asyncio.ensure_future(callback())
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
    assert result.lease_grant.keys == tuple(sorted({result.scope.advisory_key, 4242}))
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
