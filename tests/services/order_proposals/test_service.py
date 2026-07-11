import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.db import AsyncSessionLocal
from app.core.timezone import KST
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import (
    OrderProposalError,
    OrderProposalInvalidStateTransition,
    OrderProposalNotFound,
)
from app.services.order_proposals.service import RungInput


def _target_snapshot_payload(**overrides):
    payload = {
        "broker_order_id": "manual-upbit-1",
        "symbol": "KRW-AVAX",
        "side": "sell",
        "order_type": "limit",
        "limit_price": "42000",
        "remaining_quantity": "3.5",
        "status": "open",
        "observed_at": "2026-07-11T08:23:00+00:00",
    }
    payload.update(overrides)
    return payload


def _target_action_create_kwargs(action: str, **overrides):
    kwargs = {
        "symbol": "KRW-AVAX",
        "market": "crypto",
        "account_mode": "upbit",
        "side": "sell",
        "order_type": "limit",
        "proposer": "p",
        "action": action,
        "target_broker_order_id": "manual-upbit-1",
        "target_order_snapshot": _target_snapshot_payload(),
        "rungs": [RungInput(0, "sell", Decimal("3.5"), Decimal("42000"), None)],
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_target_mutation_lock_serializes_same_broker_order():
    target = SimpleNamespace(
        action="replace",
        account_mode="upbit",
        market="crypto",
        broker_account_id=None,
        target_broker_order_id=f"manual-{uuid.uuid4()}",
    )

    async with (
        AsyncSessionLocal() as first_session,
        AsyncSessionLocal() as second_session,
    ):
        first = OrderProposalsService(first_session)
        second = OrderProposalsService(second_session)

        assert await first.acquire_target_mutation_lock(target) is True
        waiter = asyncio.create_task(second.acquire_target_mutation_lock(target))
        await asyncio.sleep(0.05)
        assert waiter.done() is False

        await first_session.commit()
        assert await asyncio.wait_for(waiter, timeout=1) is True
        await second_session.rollback()


@pytest.mark.asyncio
async def test_place_still_allows_multiple_rungs_and_persists_normalized_action(
    db_session,
):
    group = await OrderProposalsService(db_session).create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        action="place",
        source_asof={"quote_asof": "2026-07-11T08:23:00+00:00"},
        rungs=[
            RungInput(0, "buy", Decimal("1"), Decimal("70000"), None),
            RungInput(1, "buy", Decimal("1"), Decimal("69000"), None),
        ],
    )

    assert group.action == "place"
    assert group.target_broker_order_id is None
    assert group.source_asof == {"quote_asof": "2026-07-11T08:23:00+00:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_target_actions_require_exactly_one_rung(db_session, action):
    with pytest.raises(OrderProposalError, match="exactly one rung"):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(
                action,
                rungs=[
                    RungInput(0, "sell", Decimal("3.5"), Decimal("42000"), None),
                    RungInput(1, "sell", Decimal("1"), Decimal("41000"), None),
                ],
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"target_broker_order_id": None},
        {"target_order_snapshot": None},
    ],
)
async def test_target_actions_require_target_broker_evidence(
    db_session, action, overrides
):
    with pytest.raises(OrderProposalError, match="requires target broker evidence"):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(action, **overrides)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
@pytest.mark.parametrize("status", ["filled", "cancelled", "expired", "rejected"])
async def test_target_actions_reject_non_open_target_at_create(
    db_session, action, status
):
    with pytest.raises(OrderProposalError, match="target broker order must be open"):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(
                action,
                target_order_snapshot=_target_snapshot_payload(
                    status=status, remaining_quantity="0"
                ),
            )
        )


@pytest.mark.asyncio
async def test_place_rejects_target_broker_evidence(db_session):
    with pytest.raises(OrderProposalError, match="cannot target a broker order"):
        await OrderProposalsService(db_session).create_proposal(
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="p",
            target_broker_order_id="old-1",
            target_order_snapshot=_target_snapshot_payload(),
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("70000"), None)],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_target_actions_reject_unsupported_account_market_tuple(
    db_session, action
):
    with pytest.raises(
        OrderProposalError, match="unsupported account_mode/market/action"
    ):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(action, account_mode="kis_mock")
        )


@pytest.mark.asyncio
async def test_cancel_rejects_rung_that_differs_from_target_snapshot(db_session):
    with pytest.raises(
        OrderProposalError, match="cancel rung must equal target broker snapshot"
    ):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(
                "cancel",
                rungs=[RungInput(0, "sell", Decimal("3.4"), Decimal("42000"), None)],
            )
        )


@pytest.mark.asyncio
async def test_replace_persists_target_snapshot_and_allows_independent_proposals(
    db_session,
):
    service = OrderProposalsService(db_session)
    first = await service.create_proposal(
        **_target_action_create_kwargs("replace", source_asof={"origin": "manual"})
    )
    second = await service.create_proposal(
        **_target_action_create_kwargs(
            "replace",
            target_broker_order_id="manual-upbit-2",
            target_order_snapshot=_target_snapshot_payload(
                broker_order_id="manual-upbit-2"
            ),
        )
    )

    assert first.proposal_id != second.proposal_id
    assert first.action == second.action == "replace"
    assert first.target_broker_order_id == "manual-upbit-1"
    assert first.source_asof == {
        "origin": "manual",
        "target_order_snapshot": _target_snapshot_payload(),
    }
    assert first.payload_hash != second.payload_hash


async def _create_single_rung(db_session, *, symbol: str = "A"):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    return service, group


async def _create_cancel_proposal(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(**_target_action_create_kwargs("cancel"))
    await db_session.commit()
    return service, group


async def _drive_to_submitting(service, proposal_id):
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(proposal_id, 0, new_state=state)


async def _record_ack(service, proposal_id, *, now: datetime):
    await _drive_to_submitting(service, proposal_id)
    return await service.record_ack(
        proposal_id,
        0,
        broker_order_id=f"B-ACK-{proposal_id}",
        correlation_id=f"corr-ack-{proposal_id}",
        idempotency_key=f"idem-ack-{proposal_id}",
        approval_hash_digest=f"digest-ack-{proposal_id}",
        now=now,
    )


async def _record_resting(service, proposal_id, *, now: datetime):
    await _drive_to_submitting(service, proposal_id)
    return await service.record_resting(
        proposal_id,
        0,
        broker_order_id=f"B-REST-{proposal_id}",
        correlation_id=f"corr-rest-{proposal_id}",
        idempotency_key=f"idem-rest-{proposal_id}",
        approval_hash_digest=f"digest-rest-{proposal_id}",
        now=now,
    )


def _retro(*, symbol="005930", trigger_type="stop_loss", created_at=None):
    return SimpleNamespace(
        symbol=symbol,
        trigger_type=trigger_type,
        created_at=created_at or datetime.now(UTC),
    )


def _loss_cut_create_kwargs(*, now: datetime):
    return {
        "symbol": "005930",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "side": "sell",
        "order_type": "limit",
        "proposer": "p",
        "rungs": [RungInput(0, "sell", Decimal("1"), Decimal("65000"), None)],
        "exit_intent": "loss_cut",
        "exit_reason": "stop_loss",
        "retrospective_id": 42,
        "approval_issue_id": "ROB-800",
        "now": now,
    }


@pytest.mark.asyncio
async def test_create_defaults_valid_until_to_next_kst_midnight(db_session):
    now = datetime(2026, 7, 11, 14, 30, tzinfo=KST)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("70000"), None)],
        now=now,
    )
    assert group.valid_until == datetime(2026, 7, 12, 0, 0, tzinfo=KST)


@pytest.mark.asyncio
async def test_loss_cut_requires_all_group_fields_without_paperclip_lookup(
    db_session, monkeypatch
):
    async def paperclip_must_not_run(*args, **kwargs):
        raise AssertionError("Paperclip status belongs to click-time revalidation")

    monkeypatch.setattr(
        "app.mcp_server.tooling.order_validation._fetch_approval_issue_status",
        paperclip_must_not_run,
    )
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError, match="exit_reason"):
        await service.create_proposal(
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            side="sell",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("65000"), None)],
            exit_intent="loss_cut",
            retrospective_id=42,
            approval_issue_id="ROB-800",
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"retrospective_id": None}, "retrospective_id"),
        ({"approval_issue_id": None}, "approval_issue_id"),
        ({"exit_reason": None}, "exit_reason"),
        ({"exit_intent": "emergency"}, "unknown exit_intent"),
    ],
)
async def test_loss_cut_required_fields_fail_closed(db_session, overrides, message):
    service = OrderProposalsService(db_session)
    kwargs = _loss_cut_create_kwargs(now=datetime.now(UTC))
    kwargs.update(overrides)
    with pytest.raises(OrderProposalError, match=message):
        await service.create_proposal(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retro", "message"),
    [
        (None, "not found"),
        (_retro(symbol="000660"), "symbol mismatch"),
        (_retro(trigger_type="fill"), "trigger_type"),
        (_retro(created_at=datetime.now(UTC) - timedelta(hours=73)), "stale"),
    ],
)
async def test_loss_cut_retrospective_validation(
    db_session, monkeypatch, retro, message
):
    async def fake_lookup(session, retrospective_id):
        return retro

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    with pytest.raises(OrderProposalError, match=message):
        await OrderProposalsService(db_session).create_proposal(
            **_loss_cut_create_kwargs(now=datetime.now(UTC))
        )


@pytest.mark.asyncio
async def test_valid_loss_cut_persists_exact_group_binding(db_session, monkeypatch):
    async def fake_lookup(session, retrospective_id):
        return _retro()

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    group = await OrderProposalsService(db_session).create_proposal(
        **_loss_cut_create_kwargs(now=datetime.now(UTC))
    )
    assert (
        group.exit_intent,
        group.exit_reason,
        group.retrospective_id,
        group.approval_issue_id,
    ) == ("loss_cut", "stop_loss", 42, "ROB-800")


@pytest.mark.asyncio
async def test_upbit_crypto_loss_cut_is_valid(db_session, monkeypatch):
    async def fake_lookup(session, retrospective_id):
        return _retro(symbol="KRW-DOT")

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    group = await OrderProposalsService(db_session).create_proposal(
        symbol="KRW-DOT",
        market="crypto",
        account_mode="upbit",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("0.1"), Decimal("3200"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-800",
        now=datetime.now(UTC),
    )
    assert group.exit_intent == "loss_cut"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "symbol"),
    [("equity_kr", "005930"), ("equity_us", "AAPL")],
)
async def test_toss_live_loss_cut_remains_unsupported(
    db_session, monkeypatch, market, symbol
):
    async def fake_lookup(session, retrospective_id):
        return _retro(symbol=symbol)

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    with pytest.raises(
        OrderProposalError,
        match="loss_cut requires a supported live account and market",
    ):
        await OrderProposalsService(db_session).create_proposal(
            symbol=symbol,
            market=market,
            account_mode="toss_live",
            side="sell",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("100"), None)],
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            approval_issue_id="ROB-800",
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_create_and_get_multi_rung(db_session):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="operator:sess-1",
        thesis="support bounce",
        strategy="ladder",
        rungs=[
            RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None),
            RungInput(1, "buy", Decimal("5"), Decimal("2200000"), None),
        ],
    )
    await db_session.commit()
    assert group.lifecycle_state == "proposed"
    assert group.root_proposal_id == group.proposal_id
    assert group.payload_hash and len(group.payload_hash) == 64

    fetched, rungs = await svc.get_proposal(group.proposal_id)
    assert fetched.id == group.id
    assert [r.rung_index for r in rungs] == [0, 1]
    assert all(r.state == "pending_approval" for r in rungs)


@pytest.mark.asyncio
async def test_get_missing_raises(db_session):
    svc = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalNotFound):
        await svc.get_proposal(uuid.uuid4())


@pytest.mark.asyncio
async def test_rung_transition_enforces_state_machine(db_session):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    # illegal: pending_approval -> filled
    with pytest.raises(OrderProposalInvalidStateTransition):
        await svc.transition_rung(group.proposal_id, 0, new_state="filled")


@pytest.mark.asyncio
async def test_replacement_lineage_supersedes_original(db_session):
    svc = OrderProposalsService(db_session)
    original = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()
    replacement = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2340000"), None)],
        supersedes_proposal_id=original.proposal_id,
    )
    await db_session.commit()
    orig_after, _ = await svc.get_proposal(original.proposal_id)
    assert orig_after.lifecycle_state == "superseded"
    assert orig_after.superseded_by_proposal_id == replacement.proposal_id
    assert replacement.root_proposal_id == original.root_proposal_id
    assert replacement.payload_hash != original.payload_hash


@pytest.mark.asyncio
async def test_approval_nonce_mismatch_and_reset(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)

    result = await service.set_approval_nonce(group.proposal_id, "nonce-1")
    assert result is None
    await service.consume_approval_nonce(group.proposal_id, "nonce-1", now=now)

    with pytest.raises(OrderProposalError, match="^nonce_mismatch$"):
        await service.consume_approval_nonce(group.proposal_id, "wrong-nonce", now=now)

    await service.set_approval_nonce(group.proposal_id, "nonce-2")
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce == "nonce-2"
    assert refreshed.approval_nonce_used_at is None


@pytest.mark.asyncio
async def test_approval_nonce_replay_blocked(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 1, tzinfo=UTC)
    await service.set_approval_nonce(group.proposal_id, "nonce-1")
    await db_session.commit()

    consumed = await service.consume_approval_nonce(
        group.proposal_id, "nonce-1", now=now
    )
    assert consumed.approval_nonce_used_at == now
    await db_session.commit()

    with pytest.raises(OrderProposalError, match="^nonce_replay$"):
        await service.consume_approval_nonce(
            group.proposal_id, "nonce-1", now=now + timedelta(seconds=1)
        )


@pytest.mark.asyncio
async def test_expire_if_needed_terminalizes_pending_rungs_and_nonce(db_session):
    service, group = await _create_single_rung(db_session)
    await service.set_approval_nonce(group.proposal_id, "nonce")
    group.valid_until = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    assert await service.expire_if_needed(
        group.proposal_id, now=datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    )
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.lifecycle_state == "expired"
    assert refreshed.approval_nonce is None
    assert [r.state for r in rungs] == ["expired"]


@pytest.mark.asyncio
async def test_void_refuses_unverified_rung_without_partial_mutation(db_session):
    service, group = await _create_single_rung(db_session)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id, 0, reason="unknown", now=datetime.now(UTC)
    )
    with pytest.raises(OrderProposalError, match="cannot void"):
        await service.void_proposal(
            group.proposal_id, reason="operator cleanup", now=datetime.now(UTC)
        )


@pytest.mark.asyncio
async def test_void_multi_rung_sets_audit_and_invalidates_nonce(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(0, "buy", Decimal("1"), Decimal("70000"), None),
            RungInput(1, "buy", Decimal("1"), Decimal("69000"), None),
        ],
    )
    await service.set_approval_nonce(group.proposal_id, "nonce")
    rows = await service.void_proposal(
        group.proposal_id, reason="thesis invalidated", now=datetime.now(UTC)
    )
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert [row.state for row in rows] == ["voided", "voided"]
    assert refreshed.lifecycle_state == "voided"
    assert refreshed.no_resubmit is True
    assert refreshed.void_reason == "thesis invalidated"
    assert refreshed.approval_nonce is None


@pytest.mark.asyncio
async def test_expire_if_needed_before_deadline_is_noop(db_session):
    service, group = await _create_single_rung(db_session)
    group.valid_until = datetime.now(UTC) + timedelta(minutes=1)
    assert not await service.expire_if_needed(group.proposal_id, now=datetime.now(UTC))


@pytest.mark.asyncio
async def test_record_approval_sets_telegram_user_and_timestamp(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 1, 30, tzinfo=UTC)

    updated = await service.record_approval(
        group.proposal_id, telegram_user_id="tg-12345", now=now
    )

    assert updated.approved_by_telegram_user_id == "tg-12345"
    assert updated.approved_at == now

    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approved_by_telegram_user_id == "tg-12345"
    assert refreshed.approved_at == now


@pytest.mark.asyncio
async def test_record_approval_missing_proposal_raises(db_session):
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalNotFound):
        await service.record_approval(
            uuid.uuid4(),
            telegram_user_id="tg-1",
            now=datetime(2026, 7, 10, 9, 1, 31, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_commit_lease_blocks_active_and_reacquires_expired(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 2, tzinfo=UTC)

    assert await service.acquire_commit_lease(
        group.proposal_id, now=now, lease_seconds=10
    )
    assert not await service.acquire_commit_lease(
        group.proposal_id, now=now + timedelta(seconds=9), lease_seconds=10
    )
    assert await service.acquire_commit_lease(
        group.proposal_id, now=now + timedelta(seconds=10), lease_seconds=5
    )

    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.commit_lease_until == now + timedelta(seconds=15)


@pytest.mark.asyncio
async def test_commit_lease_requires_timezone_aware_now(db_session):
    service, group = await _create_single_rung(db_session)

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.acquire_commit_lease(
            group.proposal_id, now=datetime(2026, 7, 10, 9, 2)
        )


@pytest.mark.asyncio
async def test_ack_is_accepted_not_filled_and_records_audit_fields(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 3, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)

    rung = await service.record_ack(
        group.proposal_id,
        0,
        broker_order_id="B1",
        correlation_id="corr1",
        idempotency_key="idem1",
        approval_hash_digest="digest1",
        now=now,
    )

    assert rung.state == "acked"
    assert rung.broker_order_id == "B1"
    assert rung.correlation_id == "corr1"
    assert rung.idempotency_key == "idem1"
    assert rung.approval_hash_digest == "digest1"
    assert rung.validated_at == now
    assert rung.updated_at == now
    assert rung.filled_qty is None


@pytest.mark.asyncio
async def test_resting_is_not_filled_and_records_audit_fields(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 4, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)

    rung = await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id="B2",
        correlation_id="corr2",
        idempotency_key="idem2",
        approval_hash_digest="digest2",
        now=now,
    )

    assert rung.state == "resting"
    assert rung.broker_order_id == "B2"
    assert rung.correlation_id == "corr2"
    assert rung.idempotency_key == "idem2"
    assert rung.approval_hash_digest == "digest2"
    assert rung.validated_at == now
    assert rung.updated_at == now
    assert rung.filled_qty is None


@pytest.mark.asyncio
async def test_record_cancelled_retains_target_id(db_session):
    service, group = await _create_cancel_proposal(db_session)
    now = datetime(2026, 7, 10, 9, 5, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)

    rung = await service.record_cancelled(
        group.proposal_id, 0, broker_order_id="old-1", now=now
    )

    assert rung.state == "cancelled"
    assert rung.broker_order_id == "old-1"
    assert rung.validated_at == now
    assert rung.updated_at == now


@pytest.mark.asyncio
async def test_record_cancelled_rejects_naive_now(db_session):
    service, group = await _create_cancel_proposal(db_session)
    await _drive_to_submitting(service, group.proposal_id)

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.record_cancelled(
            group.proposal_id,
            0,
            broker_order_id="old-1",
            now=datetime.now(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["submitting", "acked", "resting"])
async def test_record_unverified_holds_for_later_evidence(db_session, source_state):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 5, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)
    if source_state == "acked":
        await service.record_ack(
            group.proposal_id,
            0,
            broker_order_id="B3",
            correlation_id="corr3",
            idempotency_key="idem3",
            approval_hash_digest="digest3",
            now=now,
        )
    elif source_state == "resting":
        await service.record_resting(
            group.proposal_id,
            0,
            broker_order_id="B3",
            correlation_id="corr3",
            idempotency_key="idem3",
            approval_hash_digest="digest3",
            now=now,
        )

    rung = await service.record_unverified(
        group.proposal_id,
        0,
        reason="broker_timeout",
        now=now + timedelta(seconds=1),
    )

    assert rung.state == "unverified"
    assert rung.void_reason == "broker_timeout"
    assert rung.validated_at == now + timedelta(seconds=1)
    assert rung.updated_at == now + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_fill_evidence_books_by_correlation_id(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 6, tzinfo=UTC)
    correlation_id = f"corr9-{group.proposal_id}"
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=f"B9-{group.proposal_id}",
        correlation_id=correlation_id,
        idempotency_key=f"idem9-{group.proposal_id}",
        approval_hash_digest=f"digest9-{group.proposal_id}",
        now=now,
    )
    await db_session.commit()

    booked = await service.record_fill_evidence(
        correlation_id=correlation_id, filled_qty=Decimal("1"), now=now
    )

    assert booked is not None
    assert booked.state == "filled"
    assert booked.filled_qty == Decimal("1")
    assert booked.updated_at == now


@pytest.mark.asyncio
async def test_fill_evidence_books_partial_then_filled_by_broker_order_id(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 7, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await db_session.commit()

    partial = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.25"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=1),
    )
    assert partial is not None
    assert partial.state == "partially_filled"
    assert partial.filled_qty == Decimal("0.25")

    filled = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("1"),
        now=now + timedelta(seconds=2),
    )
    assert filled is not None
    assert filled.state == "filled"
    assert filled.filled_qty == Decimal("1")
    assert filled.updated_at == now + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_fill_evidence_resolves_unverified_rung(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 8, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await service.record_unverified(
        group.proposal_id, 0, reason="unknown", now=now + timedelta(seconds=1)
    )
    await db_session.commit()

    booked = await service.record_fill_evidence(
        correlation_id=acked.correlation_id,
        filled_qty=Decimal("1"),
        now=now + timedelta(seconds=2),
    )

    assert booked is not None
    assert booked.state == "filled"
    assert booked.filled_qty == Decimal("1")


@pytest.mark.asyncio
async def test_fill_evidence_missing_match_is_noop(db_session):
    service, _ = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 9, tzinfo=UTC)
    missing = f"missing-{uuid.uuid4()}"

    assert (
        await service.record_fill_evidence(
            correlation_id=missing, filled_qty=Decimal("1"), now=now
        )
        is None
    )
    assert (
        await service.record_fill_evidence(
            broker_order_id=missing, filled_qty=Decimal("1"), now=now
        )
        is None
    )
    assert await service.record_fill_evidence(filled_qty=Decimal("1"), now=now) is None


@pytest.mark.asyncio
async def test_fill_evidence_records_cancelled_terminal(db_session):
    """ROB-816 PR-3c: broker cancel evidence converges a resting rung to
    `cancelled` (the fa0dab30 canary scenario), with no filled_qty required."""
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
    rested = await _record_resting(service, group.proposal_id, now=now)
    await db_session.commit()

    cancelled = await service.record_fill_evidence(
        correlation_id=rested.correlation_id,
        terminal_state="cancelled",
        now=now + timedelta(seconds=1),
    )

    assert cancelled is not None
    assert cancelled.state == "cancelled"
    assert cancelled.updated_at == now + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_fill_evidence_cancel_after_partial_keeps_filled_qty(db_session):
    """A partially-filled rung that is later cancelled keeps the quantity that
    actually filled — cancel evidence must not zero out the partial fill."""
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 1, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await db_session.commit()
    await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.25"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=1),
    )

    cancelled = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        terminal_state="cancelled",
        now=now + timedelta(seconds=2),
    )

    assert cancelled is not None
    assert cancelled.state == "cancelled"
    assert cancelled.filled_qty == Decimal("0.25")


@pytest.mark.asyncio
async def test_fill_evidence_on_terminal_rung_short_circuits(db_session):
    """Re-evidence flowing into an already-terminal rung must short-circuit to a
    no-op — never raise InvalidStateTransition (which the reconcile kernel would
    otherwise mislabel as an anomaly)."""
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 2, tzinfo=UTC)
    rested = await _record_resting(service, group.proposal_id, now=now)
    await db_session.commit()

    first = await service.record_fill_evidence(
        broker_order_id=rested.broker_order_id, terminal_state="cancelled", now=now
    )
    assert first is not None and first.state == "cancelled"

    # A second reconcile pass re-delivers the same cancel evidence.
    again = await service.record_fill_evidence(
        broker_order_id=rested.broker_order_id, terminal_state="cancelled", now=now
    )
    assert again is None

    # A late-arriving fill against the same terminal rung must also no-op.
    late = await service.record_fill_evidence(
        broker_order_id=rested.broker_order_id,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    assert late is None


@pytest.mark.asyncio
async def test_fill_evidence_rechecks_committed_state_under_lock(db_session):
    """Concurrency invariant: once another session has committed a terminal fill,
    late/partial evidence arriving on a session that observed the rung earlier
    must short-circuit — it must never regress the already-`filled` rung back to
    `partially_filled`. Guards the record_fill_evidence lock + refresh re-check."""
    from app.mcp_server.tooling.live_order_ledger import _order_session_factory

    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 4, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await db_session.commit()

    # Prime THIS session's identity map with the rung while it is still 'acked'.
    await service.get_proposal(group.proposal_id)

    # A concurrent session commits the terminal fill.
    async with _order_session_factory()() as db2:
        other = OrderProposalsService(db2)
        await other.record_fill_evidence(
            broker_order_id=acked.broker_order_id,
            filled_qty=Decimal("1"),
            terminal_state="filled",
            now=now + timedelta(seconds=1),
        )
        await db2.commit()

    # Late partial evidence arriving on the stale session must short-circuit,
    # never regress the committed `filled` rung back to `partially_filled`.
    result = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.25"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=2),
    )
    assert result is None

    async with _order_session_factory()() as db3:
        _, rungs = await OrderProposalsService(db3).get_proposal(group.proposal_id)
        assert rungs[0].state == "filled"
        assert rungs[0].filled_qty == Decimal("1")


@pytest.mark.asyncio
async def test_fill_evidence_repeated_partial_refreshes_qty(db_session):
    """A second partial-fill evidence on an already-partially-filled rung
    refreshes the cumulative quantity without an (illegal) self-transition."""
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 3, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await db_session.commit()

    await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.25"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=1),
    )
    refreshed = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.5"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=2),
    )

    assert refreshed is not None
    assert refreshed.state == "partially_filled"
    assert refreshed.filled_qty == Decimal("0.5")
    assert refreshed.updated_at == now + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_mark_needs_reconfirm_bumps_revision(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 10, tzinfo=UTC)
    await service.transition_rung(
        group.proposal_id,
        0,
        new_state="revalidating",
        approval_revision=2,
    )

    rung = await service.mark_needs_reconfirm(group.proposal_id, 0, now=now)

    assert rung.state == "needs_reconfirm"
    assert rung.approval_revision == 3
    assert rung.validated_at == now
    assert rung.updated_at == now


@pytest.mark.asyncio
async def test_record_rejected_records_reason_from_legal_state(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 11, tzinfo=UTC)

    rung = await service.record_rejected(
        group.proposal_id, 0, reason="operator_denied", now=now
    )

    assert rung.state == "rejected"
    assert rung.void_reason == "operator_denied"
    assert rung.updated_at == now


@pytest.mark.asyncio
async def test_record_rejected_does_not_bypass_state_machine(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 12, tzinfo=UTC)
    await _record_ack(service, group.proposal_id, now=now)

    with pytest.raises(OrderProposalInvalidStateTransition):
        await service.record_rejected(
            group.proposal_id, 0, reason="late_denial", now=now
        )


@pytest.mark.asyncio
async def test_sweep_local_stale_only_voids_evidence_absent(db_session):
    service = OrderProposalsService(db_session)
    groups = []
    rung_ids = []
    for symbol in ("NO_ORDER", "TIMEOUT", "UNKNOWN"):
        group = await service.create_proposal(
            symbol=symbol,
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )
        _, rungs = await service.get_proposal(group.proposal_id)
        groups.append(group)
        rung_ids.append(rungs[0].id)

    with_broker = await service.create_proposal(
        symbol="HAS_BROKER",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await service.transition_rung(
        with_broker.proposal_id,
        0,
        new_state="revalidating",
        broker_order_id="B-present",
    )
    await service.transition_rung(
        with_broker.proposal_id, 0, new_state="pending_approval"
    )
    _, with_broker_rungs = await service.get_proposal(with_broker.proposal_id)
    not_pending = await service.create_proposal(
        symbol="REVALIDATING",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await service.transition_rung(not_pending.proposal_id, 0, new_state="revalidating")
    _, not_pending_rungs = await service.get_proposal(not_pending.proposal_id)
    await db_session.commit()

    evidence_by_rung = dict(
        zip(rung_ids, ("no_broker_order", "timeout", "unknown"), strict=True)
    )
    called = []

    async def broker_evidence(rung):
        called.append(rung.id)
        return evidence_by_rung.get(rung.id, "unknown")

    now = datetime(2026, 7, 10, 9, 13, tzinfo=UTC)
    swept = await service.sweep_local_stale(now=now, broker_evidence=broker_evidence)

    assert swept == [groups[0].proposal_id]
    assert set(rung_ids).issubset(called)
    assert with_broker_rungs[0].id not in called
    assert not_pending_rungs[0].id not in called
    states = []
    for group in groups:
        _, rungs = await service.get_proposal(group.proposal_id)
        states.append(rungs[0].state)
    assert states == ["voided_local_stale", "pending_approval", "pending_approval"]
    _, swept_rungs = await service.get_proposal(groups[0].proposal_id)
    assert swept_rungs[0].void_reason == "no_broker_order"
    assert swept_rungs[0].updated_at == now


@pytest.mark.asyncio
async def test_sweep_local_stale_accepts_sync_evidence_callback(db_session):
    service, group = await _create_single_rung(db_session, symbol="SYNC")
    now = datetime(2026, 7, 10, 9, 14, tzinfo=UTC)
    _, initial_rungs = await service.get_proposal(group.proposal_id)
    target_rung_id = initial_rungs[0].id

    swept = await service.sweep_local_stale(
        now=now,
        broker_evidence=lambda rung: (
            "no_broker_order" if rung.id == target_rung_id else "unknown"
        ),
    )

    assert group.proposal_id in swept
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "voided_local_stale"


@pytest.mark.asyncio
async def test_record_approval_dispatch_merges_source_asof(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        source_asof={"resting_deadline": "2026-07-10T15:30:00+09:00"},
    )
    await db_session.commit()
    now = datetime(2026, 7, 10, 9, 15, tzinfo=UTC)

    updated = await service.record_approval_dispatch(
        group.proposal_id, message_id=4242, chat_id="chat-1", now=now
    )

    assert updated.source_asof["resting_deadline"] == "2026-07-10T15:30:00+09:00"
    assert updated.source_asof["approval_message_id"] == 4242
    assert updated.source_asof["approval_chat_id"] == "chat-1"
    assert updated.source_asof["approval_sent_at"] == now.isoformat()

    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.source_asof["resting_deadline"] == "2026-07-10T15:30:00+09:00"
    assert refreshed.source_asof["approval_message_id"] == 4242


@pytest.mark.asyncio
async def test_record_approval_dispatch_missing_proposal_raises(db_session):
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalNotFound):
        await service.record_approval_dispatch(
            uuid.uuid4(),
            message_id=1,
            chat_id="chat-1",
            now=datetime(2026, 7, 10, 9, 15, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# Final-review Finding 1 — account_mode/market submit-routing allowlist.
# `_place_order_impl` (the submit path's default binding) has no
# `account_mode` param at all: it routes purely by `market` and always
# submits `is_mock=False`. A proposal created with an account_mode the submit
# path doesn't actually honor (kis_mock, toss_live, db_simulated) must be
# rejected at create time -- never persisted -- rather than silently routed
# to LIVE KIS on approval.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_allows_kis_live_equity_kr(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    assert group.account_mode == "kis_live"
    assert group.market == "equity_kr"


@pytest.mark.asyncio
async def test_create_proposal_allows_kis_live_equity_us(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    assert group.account_mode == "kis_live"
    assert group.market == "equity_us"


@pytest.mark.asyncio
async def test_create_proposal_allows_upbit_crypto(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="BTC/KRW",
        market="crypto",
        account_mode="upbit",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("0.01"), Decimal("100000000"), None)],
    )
    await db_session.commit()
    assert group.account_mode == "upbit"
    assert group.market == "crypto"


@pytest.mark.asyncio
async def test_create_proposal_rejects_kis_mock_equity_kr(db_session):
    service = OrderProposalsService(db_session)
    with pytest.raises(
        OrderProposalError, match="unsupported account_mode/market/action"
    ):
        await service.create_proposal(
            symbol="A",
            market="equity_kr",
            account_mode="kis_mock",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["equity_kr", "equity_us"])
async def test_create_proposal_allows_toss_live_equities(db_session, market):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="A" if market == "equity_kr" else "AAPL",
        market=market,
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    assert group.account_mode == "toss_live"
    assert group.market == market


@pytest.mark.asyncio
async def test_create_proposal_rejects_db_simulated_and_upbit_wrong_market(db_session):
    service = OrderProposalsService(db_session)
    with pytest.raises(
        OrderProposalError, match="unsupported account_mode/market/action"
    ):
        await service.create_proposal(
            symbol="A",
            market="equity_kr",
            account_mode="db_simulated",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )
    with pytest.raises(
        OrderProposalError, match="unsupported account_mode/market/action"
    ):
        await service.create_proposal(
            symbol="A",
            market="equity_kr",
            account_mode="upbit",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )


@pytest.mark.asyncio
async def test_create_proposal_rejection_leaves_no_partial_rows(db_session):
    """Airtight: the reject must fire before any group/rung row is written --
    even flushed-but-uncommitted -- so a query against this same session sees
    zero matching rows for a rejected create_proposal call.
    """
    from sqlalchemy import func, select

    from app.models.order_proposals import OrderProposal

    service = OrderProposalsService(db_session)
    symbol = f"REJECT-{uuid.uuid4().hex[:8]}"
    with pytest.raises(OrderProposalError):
        await service.create_proposal(
            symbol=symbol,
            market="equity_kr",
            account_mode="kis_mock",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )

    count = await db_session.scalar(
        select(func.count())
        .select_from(OrderProposal)
        .where(OrderProposal.symbol == symbol)
    )
    assert count == 0
