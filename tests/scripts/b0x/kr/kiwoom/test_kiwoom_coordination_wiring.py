"""G1→G2 Kiwoom owner wiring, identity guards, and grant-only canaries."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.execution_contracts import LaneStatus
from app.services.mock_lane_registry import CANONICAL_LANE_REGISTRY, LaneGuardError
from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr
from scripts.b0x.kr import kiwoom_cycle, kiwoom_ordering
from scripts.b0x.kr.kiwoom_coordination import (
    KIWOOM_COORDINATION_OWNER_ACCOUNT_MISMATCH,
    KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH,
    KIWOOM_COORDINATION_OWNER_LANE_MISMATCH,
    KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED,
    KIWOOM_COORDINATION_OWNER_TYPE_REJECTED,
    KIWOOM_KR_LANE_ID,
    KIWOOM_US_LANE_ID,
    KiwoomCoordinationOwnerRejected,
    assert_kiwoom_coordination_owner,
    build_kiwoom_coordination_factory,
    make_grant_only_kiwoom_coordination_adapter,
    production_kiwoom_coordination_factory,
    resolve_kiwoom_lane_entry,
)
from tests.scripts.b0x.kr.kiwoom.conftest import FakeAccount
from tests.scripts.b0x.kr.kiwoom.test_kiwoom_cycle import _write_table
from tests.services.mock_integration.test_kiwoom_coordination_adapter import (
    bound_kiwoom_entry,
)

pytestmark = pytest.mark.unit

IN_SESSION = dt.datetime(2026, 8, 12, 3, 0, tzinfo=dt.UTC)


def _us_bound_entry():  # noqa: ANN202 — test fixture helper
    kr = bound_kiwoom_entry()
    us = next(
        entry for entry in CANONICAL_LANE_REGISTRY if entry.lane_id == KIWOOM_US_LANE_ID
    )
    return replace(
        us,
        lane_status=kr.lane_status,
        activation_status=kr.activation_status,
        activation_reason=kr.activation_reason,
        policy_binding=kr.policy_binding,
        execution_mode=kr.execution_mode,
        scheduler_owner=kr.scheduler_owner,
        timing_owner=kr.timing_owner,
        writer=kr.writer,
        auto_order_enabled=kr.auto_order_enabled,
        max_order_notional=kr.max_order_notional,
        max_orders_per_session=kr.max_orders_per_session,
        max_open_orders=kr.max_open_orders,
        allowed_order_types=kr.allowed_order_types,
        allowed_time_in_force=kr.allowed_time_in_force,
        reconcile_required=kr.reconcile_required,
        physical_account_id="US-KIWOOM-PHYSICAL-4242",
        identity_status="KNOWN",
        fingerprint_evidence_ref="sha256:us-test-account",
        canary_binding=kr.canary_binding,
        missing_bindings=(),
    )


def test_recovery_contract_is_a_pin_over_existing_constants() -> None:
    assert set(kiwoom_ordering.KIWOOM_LANE_RECOVERY_CONTRACT) == {
        "recovery_owner",
        "restart_trigger",
        "readback_operation",
        "release_if_matches",
        "blocked_state",
    }
    assert kiwoom_ordering.KIWOOM_LANE_RECOVERY_CONTRACT == {
        "recovery_owner": kiwoom_ordering.KIWOOM_RECOVERY_OWNER,
        "restart_trigger": kiwoom_ordering.KIWOOM_RESTART_TRIGGER,
        "readback_operation": kiwoom_ordering.KIWOOM_READBACK_OPERATION,
        "release_if_matches": kiwoom_ordering.KIWOOM_RELEASE_IF_MATCHES,
        "blocked_state": kiwoom_ordering.KIWOOM_LIFECYCLE_STATUS,
    }


def test_bound_factory_creates_the_adapter_from_the_exact_lane_entry() -> None:
    entry = bound_kiwoom_entry()
    factory = build_kiwoom_coordination_factory(
        entry=entry,
        ports_factory=lambda pinned: (
            make_grant_only_kiwoom_coordination_adapter(pinned).ports
        ),
    )

    adapter = factory()
    assert type(adapter) is kiwoom_ordering.KiwoomCoordinationAdapter
    assert adapter.ports.entry is entry
    assert adapter.physical_account_id == entry.physical_account_id


def test_current_registry_keeps_kr_and_us_identity_fail_closed() -> None:
    kr = resolve_kiwoom_lane_entry(KIWOOM_KR_LANE_ID)
    us = resolve_kiwoom_lane_entry(KIWOOM_US_LANE_ID)
    assert kr.physical_account_id is None
    assert us.physical_account_id is None
    assert us.lane_status is LaneStatus.NOT_READY
    with pytest.raises(LaneGuardError, match="physical_account_identity_unknown"):
        production_kiwoom_coordination_factory(KIWOOM_KR_LANE_ID)()
    with pytest.raises(LaneGuardError, match="physical_account_identity_unknown"):
        production_kiwoom_coordination_factory(KIWOOM_US_LANE_ID)()


class _RecoveryOwnerOnly:
    recovery_owner = kiwoom_ordering.KIWOOM_RECOVERY_OWNER


class _FiveConstantFake:
    recovery_owner = kiwoom_ordering.KIWOOM_RECOVERY_OWNER
    restart_trigger = kiwoom_ordering.KIWOOM_RESTART_TRIGGER
    readback_operation = kiwoom_ordering.KIWOOM_READBACK_OPERATION
    release_if_matches_condition = kiwoom_ordering.KIWOOM_RELEASE_IF_MATCHES
    blocked_state = kiwoom_ordering.KIWOOM_LIFECYCLE_STATUS


class _AdapterSubclass(kiwoom_ordering.KiwoomCoordinationAdapter):
    pass


@pytest.mark.parametrize(
    ("label", "candidate", "expected_code"),
    [
        (
            "recovery-owner-only-dummy",
            lambda: _RecoveryOwnerOnly(),
            KIWOOM_COORDINATION_OWNER_TYPE_REJECTED,
        ),
        (
            "five-constant-fake-class",
            lambda: _FiveConstantFake(),
            KIWOOM_COORDINATION_OWNER_TYPE_REJECTED,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_identity_guard_rejects_duck_typed_owner_mutants(
    label,
    candidate,
    expected_code,  # noqa: ANN001
) -> None:
    del label
    with pytest.raises(KiwoomCoordinationOwnerRejected) as refusal:
        assert_kiwoom_coordination_owner(candidate())
    assert refusal.value.code == expected_code


def test_identity_guard_rejects_subclass_and_other_lane_instance() -> None:
    entry = bound_kiwoom_entry()
    adapter = make_grant_only_kiwoom_coordination_adapter(entry)
    subclass = _AdapterSubclass(adapter.ports)
    with pytest.raises(KiwoomCoordinationOwnerRejected) as subclass_refusal:
        assert_kiwoom_coordination_owner(subclass)
    assert subclass_refusal.value.code == KIWOOM_COORDINATION_OWNER_TYPE_REJECTED

    us_adapter = make_grant_only_kiwoom_coordination_adapter(_us_bound_entry())
    with pytest.raises(KiwoomCoordinationOwnerRejected) as lane_refusal:
        assert_kiwoom_coordination_owner(us_adapter)
    assert lane_refusal.value.code == KIWOOM_COORDINATION_OWNER_LANE_MISMATCH


@pytest.mark.parametrize(
    ("candidate_factory", "expected_code"),
    [
        (
            lambda: _RecoveryOwnerOnly(),
            KIWOOM_COORDINATION_OWNER_TYPE_REJECTED,
        ),
        (
            lambda: _FiveConstantFake(),
            KIWOOM_COORDINATION_OWNER_TYPE_REJECTED,
        ),
        (
            lambda: make_grant_only_kiwoom_coordination_adapter(_us_bound_entry()),
            KIWOOM_COORDINATION_OWNER_LANE_MISMATCH,
        ),
    ],
)
def test_identity_rejection_is_recorded_not_silently_downgraded(
    candidate_factory,
    expected_code,  # noqa: ANN001
) -> None:
    owner, record = kiwoom_cycle._resolve_coordination_owner(
        coordination_factory=candidate_factory,
        expected_entry=bound_kiwoom_entry(),
    )
    assert owner is None
    assert record["present"] is False
    assert record["identity_guard"]["status"] == "rejected"
    assert record["identity_guard"]["code"] == expected_code


def test_identity_guard_rejects_account_fingerprint_mismatch_and_contract_mutation() -> (
    None
):
    entry = bound_kiwoom_entry()
    other_entry = replace(entry, physical_account_id="OTHER-KIWOOM-PHYSICAL-4242")
    other_adapter = make_grant_only_kiwoom_coordination_adapter(other_entry)
    with pytest.raises(KiwoomCoordinationOwnerRejected) as account_refusal:
        assert_kiwoom_coordination_owner(other_adapter, expected_entry=entry)
    assert account_refusal.value.code == KIWOOM_COORDINATION_OWNER_ACCOUNT_MISMATCH

    adapter = make_grant_only_kiwoom_coordination_adapter(entry)
    adapter.recovery_owner = "spoofed-owner"  # type: ignore[misc] — mutant
    with pytest.raises(KiwoomCoordinationOwnerRejected) as contract_refusal:
        assert_kiwoom_coordination_owner(adapter, expected_entry=entry)
    assert contract_refusal.value.code == KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH


def test_identity_guard_rejects_direct_adapter_without_factory_provenance() -> None:
    entry = bound_kiwoom_entry()
    direct = kiwoom_ordering.KiwoomCoordinationAdapter(
        make_grant_only_kiwoom_coordination_adapter(entry).ports
    )

    with pytest.raises(KiwoomCoordinationOwnerRejected) as refusal:
        assert_kiwoom_coordination_owner(direct, expected_entry=entry)
    assert refusal.value.code == KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED


def test_identity_guard_rejects_grant_only_flip() -> None:
    entry = bound_kiwoom_entry()
    adapter = make_grant_only_kiwoom_coordination_adapter(entry)
    adapter._grant_only = False  # type: ignore[misc] — mutant

    with pytest.raises(KiwoomCoordinationOwnerRejected) as refusal:
        assert_kiwoom_coordination_owner(adapter, expected_entry=entry)
    assert refusal.value.code == KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH


def test_identity_guard_rejects_subclass_class_swap() -> None:
    entry = bound_kiwoom_entry()
    swapped = _AdapterSubclass(make_grant_only_kiwoom_coordination_adapter(entry).ports)
    swapped.__class__ = kiwoom_ordering.KiwoomCoordinationAdapter

    with pytest.raises(KiwoomCoordinationOwnerRejected) as refusal:
        assert_kiwoom_coordination_owner(swapped, expected_entry=entry)
    assert refusal.value.code == KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED


def test_identity_guard_rejects_equal_but_distinct_entry_copy() -> None:
    entry = bound_kiwoom_entry()
    copied_entry = replace(entry)
    copied_adapter = make_grant_only_kiwoom_coordination_adapter(copied_entry)

    owner, record = kiwoom_cycle._resolve_coordination_owner(
        coordination_factory=lambda: copied_adapter,
        expected_entry=entry,
    )
    assert owner is None
    assert record["identity_guard"] == {
        "status": "rejected",
        "code": "coordination_owner_entry_mismatch",
        "owner_type": "KiwoomCoordinationAdapter",
    }


def test_unpinned_fabricated_entry_is_rejected_by_provenance() -> None:
    entry = bound_kiwoom_entry()
    fabricated = replace(entry, physical_account_id="ATTACKER-ACCOUNT-9999")
    fabricated_adapter = make_grant_only_kiwoom_coordination_adapter(fabricated)

    owner, record = kiwoom_cycle._resolve_coordination_owner(
        coordination_factory=lambda: fabricated_adapter,
        expected_entry=None,
    )
    assert owner is None
    assert record["identity_guard"] == {
        "status": "rejected",
        "code": KIWOOM_COORDINATION_OWNER_PROVENANCE_REJECTED,
        "owner_type": "KiwoomCoordinationAdapter",
    }


class _NoopLease:
    def acquire(self) -> None:
        return None

    def release(self) -> None:
        return None

    def assert_held(self) -> None:
        return None

    def canonical(self) -> dict[str, object]:
        return {"acquired": True, "test_lease": True}


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kiwoom_cycle.kiwoom_lane, "assert_kiwoom_lane_enabled", lambda: None
    )
    monkeypatch.setattr(
        kiwoom_cycle.kiwoom_lane,
        "account_identity_summary",
        lambda: {"fingerprint": "sha256:test-account", "product_suffix": "28"},
    )


@pytest.mark.asyncio
async def test_grant_only_preview_records_owner_without_order_or_broker_mutation(
    tmp_path: Path,
) -> None:
    table_dir = tmp_path / "tables"
    out_dir = tmp_path / "artifacts"
    _write_table(table_dir, generated_at=IN_SESSION - dt.timedelta(hours=4))
    entry = bound_kiwoom_entry()
    adapter = make_grant_only_kiwoom_coordination_adapter(entry)
    account = FakeAccount()
    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "own-orders.jsonl")
    before = len(journal.read_all())

    outcome = await kiwoom_cycle.run_kiwoom_cycle(
        now=IN_SESSION,
        table_dir=table_dir,
        out_dir=out_dir,
        account=account,
        journal=journal,
        coordination_factory=lambda: adapter,
        coordination_entry=entry,
    )

    coordination = outcome.record["coordination"]
    assert coordination["present"] is True
    assert coordination["recovery_owner"] == kiwoom_ordering.KIWOOM_RECOVERY_OWNER
    assert coordination["authorizes_send"] is False
    assert coordination["local_flock_authorizes_send"] is False
    assert coordination["identity_guard"]["status"] == "accepted"
    assert account.buy_calls == []
    assert account.sell_calls == []
    assert account.cancel_calls == []
    assert len(journal.read_all()) == before == 0
    assert outcome.record["submitted"] == []


@pytest.mark.asyncio
async def test_grant_only_owner_is_not_a_send_grant_and_fails_closed(
    tmp_path: Path, armed
) -> None:  # noqa: ANN001
    table_dir = tmp_path / "tables"
    out_dir = tmp_path / "artifacts"
    _write_table(table_dir, generated_at=IN_SESSION - dt.timedelta(hours=4))
    entry = bound_kiwoom_entry()
    adapter = make_grant_only_kiwoom_coordination_adapter(entry)
    account = FakeAccount()

    outcome = await kiwoom_cycle.run_kiwoom_cycle(
        now=IN_SESSION,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        ordering=True,
        account=account,
        lease_factory=lambda *_: _NoopLease(),
        coordination_factory=lambda: adapter,
        coordination_entry=entry,
        realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
            value=Decimal("0"), source="grant-only-test"
        ),
    )

    assert (
        outcome.zero_order_reason == kiwoom_cycle.COORDINATION_GRANT_UNAVAILABLE_REASON
    )
    assert account.buy_calls == []
    assert account.sell_calls == []
    assert account.cancel_calls == []
    assert outcome.record["coordination"]["present"] is True
    assert outcome.record["coordination"]["authorizes_send"] is False


@pytest.mark.asyncio
async def test_factory_creation_failure_is_explicit_and_zero_order(
    tmp_path: Path, armed
) -> None:  # noqa: ANN001
    table_dir = tmp_path / "tables"
    out_dir = tmp_path / "artifacts"
    _write_table(table_dir, generated_at=IN_SESSION - dt.timedelta(hours=4))
    account = FakeAccount()

    def fail_factory() -> object:
        raise RuntimeError("owner construction failed")

    outcome = await kiwoom_cycle.run_kiwoom_cycle(
        now=IN_SESSION,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        ordering=True,
        account=account,
        lease_factory=lambda *_: _NoopLease(),
        coordination_factory=fail_factory,
        realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
            value=Decimal("0"), source="factory-failure-test"
        ),
    )

    assert (
        outcome.zero_order_reason == kiwoom_cycle.COORDINATION_GRANT_UNAVAILABLE_REASON
    )
    assert account.buy_calls == []
    assert account.sell_calls == []
    assert account.cancel_calls == []
    guard = outcome.record["coordination"]["identity_guard"]
    assert guard["status"] == "rejected"
    assert guard["code"] == "RuntimeError"


def test_identity_rejection_codes_are_not_silent_none_downgrades() -> None:
    entry = bound_kiwoom_entry()
    adapter = make_grant_only_kiwoom_coordination_adapter(entry)
    adapter.blocked_state = "spoofed"  # type: ignore[misc] — mutant
    owner, record = kiwoom_cycle._resolve_coordination_owner(
        coordination_factory=lambda: adapter, expected_entry=entry
    )
    assert owner is None
    assert record["present"] is False
    assert record["identity_guard"] == {
        "status": "rejected",
        "code": KIWOOM_COORDINATION_OWNER_CONTRACT_MISMATCH,
        "owner_type": "KiwoomCoordinationAdapter",
    }
