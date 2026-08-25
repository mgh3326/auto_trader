"""ROB-1264 J3C Kiwoom coordination adapter — exact §E mutants.

Every zero-I/O claim asserts fake transport call count is exactly 0.
No test opens a network socket or loads a credential value.
"""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services.brokers.client_order_ids import BrokerClientIdTarget
from app.services.brokers.kiwoom import constants as kiwoom_constants
from app.services.brokers.kiwoom.client import KiwoomMockClient
from app.services.mock_integration.coordination import (
    CoordinationError,
    CoordinationReasonCode,
    CoordinationScope,
    DurableSendClaimAdapter,
    TerminalClaimEvidence,
    physical_account_scope_for_entry,
    split_advisory_key,
)
from app.services.mock_integration.lineage import MockLineageFactory
from app.services.mock_lane_registry import (
    _KIWOOM_MOCK_PHYSICAL_ACCOUNT_ID,
    CANONICAL_LANE_REGISTRY,
    ActivationStatus,
    LaneGuardError,
    LaneRegistryEntry,
    PolicyBinding,
    assert_entry_execution_ready,
)
from scripts.b0x.kr.kiwoom_coordination import (
    _entry_provenance,
    _register_approved_adapter,
)
from scripts.b0x.kr.kiwoom_ordering import (
    ACCOUNT_SUMMARY_FINGERPRINT_IDENTITY_REJECTED,
    CALLER_DERIVED_IDENTITY_REJECTED,
    JSONL_ABSENCE_NOT_EMPTY_OWNERSHIP,
    KIWOOM_CANONICAL_LANE_ID,
    KIWOOM_LIFECYCLE_STATUS,
    KIWOOM_READBACK_OPERATION,
    KIWOOM_RECOVERY_OWNER,
    KIWOOM_RELEASE_IF_MATCHES,
    KIWOOM_RESTART_TRIGGER,
    KT00009_CANNOT_REPLACE_KT00007,
    LANE_EVIDENCE_KINDS,
    LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND,
    PROXIMITY_ATTRIBUTION_REJECTED,
    ROOT_PATH_IDENTITY_REJECTED,
    AccountWriterLease,
    InMemoryDispatchEvidence,
    InMemoryLineagePersistence,
    InMemoryReservationPort,
    InMemoryUncertaintyGate,
    KiwoomAttributionRejected,
    KiwoomCoordinationAdapter,
    KiwoomCoordinationPorts,
    KiwoomIdentityRejected,
    KiwoomTransportGateRejected,
    assert_broker_client_id_contract,
    assert_kiwoom_transport_ready,
    followup_precheck,
    native_row_from_kt00007,
    reject_jsonl_as_empty_ownership,
    reject_kt00009_as_truth,
    reject_proximity_attribution,
    require_j2a_physical_account_id,
    restart_disposition,
    wall_clock_cannot_release,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RAW_PHYSICAL_ACCOUNT_ID = "RAW-KIWOOM-PHYSICAL-4242"
POLICY_VERSION = "trading-policy-v9"
POLICY_VERSION_HASH = "f" * 16
FINGERPRINT_REF = "sha256:test-account"

J3C_WRITE_FENCE = frozenset(
    {
        "scripts/b0x/kr/kiwoom_ordering.py",
        "scripts/b0x/kr/kiwoom_cycle.py",
        "scripts/b0x/kr/kiwoom.py",
        "scripts/b0x/kr/kiwoom_attribution.py",
        "tests/scripts/b0x/kr/kiwoom/test_kiwoom_ordering_support.py",
        "tests/scripts/b0x/kr/kiwoom/test_kiwoom_cycle.py",
        "tests/scripts/b0x/kr/kiwoom/test_kiwoom_round_trip.py",
        "tests/scripts/b0x/kr/kiwoom/test_kiwoom_static_guards.py",
        "tests/services/mock_integration/test_kiwoom_coordination_adapter.py",
        "docs/contracts/rob-1264-kiwoom-coordination-adapter.md",
    }
)


# ---------------------------------------------------------------------------
# Fake lock authority (J3A protocol only; no real PostgreSQL)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def mappings(self) -> _FakeResult:
        return self

    def one(self) -> dict[str, Any]:
        return self._rows[0]

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        return next(iter(self.one().values()))


class FakeLockSpace:
    def __init__(self) -> None:
        self.held: dict[int, int] = {}
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
        for key in [key for key, owner in self.held.items() if owner == pid]:
            del self.held[key]
            self.depth.pop((key, pid), None)

    def rows(self, pid: int, database_oid: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, owner in self.held.items():
            if owner != pid:
                continue
            classid, objid = split_advisory_key(key)
            out.append(
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
        return out


class FakeLockConnection:
    def __init__(
        self,
        space: FakeLockSpace,
        *,
        pid: int = 4242,
        drop_after_pg_locks: int | None = None,
    ) -> None:
        self._space = space
        self._pid = pid
        self._database_oid = 99001
        self.committed = False
        self._drop_after_pg_locks = drop_after_pg_locks
        self.pg_locks_seen = 0

    def can_prove_backend_session_termination(self) -> bool:
        return True

    async def execute(self, statement: Any, parameters: Any = None, /) -> _FakeResult:
        sql = str(statement)
        params = dict(parameters or {})
        if "pg_backend_pid" in sql:
            return _FakeResult(
                [{"backend_pid": self._pid, "database_oid": self._database_oid}]
            )
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(
                [{"acquired": self._space.try_lock(int(params["key"]), self._pid)}]
            )
        if "pg_advisory_unlock" in sql:
            return _FakeResult(
                [{"released": self._space.unlock(int(params["key"]), self._pid)}]
            )
        if "pg_locks" in sql:
            self.pg_locks_seen += 1
            if (
                self._drop_after_pg_locks is not None
                and self.pg_locks_seen > self._drop_after_pg_locks
            ):
                self._space.terminate(self._pid)
            return _FakeResult(self._space.rows(int(params["pid"]), self._database_oid))
        raise AssertionError(sql)

    async def commit(self) -> None:
        self.committed = True

    async def close(self) -> None:
        return None

    async def terminate_backend_session(
        self, *, expected_pid: int, owner_token: str
    ) -> Any:
        del expected_pid, owner_token
        self._space.terminate(self._pid)
        from app.services.mock_integration.coordination import BackendTerminationReceipt

        return BackendTerminationReceipt(
            backend_pid=self._pid, owner_token="x", terminated=True
        )


class ConnectionFactory:
    def __init__(
        self,
        space: FakeLockSpace,
        *,
        pid: int = 4242,
        drop_after_pg_locks: int | None = None,
    ) -> None:
        self.space = space
        self.pid = pid
        self.drop_after_pg_locks = drop_after_pg_locks
        self.calls = 0
        self.last: FakeLockConnection | None = None

    async def __call__(self) -> FakeLockConnection:
        self.calls += 1
        self.last = FakeLockConnection(
            self.space, pid=self.pid, drop_after_pg_locks=self.drop_after_pg_locks
        )
        return self.last


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def bound_kiwoom_entry(**overrides: object) -> LaneRegistryEntry:
    values: dict[str, object] = {
        "lane_status": LaneStatus.AUTO_ENABLED,
        "activation_status": ActivationStatus.ENABLED,
        "activation_reason": "j3c-test fully bound",
        "policy_binding": PolicyBinding(POLICY_VERSION, POLICY_VERSION_HASH),
        "execution_mode": "test-only-bounded",
        "scheduler_owner": SchedulerOwner.MANUAL,
        "timing_owner": "test-only-timing",
        "writer": True,
        "auto_order_enabled": True,
        "max_order_notional": Decimal("10000000"),
        "max_orders_per_session": 8,
        "max_open_orders": 8,
        "allowed_order_types": ("limit",),
        "allowed_time_in_force": ("day",),
        "reconcile_required": True,
        "physical_account_id": RAW_PHYSICAL_ACCOUNT_ID,
        "identity_status": "KNOWN",
        "fingerprint_evidence_ref": FINGERPRINT_REF,
        "canary_binding": "test-only-bounded-canary",
        "missing_bindings": (),
    }
    values.update(overrides)
    canonical = next(
        entry
        for entry in CANONICAL_LANE_REGISTRY
        if entry.lane_id == KIWOOM_CANONICAL_LANE_ID
    )
    return replace(canonical, **values)


def bound_registry(entry: LaneRegistryEntry) -> tuple[LaneRegistryEntry, ...]:
    return tuple(
        entry if item.lane_id == entry.lane_id else item
        for item in CANONICAL_LANE_REGISTRY
    )


@dataclass
class Planned:
    cycle_id: str = "cycle-j3c-1"
    order_key: str = "buy:005930:l1"
    client_order_id: str = "b0xkw-test-1"
    symbol: str = "005930"
    side: str = "buy"
    price: int = 70000
    quantity: int = 1
    leg: str = "L1"
    notional: Decimal = field(default_factory=lambda: Decimal("70000"))


class FakeKiwoomAccount:
    def __init__(
        self, *, client: object | None = None, fail_read: bool = False
    ) -> None:
        self._client = client if client is not None else make_test_mock_client()
        self.buy_calls: list[dict[str, Any]] = []
        self.sell_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self._next = 1
        self.fail_read = fail_read
        self.rows: list[dict[str, Any]] = []

    async def place_limit_buy(
        self, *, symbol: str, quantity: int, price: int
    ) -> dict[str, Any]:
        self.buy_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        order_no = f"{self._next:010d}"
        self._next += 1
        self.rows.append(
            {
                "order_id": order_no,
                "symbol": symbol,
                "status": "open",
                "ordered_quantity": quantity,
                "filled_quantity": 0,
                "remaining_quantity": quantity,
            }
        )
        return {"return_code": 0, "ord_no": order_no}

    async def place_limit_sell(
        self, *, symbol: str, quantity: int, price: int
    ) -> dict[str, Any]:
        self.sell_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        return {"return_code": 0, "ord_no": "0000000099"}

    async def cancel(
        self, *, original_order_no: str, symbol: str, cancel_quantity: int
    ) -> dict[str, Any]:
        self.cancel_calls.append(
            {
                "original_order_no": original_order_no,
                "symbol": symbol,
                "cancel_quantity": cancel_quantity,
            }
        )
        return {"return_code": 0, "ord_no": original_order_no}

    async def read_order_detail(
        self, *, order_date: str | None = None, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        del order_date, symbol
        if self.fail_read:
            raise RuntimeError("kt00007 unavailable")
        return list(self.rows)


def make_test_mock_client() -> KiwoomMockClient:
    return KiwoomMockClient(
        base_url=kiwoom_constants.MOCK_BASE_URL,
        app_key="unit-test",
        app_secret="unit-test",
        account_no="unit-test",
    )


def build_offline_adapter(
    *,
    space: FakeLockSpace | None = None,
    pid: int = 4242,
    entry: LaneRegistryEntry | None = None,
    unresolved: bool = False,
    drop_after_pg_locks: int | None = None,
) -> KiwoomCoordinationAdapter:
    bound = entry or bound_kiwoom_entry()
    lock_space = space or FakeLockSpace()
    persistence = InMemoryLineagePersistence()
    dispatch = InMemoryDispatchEvidence()
    gate = InMemoryUncertaintyGate()
    if unresolved:
        gate.mark_unresolved(
            physical_account_scope_for_entry(bound).claim_account_scope
        )
    persistence.events = persistence.events
    ports = KiwoomCoordinationPorts(
        persistence=persistence,
        dispatch_evidence=dispatch,
        uncertainty_gate=gate,
        claims=DurableSendClaimAdapter(InMemoryReservationPort()),
        connection_factory=ConnectionFactory(
            lock_space, pid=pid, drop_after_pg_locks=drop_after_pg_locks
        ),
        registry=bound_registry(bound),
        lineage_factory=MockLineageFactory(),
        entry=bound,
        diagnostic_fingerprint=FINGERPRINT_REF,
        coordination_provenance=_entry_provenance(bound),
        legacy_offline=True,
    )
    return _register_approved_adapter(ports, grant_only=False)


def _scope_assert_owned_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count real CoordinationScope.assert_owned invocations, not a self-list."""

    observed: list[str] = []
    original = CoordinationScope.assert_owned

    async def _spy(self: CoordinationScope) -> None:
        observed.append("assert_owned")
        await original(self)

    monkeypatch.setattr(CoordinationScope, "assert_owned", _spy)
    return observed


def offline_coordination_factory(
    *, entry: LaneRegistryEntry | None = None
) -> KiwoomCoordinationAdapter:
    """Used by cycle tests so ORDERING submits go through J3A."""

    return build_offline_adapter(entry=entry)


# ---------------------------------------------------------------------------
# §E-1 Identity and distributed ownership
# ---------------------------------------------------------------------------


def test_identity_rejects_caller_root_and_summary_fingerprint_mutants() -> None:
    entry = bound_kiwoom_entry()
    assert require_j2a_physical_account_id(entry) == RAW_PHYSICAL_ACCOUNT_ID
    with pytest.raises(KiwoomIdentityRejected) as caller:
        require_j2a_physical_account_id(
            entry, caller_physical_account_id="sha256:caller"
        )
    assert caller.value.reason == CALLER_DERIVED_IDENTITY_REJECTED
    with pytest.raises(KiwoomIdentityRejected) as root:
        require_j2a_physical_account_id(entry, root_path="/tmp/artifacts")
    assert root.value.reason == ROOT_PATH_IDENTITY_REJECTED
    with pytest.raises(KiwoomIdentityRejected) as summary:
        require_j2a_physical_account_id(
            entry, account_summary_fingerprint="sha256:test-account"
        )
    assert summary.value.reason == ACCOUNT_SUMMARY_FINGERPRINT_IDENTITY_REJECTED


@pytest.mark.asyncio
async def test_local_flock_without_j3a_grant_cannot_authorize_transport() -> None:
    account = FakeKiwoomAccount()
    lease = AccountWriterLease(
        root=Path("/tmp/j3c-flock"),
        lane="kiwoom_mock",
        account_fingerprint="sha256:test-account",
    )
    # Diagnostic flock is held; no J3A grant is minted.
    assert lease.canonical()["authorizes_send"] is False
    with pytest.raises(KiwoomTransportGateRejected) as exc:
        assert_kiwoom_transport_ready(
            account=account,
            entry=bound_kiwoom_entry(),
            physical_account_id=RAW_PHYSICAL_ACCOUNT_ID,
            grant_owned=False,
        )
    assert exc.value.reason == LOCAL_FLOCK_CANNOT_AUTHORIZE_SEND
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_two_fake_hosts_one_physical_account_one_writer() -> None:
    space = FakeLockSpace()
    first = build_offline_adapter(space=space, pid=1001)
    second = build_offline_adapter(space=space, pid=2002)
    account_a = FakeKiwoomAccount()
    account_b = FakeKiwoomAccount()
    entered = asyncio.Event()
    release_first = asyncio.Event()

    async def held_buy(*, symbol: str, quantity: int, price: int) -> dict[str, Any]:
        payload = await account_a.place_limit_buy(
            symbol=symbol, quantity=quantity, price=price
        )
        entered.set()
        await release_first.wait()
        return payload

    task = asyncio.create_task(
        first.submit_coordinated(
            account_a,
            planned=Planned(cycle_id="host-a"),
            policy_version=POLICY_VERSION,
            policy_version_hash=POLICY_VERSION_HASH,
            now=datetime.now(UTC),
            mutation=held_buy,
        )
    )
    await entered.wait()
    with pytest.raises(CoordinationError) as exc:
        await second.submit_coordinated(
            account_b,
            planned=Planned(cycle_id="host-b"),
            policy_version=POLICY_VERSION,
            policy_version_hash=POLICY_VERSION_HASH,
            now=datetime.now(UTC),
        )
    assert exc.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED
    release_first.set()
    await task
    assert len(account_a.buy_calls) == 1
    assert account_b.buy_calls == []


@pytest.mark.asyncio
async def test_durable_claim_conflict_makes_zero_transport() -> None:
    account = FakeKiwoomAccount()
    adapter = build_offline_adapter(unresolved=True)
    with pytest.raises(CoordinationError) as exc:
        await adapter.submit_coordinated(
            account,
            planned=Planned(),
            policy_version=POLICY_VERSION,
            policy_version_hash=POLICY_VERSION_HASH,
            now=datetime.now(UTC),
        )
    assert exc.value.reason_code is CoordinationReasonCode.DURABLE_CLAIM_CONFLICT
    assert adapter.transport_calls == []
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_lease_loss_before_callback_makes_zero_transport() -> None:
    """J3A pre-callback assert sees an empty lock table → no callback POST."""

    account = FakeKiwoomAccount()
    adapter = build_offline_adapter(drop_after_pg_locks=1)
    with pytest.raises(CoordinationError) as exc:
        await adapter.submit_coordinated(
            account,
            planned=Planned(),
            policy_version=POLICY_VERSION,
            policy_version_hash=POLICY_VERSION_HASH,
            now=datetime.now(UTC),
        )
    assert exc.value.reason_code in {
        CoordinationReasonCode.LEASE_LOST,
        CoordinationReasonCode.LOCK_AUTHORITY_UNAVAILABLE,
    }
    assert adapter.transport_calls == []
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_stale_fence_between_preassert_and_send_makes_zero_transport() -> None:
    """Lock disappears after J3A's pre-callback attest; lane reassert must catch it.

    Removing ``scope.assert_owned()`` from the adapter lets the POST through
    and this test fails.
    """

    account = FakeKiwoomAccount()
    adapter = build_offline_adapter(drop_after_pg_locks=3)
    with pytest.raises(CoordinationError) as exc:
        await adapter.submit_coordinated(
            account,
            planned=Planned(),
            policy_version=POLICY_VERSION,
            policy_version_hash=POLICY_VERSION_HASH,
            now=datetime.now(UTC),
        )
    assert exc.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert adapter.transport_calls == []
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_event_loop_mismatch_on_lane_reassert_makes_zero_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lane reassert runs J3A's event-loop check; a foreign loop is zero I/O."""

    account = FakeKiwoomAccount()
    adapter = build_offline_adapter()
    original = CoordinationScope.assert_owned
    foreign_loop = asyncio.new_event_loop()

    async def _foreign_loop_assert(self: CoordinationScope) -> None:
        real_get = asyncio.get_running_loop

        def _lie() -> asyncio.AbstractEventLoop:
            return foreign_loop

        monkeypatch.setattr(asyncio, "get_running_loop", _lie)
        try:
            await original(self)
        finally:
            monkeypatch.setattr(asyncio, "get_running_loop", real_get)

    monkeypatch.setattr(CoordinationScope, "assert_owned", _foreign_loop_assert)
    try:
        with pytest.raises(CoordinationError) as exc:
            await adapter.submit_coordinated(
                account,
                planned=Planned(),
                policy_version=POLICY_VERSION,
                policy_version_hash=POLICY_VERSION_HASH,
                now=datetime.now(UTC),
            )
        assert exc.value.reason_code is CoordinationReasonCode.LEASE_EVENT_LOOP_MISMATCH
        assert adapter.transport_calls == []
        assert account.buy_calls == []
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_batch_and_cancel_call_assert_owned_before_every_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _scope_assert_owned_calls(monkeypatch)
    adapter = build_offline_adapter()
    account = FakeKiwoomAccount()
    first = await adapter.submit_coordinated(
        account,
        planned=Planned(order_key="l1", cycle_id="batch-1"),
        policy_version=POLICY_VERSION,
        policy_version_hash=POLICY_VERSION_HASH,
        now=datetime.now(UTC),
    )
    await adapter.submit_coordinated(
        account,
        planned=Planned(order_key="l2", cycle_id="batch-2"),
        policy_version=POLICY_VERSION,
        policy_version_hash=POLICY_VERSION_HASH,
        now=datetime.now(UTC),
    )
    await adapter.cancel_attributed(
        account,
        planned=Planned(order_key="cancel-l1", cycle_id="batch-1"),
        native_order_id=first.evidence.broker_order_id or "",
        known_remainder=Decimal("1"),
        policy_version=POLICY_VERSION,
        policy_version_hash=POLICY_VERSION_HASH,
    )
    assert observed == ["assert_owned", "assert_owned", "assert_owned"]
    assert len(account.buy_calls) == 2
    assert len(account.cancel_calls) == 1


# ---------------------------------------------------------------------------
# §E-2 Nullable native client ID and ACK order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kiwoom_attempt_cid_none_and_ack_precedes_jsonl() -> None:
    adapter = build_offline_adapter()
    account = FakeKiwoomAccount()
    jsonl: list[str] = []

    def record_order_no(*, order_no: str, planned: Planned, at: datetime) -> None:
        del planned, at
        adapter.ports.persistence.events.append("jsonl_callback")
        jsonl.append(order_no)

    result = await adapter.submit_coordinated(
        account,
        planned=Planned(),
        record_order_no=record_order_no,
        policy_version=POLICY_VERSION,
        policy_version_hash=POLICY_VERSION_HASH,
        now=datetime.now(UTC),
    )
    envelope = result.envelope
    assert_broker_client_id_contract(envelope)
    assert envelope.broker_client_id_target is None
    assert envelope.order_attempt is not None
    assert envelope.order_attempt.broker_client_order_id is None
    assert envelope.order_attempt.idempotency_key.startswith("mock-idempotency-v1:")
    assert envelope.order_attempt.broker_order_id == jsonl[0]
    assert set(BrokerClientIdTarget) == {
        BrokerClientIdTarget.TOSS,
        BrokerClientIdTarget.BINANCE_SPOT_DEMO,
        BrokerClientIdTarget.ALPACA_PAPER,
    }
    events = adapter.ports.persistence.events
    assert events.index("j2b_ack_persisted") < events.index("jsonl_callback")
    assert adapter.ordered_events.index(
        "j2b_ack_persisted"
    ) < adapter.ordered_events.index("jsonl_appended")


# ---------------------------------------------------------------------------
# §E-3 Crash / restart no-repost
# ---------------------------------------------------------------------------


def test_restart_not_created_allows_matched_cleanup() -> None:
    disposition = restart_disposition(
        durable_broker_order_id=None,
        kt00007_readable=True,
        pre_send_not_created=True,
    )
    assert disposition.status == "not_created"
    assert disposition.allow_repost is False
    assert disposition.block_physical_account is False


def test_restart_post_before_ack_blocks_account_and_forbids_repost() -> None:
    disposition = restart_disposition(
        durable_broker_order_id=None,
        kt00007_readable=True,
        pre_send_not_created=False,
    )
    assert disposition.status == "unknown_pending_reconcile"
    assert disposition.block_physical_account is True
    assert disposition.allow_repost is False


def test_restart_ack_without_jsonl_recovers_from_kt00007() -> None:
    rows = [
        {
            "order_id": "0000000007",
            "status": "open",
            "filled_quantity": 0,
            "remaining_quantity": 1,
            "native": "raw",
        }
    ]
    disposition = restart_disposition(
        durable_broker_order_id="0000000007",
        kt00007_readable=True,
        kt00007_rows=rows,
        jsonl_missing=True,
    )
    assert disposition.status == "recovered_from_j2b_and_kt00007"
    assert disposition.native is not None
    assert disposition.native.raw_row["native"] == "raw"
    assert disposition.allow_repost is False


def test_restart_jsonl_missing_cannot_become_empty_ownership() -> None:
    with pytest.raises(KiwoomAttributionRejected) as exc:
        restart_disposition(
            durable_broker_order_id=None,
            kt00007_readable=True,
            jsonl_missing=True,
        )
    assert exc.value.reason == JSONL_ABSENCE_NOT_EMPTY_OWNERSHIP


def test_restart_broker_read_or_missing_row_holds() -> None:
    unread = restart_disposition(
        durable_broker_order_id="0000000007",
        kt00007_readable=False,
    )
    assert unread.status == "unknown_pending_reconcile"
    assert unread.block_physical_account is True
    missing = restart_disposition(
        durable_broker_order_id="0000000007",
        kt00007_readable=True,
        kt00007_rows=[],
    )
    assert missing.status == "unknown_pending_reconcile"
    assert missing.block_physical_account is True


# ---------------------------------------------------------------------------
# §E-4 Attribution mutants
# ---------------------------------------------------------------------------


def test_proximity_match_without_broker_id_rejected() -> None:
    with pytest.raises(KiwoomAttributionRejected) as exc:
        reject_proximity_attribution()
    assert exc.value.reason == PROXIMITY_ATTRIBUTION_REJECTED
    with pytest.raises(KiwoomAttributionRejected):
        native_row_from_kt00007(
            [{"symbol": "005930", "side": "buy", "order_id": "x"}],
            broker_order_id="",
        )


def test_kt00009_cannot_replace_kt00007() -> None:
    with pytest.raises(KiwoomAttributionRejected) as exc:
        reject_kt00009_as_truth()
    assert exc.value.reason == KT00009_CANNOT_REPLACE_KT00007


def test_foreign_collision_not_attributed_from_jsonl() -> None:
    rows = [
        {
            "order_id": "FOREIGN-1",
            "symbol": "005930",
            "status": "open",
            "filled_quantity": 0,
            "remaining_quantity": 1,
        }
    ]
    assert native_row_from_kt00007(rows, broker_order_id="OURS-1") is None
    with pytest.raises(KiwoomAttributionRejected):
        reject_jsonl_as_empty_ownership()


def test_native_raw_row_preserved_beside_normalized_state() -> None:
    raw = {
        "order_id": "0000000042",
        "status": "partially_filled",
        "filled_quantity": 1,
        "remaining_quantity": 2,
        "broker_raw": {"ord_remnq": "2"},
    }
    native = native_row_from_kt00007([raw], broker_order_id="0000000042")
    assert native is not None
    assert native.normalized_state == "partial"
    assert native.raw_row["broker_raw"] == {"ord_remnq": "2"}


# ---------------------------------------------------------------------------
# §E-5 Terminal / remainder
# ---------------------------------------------------------------------------


def test_terminal_normalization_and_partial_holds_claim() -> None:
    assert (
        native_row_from_kt00007(
            [
                {
                    "order_id": "1",
                    "status": "open",
                    "remaining_quantity": 1,
                    "filled_quantity": 0,
                }
            ],
            broker_order_id="1",
        ).normalized_state
        == "open"
    )
    partial = native_row_from_kt00007(
        [
            {
                "order_id": "2",
                "status": "partial",
                "remaining_quantity": 1,
                "filled_quantity": 1,
            }
        ],
        broker_order_id="2",
    )
    assert partial is not None
    assert partial.normalized_state == "partial"
    assert followup_precheck(
        operation="cancel",
        native_order_id="2",
        known_remainder=Decimal(partial.remaining_quantity or 0),
        fresh_guards_passed=True,
    )
    assert not followup_precheck(
        operation="cancel",
        native_order_id="2",
        known_remainder=None,
        fresh_guards_passed=True,
    )


def test_wall_clock_journal_absence_cannot_release() -> None:
    evidence = wall_clock_cannot_release()
    assert evidence.authorizes_release is False
    assert evidence.lane_native_terminal_evidence is False


@pytest.mark.asyncio
async def test_release_requires_terminal_evidence_and_owner_match() -> None:
    port = InMemoryReservationPort()
    claims = DurableSendClaimAdapter(port)
    from app.services.mock_integration.coordination import DurableClaim

    scope = physical_account_scope_for_entry(bound_kiwoom_entry())
    claim = await claims.reserve(
        scope=scope, idempotency_key="mock-idempotency-v1:abc", side="buy"
    )
    assert isinstance(claim, DurableClaim)
    with pytest.raises(CoordinationError):
        await claims.release_with_terminal_evidence(claim, TerminalClaimEvidence())
    assert port.rows
    released = await claims.release_with_terminal_evidence(
        claim,
        TerminalClaimEvidence(
            lane_native_terminal_evidence=True,
            account_position_reconciled=True,
            remainder_known=True,
        ),
    )
    assert released == 1
    assert port.rows == {}


# ---------------------------------------------------------------------------
# §E-6 Actual transport gate
# ---------------------------------------------------------------------------


def test_live_character_client_rejected_before_transport() -> None:
    account = FakeKiwoomAccount(client=object())
    with pytest.raises(KiwoomTransportGateRejected):
        assert_kiwoom_transport_ready(
            account=account,
            entry=bound_kiwoom_entry(),
            physical_account_id=RAW_PHYSICAL_ACCOUNT_ID,
            grant_owned=True,
        )


def test_wrong_physical_profile_rejected() -> None:
    account = FakeKiwoomAccount()
    with pytest.raises(KiwoomTransportGateRejected):
        assert_kiwoom_transport_ready(
            account=account,
            entry=bound_kiwoom_entry(),
            physical_account_id="SOME-OTHER-ACCOUNT",
            grant_owned=True,
        )


@pytest.mark.asyncio
async def test_submit_coordinated_wires_transport_gate_before_post() -> None:
    """Removing assert_kiwoom_transport_ready from submit_coordinated's callback fails this."""

    adapter = build_offline_adapter()
    account = FakeKiwoomAccount(client=object())
    with pytest.raises(KiwoomTransportGateRejected):
        await adapter.submit_coordinated(
            account,
            planned=Planned(),
            policy_version=POLICY_VERSION,
            policy_version_hash=POLICY_VERSION_HASH,
            now=datetime.now(UTC),
        )
    assert account.buy_calls == []
    assert adapter.transport_calls == []


@pytest.mark.asyncio
async def test_cancel_attributed_wires_transport_gate_before_cancel() -> None:
    """Removing assert_kiwoom_transport_ready from cancel_attributed's callback fails this."""

    adapter = build_offline_adapter()
    account = FakeKiwoomAccount(client=object())
    with pytest.raises(KiwoomTransportGateRejected):
        await adapter.cancel_attributed(
            account,
            planned=Planned(order_key="cancel-l1", cycle_id="batch-1"),
            native_order_id="0000000001",
            known_remainder=Decimal("1"),
            policy_version=POLICY_VERSION,
            policy_version_hash=POLICY_VERSION_HASH,
        )
    assert account.cancel_calls == []
    assert adapter.transport_calls == []


@pytest.mark.asyncio
async def test_exact_mock_client_reaches_transport_only_after_j3a_assert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _scope_assert_owned_calls(monkeypatch)
    adapter = build_offline_adapter()
    account = FakeKiwoomAccount()
    await adapter.submit_coordinated(
        account,
        planned=Planned(),
        policy_version=POLICY_VERSION,
        policy_version_hash=POLICY_VERSION_HASH,
        now=datetime.now(UTC),
    )
    assert observed == ["assert_owned"]
    assert len(account.buy_calls) == 1


# ---------------------------------------------------------------------------
# §E-7 Blocked lifecycle and static safety
# ---------------------------------------------------------------------------


def test_pnl_unreadable_is_explicit_block_not_numeric_zero() -> None:
    from scripts.b0x.kr.kiwoom_attribution import RealizedPnlInput

    blocked = RealizedPnlInput(
        value=None,
        source="own_order_journal",
        reason="own_order_activity_exists_today_without_dedicated_realized_pnl_source",
    )
    assert blocked.readable is False
    assert blocked.value is None
    assert blocked.canonical()["value"] is None


def _record_lane_evidence_literal_kinds(path: Path) -> set[str]:
    """Kinds passed as string literals to record_lane_evidence(...)."""

    kinds: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute) and func.attr == "record_lane_evidence"
        ):
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            kinds.add(node.args[0].value)
    return kinds


def test_recovery_contract_six_items_and_lifecycle_status() -> None:
    assert KIWOOM_RECOVERY_OWNER == (
        "scripts.b0x.kr.kiwoom_ordering.KiwoomCoordinationAdapter"
    )
    assert KIWOOM_RESTART_TRIGGER
    assert KIWOOM_READBACK_OPERATION == "kt00007"
    assert KIWOOM_RELEASE_IF_MATCHES
    assert KIWOOM_LIFECYCLE_STATUS == "AUTO_READY_BLOCKED_BY_LIFECYCLE"
    contract = (
        REPO_ROOT / "docs" / "contracts" / "rob-1264-kiwoom-coordination-adapter.md"
    ).read_text(encoding="utf-8")
    assert "C5 remains" in contract
    source = REPO_ROOT / "scripts" / "b0x" / "kr" / "kiwoom_ordering.py"
    assert _record_lane_evidence_literal_kinds(source) == set(LANE_EVIDENCE_KINDS)


@pytest.mark.asyncio
async def test_all_seven_lane_evidence_kinds_are_written_by_code() -> None:
    adapter = build_offline_adapter()
    account = FakeKiwoomAccount()
    await adapter.submit_coordinated(
        account,
        planned=Planned(),
        policy_version=POLICY_VERSION,
        policy_version_hash=POLICY_VERSION_HASH,
        now=datetime.now(UTC),
    )
    rejected = native_row_from_kt00007(
        [
            {
                "order_id": "R1",
                "status": "rejected",
                "filled_quantity": 0,
                "remaining_quantity": 0,
            }
        ],
        broker_order_id="R1",
    )
    expired = native_row_from_kt00007(
        [
            {
                "order_id": "E1",
                "status": "expired",
                "filled_quantity": 0,
                "remaining_quantity": 0,
            }
        ],
        broker_order_id="E1",
    )
    partial = native_row_from_kt00007(
        [
            {
                "order_id": "P1",
                "status": "partial",
                "filled_quantity": 1,
                "remaining_quantity": 1,
            }
        ],
        broker_order_id="P1",
    )
    assert rejected is not None and expired is not None and partial is not None
    adapter.record_native_broker_truth(rejected)
    adapter.record_native_broker_truth(expired)
    adapter.record_native_broker_truth(partial)
    adapter.apply_restart_disposition(
        durable_broker_order_id=None,
        kt00007_readable=False,
    )
    await adapter.cancel_attributed(
        account,
        planned=Planned(order_key="c1", cycle_id="c1"),
        native_order_id="0000000001",
        known_remainder=Decimal("1"),
        policy_version=POLICY_VERSION,
        policy_version_hash=POLICY_VERSION_HASH,
    )
    scope = physical_account_scope_for_entry(bound_kiwoom_entry())
    claim = await adapter.ports.claims.reserve(
        scope=scope, idempotency_key="mock-idempotency-v1:term", side="buy"
    )
    await adapter.release_if_matches_terminal(
        claim,
        TerminalClaimEvidence(
            lane_native_terminal_evidence=True,
            account_position_reconciled=True,
            remainder_known=True,
        ),
    )
    written = {
        event.removeprefix("lane_evidence:")
        for event in adapter.ordered_events
        if event.startswith("lane_evidence:")
    }
    assert written == set(LANE_EVIDENCE_KINDS)


def test_c5_remains_unknown_and_is_not_erased() -> None:
    contract = (
        REPO_ROOT / "docs" / "contracts" / "rob-1264-kiwoom-coordination-adapter.md"
    ).read_text(encoding="utf-8")
    assert "C5 remains" in contract
    source = (REPO_ROOT / "scripts" / "b0x" / "kr" / "kiwoom_ordering.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        if isinstance(node, ast.Name):
            names.add(node.id)
    assert "TaskGroup" not in names
    assert "timeout" not in names or "DEFAULT_TIMEOUT" in names


def test_no_j3a_sql_or_reason_enum_copied() -> None:
    source = (REPO_ROOT / "scripts" / "b0x" / "kr" / "kiwoom_ordering.py").read_text(
        encoding="utf-8"
    )
    assert "pg_try_advisory_lock" not in source
    assert "mock-physical-account-v1" not in source
    assert "class CoordinationReasonCode" not in source


_KIWOOM_CLIENT_PY_SHA256: str = (
    "7c68b03e5e99582071207ce7518891ec8d50d733a06cb356b48414f06bf15a93"
)


def test_client_py_matches_reviewed_g1_transport_pin() -> None:
    import hashlib

    digest = hashlib.sha256(
        (
            REPO_ROOT / "app" / "services" / "brokers" / "kiwoom" / "client.py"
        ).read_bytes()
    ).hexdigest()
    assert digest == _KIWOOM_CLIENT_PY_SHA256


def test_client_py_keeps_j3c_coordination_out_of_transport() -> None:
    """G1 may wire canonical Redis, but must not import J3C coordination."""

    source = (
        REPO_ROOT / "app" / "services" / "brokers" / "kiwoom" / "client.py"
    ).read_text(encoding="utf-8")
    assert "KiwoomAuthClient" in source
    assert "pg_try_advisory_lock" not in source
    assert "CoordinationReasonCode" not in source


# ---------------------------------------------------------------------------
# §E-9 The signed lane, exactly as shipped
#
# Every other test in this module builds ``bound_kiwoom_entry()``, which
# replaces twenty fields on the canonical row to make the lane execution
# ready. That fixture answers "does the adapter behave once kr.kiwoom.mock is
# activated". It cannot answer "is kr.kiwoom.mock activated", and the B-track
# runner consumes the *signed* row, not the fixture. These tests pin the
# signed row's real answer so a future reader does not mistake the fixture's
# green for a grant the port would actually issue.
# ---------------------------------------------------------------------------


def canonical_kiwoom_entry() -> LaneRegistryEntry:
    """The signed kr.kiwoom.mock row — no overrides, no replace()."""

    return next(
        entry
        for entry in CANONICAL_LANE_REGISTRY
        if entry.lane_id == KIWOOM_CANONICAL_LANE_ID
    )


def canonical_unknown_kiwoom_entry() -> LaneRegistryEntry:
    """The untouched US Kiwoom row, which remains an UNKNOWN identity."""

    return next(
        entry for entry in CANONICAL_LANE_REGISTRY if entry.lane_id == "us.kiwoom.mock"
    )


def test_signed_kiwoom_lane_is_not_execution_ready() -> None:
    """The signed row remains blocked even after identity is known."""

    entry = canonical_kiwoom_entry()
    assert entry.activation_status is ActivationStatus.BLOCKED
    assert entry.lane_status is LaneStatus.NOT_READY
    assert entry.writer is False
    assert entry.auto is False
    assert entry.scheduler_owner is None
    physical_account_id = entry.physical_account_id
    assert physical_account_id == _KIWOOM_MOCK_PHYSICAL_ACCOUNT_ID
    assert physical_account_id.startswith(
        "kiwoom_mock:kr:credential_fingerprint=sha256:"
    )
    assert physical_account_id.endswith(":kr_kiwoom_mock_domain")
    assert entry.identity_status == "KNOWN"
    assert entry.fingerprint_evidence_ref
    assert entry.missing_bindings

    with pytest.raises(LaneGuardError) as refusal:
        assert_entry_execution_ready(entry)
    assert "lane_activation_not_enabled" in str(refusal.value)


def test_signed_kiwoom_lane_fails_every_execution_ready_clause() -> None:
    """Identity is bound, but activation and remaining grants are absent.

    Recorded so nobody reads the single ``lane_activation_not_enabled`` string
    above as "flip one boolean and the grant flows".
    """

    entry = canonical_kiwoom_entry()
    assert entry.activation_status is not ActivationStatus.ENABLED
    assert not (entry.writer and entry.auto)
    assert entry.physical_account_id == _KIWOOM_MOCK_PHYSICAL_ACCOUNT_ID
    assert entry.identity_status == "KNOWN"
    assert entry.fingerprint_evidence_ref
    assert entry.missing_bindings != ()


def test_unknown_us_kiwoom_lane_cannot_construct_the_coordination_adapter() -> None:
    """The untouched US UNKNOWN row still fails on J2A identity."""

    entry = canonical_unknown_kiwoom_entry()
    assert entry.lane_id == "us.kiwoom.mock"
    assert entry.physical_account_id is None
    assert entry.identity_status == "UNKNOWN"
    account = FakeKiwoomAccount()
    ports = KiwoomCoordinationPorts(
        persistence=InMemoryLineagePersistence(),
        dispatch_evidence=InMemoryDispatchEvidence(),
        uncertainty_gate=InMemoryUncertaintyGate(),
        claims=DurableSendClaimAdapter(InMemoryReservationPort()),
        connection_factory=ConnectionFactory(FakeLockSpace(), pid=4242),
        registry=CANONICAL_LANE_REGISTRY,
        lineage_factory=MockLineageFactory(),
        entry=entry,
    )
    with pytest.raises(LaneGuardError) as refusal:
        KiwoomCoordinationAdapter(ports)
    assert "physical_account_identity_unknown" in str(refusal.value)
    assert account.buy_calls == []
    assert account.sell_calls == []


def test_signed_kiwoom_lane_transport_gate_refuses_even_claiming_a_grant() -> None:
    """``grant_owned=True`` is not a grant. The signed row still fails closed.

    The signed row raises ``KiwoomTransportGateRejected`` /
    ``transport_gate_rejected`` because its canonical physical identity
    differs from the caller id. ``LaneGuardError`` is not a path through
    ``assert_kiwoom_transport_ready`` on this row; that type is raised by
    ``require_j2a_physical_account_id`` during adapter construction and is
    pinned by ``test_signed_kiwoom_lane_cannot_construct_the_coordination_adapter``.
    """

    account = FakeKiwoomAccount()
    with pytest.raises(KiwoomTransportGateRejected) as refusal:
        assert_kiwoom_transport_ready(
            account=account,
            entry=canonical_kiwoom_entry(),
            physical_account_id=RAW_PHYSICAL_ACCOUNT_ID,
            grant_owned=True,
        )
    assert refusal.value.reason == "transport_gate_rejected"
    assert account.buy_calls == []
    assert account.sell_calls == []


def test_execution_ready_writer_clause_fires_once_activation_passes() -> None:
    """1182 is independent of 1180: activation ENABLED, writer/auto still off.

    ``kr.kiwoom.mock`` is not on the signed-restriction lists, so 1178
    stays quiet. Only ``activation_status`` is replaced — this is not
    ``bound_kiwoom_entry()``.
    """

    entry = replace(
        canonical_kiwoom_entry(),
        activation_status=ActivationStatus.ENABLED,
    )
    assert entry.writer is False
    assert entry.auto is False
    with pytest.raises(LaneGuardError) as refusal:
        assert_entry_execution_ready(entry)
    assert refusal.value.code == "lane_writer_not_enabled"


def test_execution_ready_identity_clause_fires_once_prior_clauses_pass() -> None:
    """1184 is independent of 1180/1182: those two pass, identity stays unknown."""

    entry = replace(
        canonical_kiwoom_entry(),
        activation_status=ActivationStatus.ENABLED,
        writer=True,
        auto_order_enabled=True,
        physical_account_id=None,
        identity_status="UNKNOWN",
        fingerprint_evidence_ref=None,
    )
    assert entry.physical_account_id is None
    assert entry.identity_status == "UNKNOWN"
    assert entry.fingerprint_evidence_ref is None
    with pytest.raises(LaneGuardError) as refusal:
        assert_entry_execution_ready(entry)
    assert refusal.value.code == "physical_account_identity_unknown"


def test_execution_ready_missing_bindings_clause_fires_once_prior_clauses_pass() -> (
    None
):
    """1197 is independent of 1180/1182/1184 *and* of the field-level check.

    Filling policy/caps/canary makes ``required_bindings`` (:1198-1214)
    pass, so deleting only 1197 cannot hide behind the same
    ``lane_binding_incomplete`` code. ``missing_bindings`` stays the
    signed 5-tuple. Still not ``bound_kiwoom_entry()``.
    """

    signed = canonical_kiwoom_entry()
    entry = replace(
        signed,
        activation_status=ActivationStatus.ENABLED,
        writer=True,
        auto_order_enabled=True,
        physical_account_id=RAW_PHYSICAL_ACCOUNT_ID,
        identity_status="KNOWN",
        fingerprint_evidence_ref=FINGERPRINT_REF,
        policy_binding=PolicyBinding(POLICY_VERSION, POLICY_VERSION_HASH),
        execution_mode="test-only-clause-independence",
        scheduler_owner=SchedulerOwner.MANUAL,
        max_order_notional=Decimal("10000000"),
        max_orders_per_session=8,
        max_open_orders=8,
        allowed_order_types=("limit",),
        allowed_time_in_force=("day",),
        reconcile_required=True,
        canary_binding="test-only-clause-independence",
    )
    assert entry.missing_bindings == signed.missing_bindings
    assert entry.missing_bindings
    with pytest.raises(LaneGuardError) as refusal:
        assert_entry_execution_ready(entry)
    assert refusal.value.code == "lane_binding_incomplete"
