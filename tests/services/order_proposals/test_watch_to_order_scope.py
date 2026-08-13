"""Contract tests for the public watcher proposal-scope seam."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.db import AsyncSessionLocal
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.service import RungInput, WatchToOrderScope


def _scope(*, suffix: str | None = None) -> WatchToOrderScope:
    unique = suffix or uuid.uuid4().hex
    return WatchToOrderScope(
        symbol=f"SEAM-{unique}",
        market="equity_us",
        account_mode="kis_live",
        broker_account_id=f"account-{unique}",
        action="place",
    )


def _create_kwargs(scope: WatchToOrderScope, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "symbol": scope.symbol,
        "market": scope.market,
        "account_mode": scope.account_mode,
        "broker_account_id": scope.broker_account_id,
        "side": "buy",
        "order_type": "limit",
        "proposer": "watch-scope-test",
        "strategy": "watch_scope_test",
        "action": scope.action,
        "rungs": [RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_inspect_watch_to_order_scope_uses_every_scope_axis(db_session):
    scope = _scope()
    service = OrderProposalsService(db_session)
    matching = await service.create_proposal(**_create_kwargs(scope))
    await service.create_proposal(
        **_create_kwargs(scope, symbol=f"other-symbol-{uuid.uuid4().hex}")
    )
    await service.create_proposal(
        **_create_kwargs(scope, broker_account_id=f"other-{uuid.uuid4().hex}")
    )
    await service.create_proposal(**_create_kwargs(scope, broker_account_id=None))
    await service.create_proposal(**_create_kwargs(scope, account_mode="toss_live"))
    await service.create_proposal(**_create_kwargs(scope, market="equity_kr"))

    inspection = await service.inspect_watch_to_order_scope(
        symbol=scope.symbol,
        market=scope.market,
        account_mode=scope.account_mode,
        broker_account_id=scope.broker_account_id,
    )

    assert inspection.lock_acquired is True
    assert inspection.lock_failure_code is None
    assert inspection.scope == scope
    assert inspection.origin_marker == "order_proposals"
    assert [group.proposal_id for group in inspection.active_groups] == [
        matching.proposal_id
    ]
    snapshot = inspection.active_groups[0]
    assert snapshot.origin_marker == "order_proposals"
    assert snapshot.proposer == "watch-scope-test"
    assert snapshot.strategy == "watch_scope_test"
    assert [(rung.rung_index, rung.state) for rung in snapshot.rungs] == [
        (0, "pending_approval")
    ]
    assert snapshot.all_rung_states == ("pending_approval",)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_inspect_reports_mixed_state_group_by_rung_not_rollup(db_session):
    scope = _scope()
    service = OrderProposalsService(db_session)
    original = await service.create_proposal(
        **_create_kwargs(
            scope,
            rungs=[
                RungInput(0, "buy", Decimal("1"), Decimal("100"), None),
                RungInput(1, "buy", Decimal("1"), Decimal("99"), None),
            ],
        )
    )
    for state in ("revalidating", "approved", "submitting", "resting"):
        await service.transition_rung(original.proposal_id, 0, new_state=state)
    await service.transition_rung(original.proposal_id, 1, new_state="revalidating")
    await service.mark_needs_reconfirm(
        original.proposal_id,
        1,
        now=datetime(2026, 8, 13, 6, 0, tzinfo=UTC),
    )

    replacement = await service.create_proposal(
        **_create_kwargs(
            scope,
            supersedes_proposal_id=original.proposal_id,
        )
    )
    inspection = await service.inspect_watch_to_order_scope(
        symbol=scope.symbol,
        market=scope.market,
        account_mode=scope.account_mode,
        broker_account_id=scope.broker_account_id,
    )

    assert inspection.lock_acquired is True
    assert {group.proposal_id for group in inspection.active_groups} == {
        original.proposal_id,
        replacement.proposal_id,
    }
    original_snapshot = next(
        group
        for group in inspection.active_groups
        if group.proposal_id == original.proposal_id
    )
    assert original_snapshot.lifecycle_state == "superseded"
    assert [(rung.rung_index, rung.state) for rung in original_snapshot.rungs] == [
        (0, "resting"),
        (1, "superseded"),
    ]
    await db_session.rollback()


@pytest.mark.asyncio
async def test_scope_companion_rejects_active_rungs_without_creating(db_session):
    scope = _scope()
    service = OrderProposalsService(db_session)
    existing = await service.create_proposal(**_create_kwargs(scope))
    inspection = await service.inspect_watch_to_order_scope(
        symbol=scope.symbol,
        market=scope.market,
        account_mode=scope.account_mode,
        broker_account_id=scope.broker_account_id,
    )

    with pytest.raises(
        OrderProposalError, match="watch_to_order_scope_active_rungs_present"
    ):
        await service.create_proposal_in_watch_to_order_scope(
            inspection, **_create_kwargs(scope)
        )

    repeated = await service.inspect_watch_to_order_scope(
        symbol=scope.symbol,
        market=scope.market,
        account_mode=scope.account_mode,
        broker_account_id=scope.broker_account_id,
    )
    assert [group.proposal_id for group in repeated.active_groups] == [
        existing.proposal_id
    ]
    await db_session.rollback()


@pytest.mark.asyncio
async def test_scope_try_lock_is_nonblocking_and_loser_creates_nothing(
    db_session,
):
    scope = _scope()
    async with (
        AsyncSessionLocal() as winner_session,
        AsyncSessionLocal() as loser_session,
    ):
        winner = OrderProposalsService(winner_session)
        loser = OrderProposalsService(loser_session)
        winner_inspection = await winner.inspect_watch_to_order_scope(
            symbol=scope.symbol,
            market=scope.market,
            account_mode=scope.account_mode,
            broker_account_id=scope.broker_account_id,
        )
        assert winner_inspection.lock_acquired is True
        assert winner_inspection.active_groups == ()

        loser_inspection = await asyncio.wait_for(
            loser.inspect_watch_to_order_scope(
                symbol=scope.symbol,
                market=scope.market,
                account_mode=scope.account_mode,
                broker_account_id=scope.broker_account_id,
                # The lock still collides across actions; action is not a scope
                # axis because existing active orders must not be bypassed.
                action="replace",
            ),
            timeout=1,
        )
        assert loser_inspection.lock_acquired is False
        assert loser_inspection.active_groups == ()
        assert (
            loser_inspection.lock_failure_code
            == "watch_to_order_scope_lock_unavailable"
        )
        with pytest.raises(
            OrderProposalError, match="watch_to_order_scope_reservation_unavailable"
        ):
            await loser.create_proposal_in_watch_to_order_scope(
                loser_inspection, **_create_kwargs(scope, action="replace")
            )

        created = await winner.create_proposal_in_watch_to_order_scope(
            winner_inspection, **_create_kwargs(scope)
        )
        await winner_session.commit()
        await loser_session.rollback()

    async with AsyncSessionLocal() as verifier_session:
        verifier = OrderProposalsService(verifier_session)
        inspection = await verifier.inspect_watch_to_order_scope(
            symbol=scope.symbol,
            market=scope.market,
            account_mode=scope.account_mode,
            broker_account_id=scope.broker_account_id,
        )
        assert inspection.lock_acquired is True
        assert [group.proposal_id for group in inspection.active_groups] == [
            created.proposal_id
        ]
        await verifier_session.rollback()
