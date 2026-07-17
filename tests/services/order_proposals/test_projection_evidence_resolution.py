from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.service import OrderProposalError, RungInput

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture(
    params=[("toss_live", "equity_us", "AAPL"), ("kis_live", "equity_kr", "005930")]
)
def broker_scope(request):
    return request.param


async def _resting_rung(
    db_session,
    *,
    account_mode: str,
    market: str,
    symbol: str,
    broker_order_id: str | None,
    correlation_id: str | None,
    idempotency_key: str | None = None,
):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market=market,
        account_mode=account_mode,
        side="sell",
        order_type="limit",
        proposer="projection-evidence-test",
        rungs=[RungInput(0, "sell", Decimal("2"), Decimal("100"), None)],
    )
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(group.proposal_id, 0, new_state=state)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=broker_order_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        approval_hash_digest=f"digest-{uuid4().hex}",
        now=datetime.now(UTC),
    )
    _, rungs = await service.get_proposal(group.proposal_id)
    return group.proposal_id, rungs[0].id


async def _rung(db_session, proposal_id):
    _, rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    await db_session.refresh(rungs[0])
    return rungs[0]


async def test_intersection_keeps_terminal_sibling_untouched_and_transitions_owner(
    db_session, broker_scope
):
    """broker={B}, corr={A,B}: R1 selects B even when A is terminal."""
    account_mode, market, symbol = broker_scope
    unique = uuid4().hex
    broker_b = f"broker-b-{unique}"
    broker_a = f"broker-a-{unique}"
    correlation = f"corr-siblings-{unique}"
    b_proposal, b_id = await _resting_rung(
        db_session,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
        broker_order_id=broker_b,
        correlation_id=correlation,
    )
    a_proposal, _ = await _resting_rung(
        db_session,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
        broker_order_id=broker_a,
        correlation_id=correlation,
    )
    service = OrderProposalsService(db_session)
    await service.transition_rung(a_proposal, 0, new_state="filled")

    owner = await service.find_unambiguous_evidence_rung_id(
        broker_order_id=broker_b,
        correlation_id=correlation,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
    )
    assert owner == b_id
    transitioned = await service.record_fill_evidence_for_rung(
        rung_id=owner,
        broker_order_id=broker_b,
        correlation_id=correlation,
        filled_qty=Decimal("2"),
        terminal_state="filled",
        now=datetime.now(UTC),
        account_mode=account_mode,
        market=market,
        symbol=symbol,
    )
    assert transitioned is not None and transitioned.id == b_id
    assert (await _rung(db_session, b_proposal)).state == "filled"
    assert (await _rung(db_session, a_proposal)).state == "filled"


async def test_correlation_only_content_hash_siblings_are_classified_separately(
    db_session, broker_scope
):
    account_mode, market, symbol = broker_scope
    correlation = f"corr-content-{uuid4().hex}"
    await _resting_rung(
        db_session,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
        broker_order_id=f"broker-1-{uuid4().hex}",
        correlation_id=correlation,
    )
    await _resting_rung(
        db_session,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
        broker_order_id=f"broker-2-{uuid4().hex}",
        correlation_id=correlation,
    )
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError, match="content_hash_only_ambiguous"):
        await service.find_unambiguous_evidence_rung_id(
            broker_order_id=None,
            correlation_id=correlation,
            account_mode=account_mode,
            market=market,
            symbol=symbol,
        )


async def test_duplicate_broker_id_is_classified_separately(db_session, broker_scope):
    account_mode, market, symbol = broker_scope
    broker_order_id = f"broker-duplicate-{uuid4().hex}"
    await _resting_rung(
        db_session,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
        broker_order_id=broker_order_id,
        correlation_id=f"corr-1-{uuid4().hex}",
    )
    await _resting_rung(
        db_session,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
        broker_order_id=broker_order_id,
        correlation_id=f"corr-2-{uuid4().hex}",
    )
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError, match="broker_id_duplicate"):
        await service.find_unambiguous_evidence_rung_id(
            broker_order_id=broker_order_id,
            correlation_id=None,
            account_mode=account_mode,
            market=market,
            symbol=symbol,
        )


async def test_superseded_shared_client_id_uses_intersection_not_key_priority(
    db_session, broker_scope
):
    """A content-derived client id may be shared; broker evidence chooses B."""
    account_mode, market, symbol = broker_scope
    unique = uuid4().hex
    correlation = f"corr-supersede-{unique}"
    idempotency_key = f"tossp6-{unique[:16]}"
    broker_b = f"broker-b-{unique}"
    b_proposal, b_id = await _resting_rung(
        db_session,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
        broker_order_id=broker_b,
        correlation_id=correlation,
        idempotency_key=idempotency_key,
    )
    a_proposal, _ = await _resting_rung(
        db_session,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
        broker_order_id=f"broker-a-{unique}",
        correlation_id=correlation,
        idempotency_key=idempotency_key,
    )
    service = OrderProposalsService(db_session)
    owner = await service.find_unambiguous_evidence_rung_id(
        broker_order_id=broker_b,
        correlation_id=correlation,
        idempotency_key=idempotency_key,
        account_mode=account_mode,
        market=market,
        symbol=symbol,
    )
    assert owner == b_id
    await service.record_fill_evidence_for_rung(
        rung_id=owner,
        broker_order_id=broker_b,
        correlation_id=correlation,
        idempotency_key=idempotency_key,
        filled_qty=Decimal("2"),
        terminal_state="filled",
        now=datetime.now(UTC),
        account_mode=account_mode,
        market=market,
        symbol=symbol,
    )
    assert (await _rung(db_session, b_proposal)).state == "filled"
    assert (await _rung(db_session, a_proposal)).state == "resting"
