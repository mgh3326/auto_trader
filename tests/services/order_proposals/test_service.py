import uuid
from decimal import Decimal

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import (
    OrderProposalInvalidStateTransition,
    OrderProposalNotFound,
)
from app.services.order_proposals.service import RungInput


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
