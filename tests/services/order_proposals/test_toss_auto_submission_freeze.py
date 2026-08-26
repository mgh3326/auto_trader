"""Offline contracts for the narrowed same-session Toss fill interlock."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.review import TossLiveOrderLedger
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.service import RungInput
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = pytest.mark.asyncio


async def _seed_auto_resting_toss(
    db_session,
    *,
    account_id: str,
    identity: str,
    include_unfilled_sibling: bool = False,
):
    now = datetime(2026, 8, 26, 2, 38, tzinfo=UTC)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        broker_account_id=account_id,
        side="buy",
        order_type="limit",
        proposer="offline-toss-freeze-fixture",
        thesis="offline clean-full-fill freeze fixture",
        rungs=[
            RungInput(0, "buy", Decimal("1"), Decimal("97000"), None),
            *(
                [RungInput(1, "buy", Decimal("1"), Decimal("96000"), None)]
                if include_unfilled_sibling
                else []
            ),
        ],
        source_asof={
            "auto_approved": {
                "policy_version": "offline-fixture",
                "approved_at": now.isoformat(),
                "eligibility": [],
                "outcomes": ["submitted_resting"],
            }
        },
    )
    await service.transition_rung(group.proposal_id, 0, new_state="revalidating")
    await service.transition_rung(group.proposal_id, 0, new_state="approved")
    await service.transition_rung(group.proposal_id, 0, new_state="submitting")
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=f"broker-{identity}",
        correlation_id=f"correlation-{identity}",
        idempotency_key=f"client-{identity}",
        approval_hash_digest=f"digest-{identity}",
        now=now,
    )
    await db_session.commit()
    return service, group, now


async def _record_fill(
    service: OrderProposalsService,
    db_session,
    *,
    identity: str,
    filled_qty: Decimal,
    terminal_state: str,
    now: datetime,
):
    rung = await service.record_fill_evidence(
        broker_order_id=f"broker-{identity}",
        correlation_id=f"correlation-{identity}",
        idempotency_key=f"client-{identity}",
        filled_qty=filled_qty,
        terminal_state=terminal_state,  # type: ignore[arg-type]
        now=now,
        account_mode="toss_live",
    )
    await db_session.commit()
    assert rung is not None
    return rung


async def _record_clean_toss_place_ledger(
    db_session,
    *,
    identity: str,
    price: Decimal = Decimal("97000"),
) -> int:
    ledger_service = TossLiveOrderLedgerService(db_session)
    row = await ledger_service.record_send(
        operation_kind="place",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("1"),
        price=price,
        order_amount=None,
        currency="KRW",
        client_order_id=f"client-{identity}",
        broker_order_id=f"broker-{identity}",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        correlation_id=f"correlation-{identity}",
    )
    await ledger_service.update_reconcile_outcome(
        ledger_id=row.id,
        status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("1"),
        avg_fill_price=Decimal("97000"),
    )
    return row.id


async def _record_unresolved_toss_mutation_sibling(
    db_session,
    *,
    identity: str,
    operation_kind: str,
) -> None:
    """Record the D12c cancel/modify anomaly through the ledger service."""
    ledger_service = TossLiveOrderLedgerService(db_session)
    row = await ledger_service.record_send(
        operation_kind=operation_kind,
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("1"),
        price=Decimal("97000"),
        order_amount=None,
        currency="KRW",
        client_order_id=f"{operation_kind}-client-{identity}",
        broker_order_id=f"{operation_kind}-broker-{identity}",
        original_order_id=f"broker-{identity}",
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        correlation_id=f"{operation_kind}-correlation-{identity}",
    )
    await ledger_service.mark_manual_review(
        ledger_id=row.id,
        reason="offline unresolved mutation sibling",
        error={"code": "offline_unresolved_mutation_sibling"},
    )


@pytest.mark.asyncio
async def test_normal_full_fill_with_clean_toss_reconciliation_releases_freeze(
    db_session,
) -> None:
    """Only exact full reconciliation may release a same-session interlock."""
    identity = uuid.uuid4().hex
    account_id = f"offline-clean-{identity}"
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=account_id,
        identity=identity,
    )
    await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    await _record_clean_toss_place_ledger(db_session, identity=identity)

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze is None
    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    persisted = refreshed.source_asof["auto_approved"]["toss_auto_submission_freeze"]
    assert persisted["state"] == "resolved"
    assert persisted["resolution"]["reason"] == "normal_full_fill_reconciled"


@pytest.mark.parametrize("operation_kind", ("cancel", "modify"))
async def test_unresolved_linked_toss_mutation_sibling_keeps_freeze(
    db_session, operation_kind: str
) -> None:
    """D12c: a clean place row cannot hide its anomalous linked mutation."""
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-{operation_kind}-sibling-{identity}",
        identity=identity,
    )
    await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    await _record_clean_toss_place_ledger(db_session, identity=identity)
    await _record_unresolved_toss_mutation_sibling(
        db_session,
        identity=identity,
        operation_kind=operation_kind,
    )

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze is not None
    assert freeze["state"] == "frozen"


async def test_clean_full_fill_missing_reconciled_at_stays_frozen(db_session) -> None:
    """MUTATION-ANCHOR: s156-toss-freeze-reconciled-at."""
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-missing-reconciled-at-{identity}",
        identity=identity,
    )
    await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    ledger_id = await _record_clean_toss_place_ledger(db_session, identity=identity)
    row = await db_session.get(TossLiveOrderLedger, ledger_id)
    assert row is not None
    row.reconciled_at = None
    await db_session.commit()

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze is not None, "MUTATION-ANCHOR: s156-toss-freeze-reconciled-at"
    assert freeze["state"] == "frozen"


@pytest.mark.parametrize(
    ("target", "field", "value"),
    (
        ("rung", "filled_qty", Decimal("2")),
        ("ledger", "filled_qty", Decimal("2")),
        ("ledger", "avg_fill_price", Decimal("97001")),
        ("ledger", "requires_manual_review", True),
        ("ledger", "last_reconcile_error", {"code": "offline_residue"}),
        ("ledger", "broker_status", "PARTIALLY_FILLED"),
        ("ledger", "side", "sell"),
        ("ledger", "quantity", Decimal("2")),
        ("ledger", "symbol", "000660"),
        ("ledger", "market", "us"),
        ("ledger", "avg_fill_price", Decimal("0")),
        ("ledger", "status", "anomaly"),
    ),
    ids=(
        "rung-filled-qty",
        "ledger-filled-qty",
        "worse-buy-fill-price",
        "manual-review",
        "reconcile-error",
        "broker-partial",
        "ledger-side",
        "ledger-quantity",
        "ledger-symbol",
        "ledger-market",
        "zero-average-fill",
        "ledger-anomaly",
    ),
)
async def test_toss_full_fill_conjunction_violations_stay_frozen(
    db_session,
    target: str,
    field: str,
    value: object,
) -> None:
    """The ④ release conjunction rejects each malformed rung/ledger fact."""
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-conjunction-{identity}",
        identity=identity,
    )
    await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    ledger_id = await _record_clean_toss_place_ledger(db_session, identity=identity)
    if target == "rung":
        _group, rungs = await service.get_proposal(group.proposal_id)
        setattr(rungs[0], field, value)
    else:
        row = await db_session.get(TossLiveOrderLedger, ledger_id)
        assert row is not None
        setattr(row, field, value)
    await db_session.commit()

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze is not None
    assert freeze["state"] == "frozen"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("session_date", "not-a-date"),
        ("state", "cleared"),
    ),
    ids=("malformed-session-date", "unknown-state"),
)
async def test_malformed_toss_freeze_control_evidence_fails_closed(
    db_session,
    field: str,
    value: str,
) -> None:
    """D14/D20: malformed durable control evidence cannot release the lane."""
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-malformed-freeze-{identity}",
        identity=identity,
    )
    await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    await _record_clean_toss_place_ledger(db_session, identity=identity)
    frozen_group, _rungs = await service.get_proposal(group.proposal_id)
    source_asof = dict(frozen_group.source_asof or {})
    auto = dict(source_asof["auto_approved"])
    freeze_record = dict(auto["toss_auto_submission_freeze"])
    freeze_record[field] = value
    auto["toss_auto_submission_freeze"] = freeze_record
    source_asof["auto_approved"] = auto
    await service._repo.update_group(frozen_group, source_asof=source_asof)
    await db_session.commit()

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze == {
        "state": "frozen",
        "reason": "toss_reconciliation_lookup_unavailable",
        "session_date": "2026-08-26",
        "market": "equity_kr",
    }


async def test_resolved_toss_freeze_reopens_when_ledger_evidence_turns_unsafe(
    db_session,
) -> None:
    """D13: a later anomalous ledger record re-closes a prior release."""
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-reopen-freeze-{identity}",
        identity=identity,
    )
    await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    ledger_id = await _record_clean_toss_place_ledger(db_session, identity=identity)

    assert await service.active_toss_auto_submission_freeze(group, now=now) is None
    row = await db_session.get(TossLiveOrderLedger, ledger_id)
    assert row is not None
    row.status = "anomaly"
    await db_session.commit()

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze is not None
    assert freeze["state"] == "frozen"
    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    persisted = refreshed.source_asof["auto_approved"]["toss_auto_submission_freeze"]
    assert persisted["state"] == "frozen"
    assert persisted["reopened_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_unresolved_partial_toss_fill_remains_account_wide_frozen(
    db_session,
) -> None:
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-partial-{identity}",
        identity=identity,
    )
    rung = await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("0.4"),
        terminal_state="partially_filled",
        now=now,
    )

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert rung.state == "partially_filled"
    assert rung.filled_qty == Decimal("0.4")
    assert freeze is not None
    assert freeze["state"] == "frozen"


@pytest.mark.asyncio
async def test_unfilled_sibling_rung_keeps_full_fill_indeterminate_and_frozen(
    db_session,
) -> None:
    """A group clears only when every rung is fully reconciled."""
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-unfilled-sibling-{identity}",
        identity=identity,
        include_unfilled_sibling=True,
    )
    await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    await _record_clean_toss_place_ledger(db_session, identity=identity)

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze is not None
    assert freeze["state"] == "frozen"


@pytest.mark.asyncio
async def test_unexpected_or_unavailable_toss_reconciliation_remains_frozen(
    db_session,
) -> None:
    """MUTATION-ANCHOR: s156-toss-freeze-fail-closed-unavailable."""
    unexpected_identity = uuid.uuid4().hex
    unexpected_service, unexpected_group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-unexpected-{unexpected_identity}",
        identity=unexpected_identity,
    )
    await _record_fill(
        unexpected_service,
        db_session,
        identity=unexpected_identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    # A limit-price mismatch is an unexpected fill, not clearance.
    await _record_clean_toss_place_ledger(
        db_session,
        identity=unexpected_identity,
        price=Decimal("97100"),
    )

    unexpected = await unexpected_service.active_toss_auto_submission_freeze(
        unexpected_group,
        now=now,
    )

    assert unexpected is not None
    assert unexpected["state"] == "frozen"

    unavailable_identity = uuid.uuid4().hex
    unavailable_service, unavailable_group, _ = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-unavailable-{unavailable_identity}",
        identity=unavailable_identity,
    )
    await _record_fill(
        unavailable_service,
        db_session,
        identity=unavailable_identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )

    unavailable = await unavailable_service.active_toss_auto_submission_freeze(
        unavailable_group,
        now=now,
    )

    assert unavailable is not None
    assert unavailable["state"] == "frozen"


@pytest.mark.asyncio
async def test_toss_reconciliation_lookup_error_fails_closed_to_freeze(
    monkeypatch,
    db_session,
) -> None:
    """MUTATION-ANCHOR: s156-toss-freeze-fail-closed-lookup-error."""
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-lookup-error-{identity}",
        identity=identity,
    )
    await _record_fill(
        service,
        db_session,
        identity=identity,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )

    async def unavailable_rungs(_proposal_pk: int):
        raise RuntimeError("offline injected ledger lookup failure")

    monkeypatch.setattr(service._repo, "list_rungs", unavailable_rungs)

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze == {
        "state": "frozen",
        "reason": "toss_reconciliation_lookup_unavailable",
        "session_date": "2026-08-26",
        "market": "equity_kr",
    }


@pytest.mark.asyncio
async def test_malformed_auto_evidence_fails_closed_to_freeze(db_session) -> None:
    """Partial control-plane evidence cannot be interpreted as no freeze."""
    identity = uuid.uuid4().hex
    service, group, now = await _seed_auto_resting_toss(
        db_session,
        account_id=f"offline-malformed-auto-{identity}",
        identity=identity,
    )
    await service._repo.update_group(group, source_asof={"auto_approved": []})
    await db_session.commit()

    freeze = await service.active_toss_auto_submission_freeze(group, now=now)

    assert freeze == {
        "state": "frozen",
        "reason": "toss_reconciliation_lookup_unavailable",
        "session_date": "2026-08-26",
        "market": "equity_kr",
    }
