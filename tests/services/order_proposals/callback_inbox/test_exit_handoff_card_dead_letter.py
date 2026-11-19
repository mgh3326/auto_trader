"""hk 5441 O3 — an exit handoff card after a W5 pre-core dead letter.

Design ``docs/design/5438-go-execution-path.md`` O1, O3. The durable inbox dead
letters a callback whose notifier could not be resolved three times. That
happens before the approval core is entered, so nothing on the proposal
changes: the card is still published and its nonce is still unconsumed.

The handoff must therefore stay ``card_claimed`` -- releasing it would let a new
card be approved beside the old one -- and the position may be sold once.

Real here: the inbox ingress, the worker's attempt budget and dead letter
(``process_callback_job``), the proposal service, the handoff claim, its settle
step, and the committed send fence of the card's send boundary
(``exit_handoff_place_fn``). The transport behind that boundary is a counter:
it stands for the broker and counts the sells that would have gone out.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal, engine
from app.core.timezone import now_kst
from app.models.execution_mandates import ExecutionExitHandoff
from app.models.order_proposals import OrderProposal
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.exit_handoff_card import exit_handoff_place_fn
from app.services.strategy_sleeves import account_binding, exit_handoffs
from app.services.strategy_sleeves.exit_handoffs import (
    SettleOutcome,
    settle_exit_handoff,
)
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.execution_intent_gateway.test_upbit_db import (
    ACCOUNT_REF,
    SYMBOL,
    _card,
    _handoff,
    _mandate,
    _set_state,
    _sleeve,
)
from tests.services.order_proposals.window_fakes import allow_known_session

from .conftest import (
    CHAT_ID,
    _publish_fixture_card,
    load_job,
    make_update,
    proposal_callback_data,
    shape_owned_callback_inbox_row,
)

validate_run_owned_database_url(engine.url)

pytestmark = pytest.mark.integration


@pytest.fixture
def proven(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sleeve's proven position and the configured account, as fixed facts."""

    async def holdings(_session: Any, _sleeve: Any, _symbol: str) -> Decimal:
        return Decimal("100")

    monkeypatch.setattr(exit_handoffs, "proven_holdings", holdings)
    monkeypatch.setattr(
        account_binding, "configured_upbit_account_ref", lambda: ACCOUNT_REF
    )


async def _publish(card: OrderProposal, nonce: str) -> OrderProposal:
    """Publish the card exactly as ``seed_proposal`` publishes its fixture."""
    async with AsyncSessionLocal() as session:
        group = await session.scalar(
            select(OrderProposal).where(OrderProposal.proposal_id == card.proposal_id)
        )
        assert group is not None
        service = OrderProposalsService(session)
        dispatched_at = datetime.now(UTC)
        window = await allow_known_session(group, now=dispatched_at)
        await service.record_approval_dispatch(
            group.proposal_id,
            message_id=555,
            chat_id=str(CHAT_ID),
            now=dispatched_at,
            approval_window_policy_stamp=window.policy_stamp,
        )
        await service.set_approval_nonce(group.proposal_id, nonce)
        await _publish_fixture_card(service, group, nonce=nonce)
        await session.commit()
        await session.refresh(group)
        return group


async def _proposal(proposal_id: uuid.UUID) -> OrderProposal:
    async with AsyncSessionLocal() as session:
        row = await session.scalar(
            select(OrderProposal).where(OrderProposal.proposal_id == proposal_id)
        )
        assert row is not None
        return row


async def _handoff_row(handoff_id: int) -> ExecutionExitHandoff:
    async with AsyncSessionLocal() as session:
        row = await session.get(ExecutionExitHandoff, handoff_id)
        assert row is not None
        return row


async def _dead_letter_before_the_core(
    inbox_cleanup: list[uuid.UUID],
    data: str,
    monkeypatch: pytest.MonkeyPatch,
) -> uuid.UUID:
    """Three notifier-resolution failures: the W5 pre-core dead letter."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.contracts import MAX_ATTEMPTS
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    assert MAX_ATTEMPTS == 3
    update_id = 660_000 + uuid.uuid4().int % 100_000

    async def no_kick(_job_id: uuid.UUID) -> None:
        return None

    queued = await ingest_callback_update(
        make_update(data=data, update_id=update_id, callback_id=f"cbq-{update_id}"),
        now=now_kst(),
        enqueue_fn=no_kick,
    )
    assert queued.job_id is not None
    inbox_cleanup.append(queued.job_id)
    core_entries: list[int] = []

    async def core(_normalized: Any, **_kwargs: Any) -> dict[str, Any]:
        core_entries.append(1)
        return {"handled": True, "reason": "approved"}

    def notifier_unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("notifier unavailable")

    monkeypatch.setattr(worker_module, "resolve_notifier", notifier_unavailable)
    for attempt in (1, 2):
        result = await process_callback_job(queued.job_id, handler=core)
        assert result["status"] == "retry_scheduled", (attempt, result)
        async with AsyncSessionLocal() as session:
            await shape_owned_callback_inbox_row(
                session, queued.job_id, available_at=now_kst() - timedelta(seconds=1)
            )
            await session.commit()
    final = await process_callback_job(queued.job_id, handler=core)

    assert final["status"] == "dead_letter", final
    assert core_entries == [], "a pre-core failure reached the approval core"
    row = await load_job(queued.job_id)
    assert row is not None and row.state == "dead_letter"
    assert row.error_class == "attempts_exhausted" and row.attempt_count == 3
    return queued.job_id


@pytest.mark.asyncio
async def test_o3_after_a_w5_pre_core_dead_letter_the_position_sells_at_most_once(
    proven: None,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    card = await _publish(await _card(sleeve, handoff_id), nonce="nonce-hk5441-o3")
    before = await _proposal(card.proposal_id)

    await _dead_letter_before_the_core(
        inbox_cleanup, proposal_callback_data(card), monkeypatch
    )

    # Nothing on the proposal changed: the original card is still approvable.
    after = await _proposal(card.proposal_id)
    assert after.lifecycle_state == before.lifecycle_state
    assert after.approval_nonce == "nonce-hk5441-o3"
    assert after.approval_nonce_used_at is None
    # So the handoff is still that card's, with no send attempt counted.
    handoff = await _handoff_row(handoff_id)
    assert handoff.state == "card_claimed"
    assert handoff.proposal_id == card.proposal_id and handoff.send_attempts == 0
    async with AsyncSessionLocal() as session:
        settled = await settle_exit_handoff(
            session, handoff_id, expected_claim_count=1, reading=None, now=now_kst()
        )
        await session.commit()
    assert settled.outcome is SettleOutcome.HELD and settled.state == "card_claimed"
    # No new card can be created beside it.
    with pytest.raises(OrderProposalError, match="exit_handoff_not_ready"):
        await _card(sleeve, handoff_id)

    sells: list[str] = []

    async def broker(**kwargs: Any) -> dict[str, Any]:
        # The order layer calls the pre-send hook, then makes the request.
        await kwargs["pre_send_hook"]()
        sells.append(str(kwargs["symbol"]))
        return {"success": True, "order_id": "uuid-card"}

    def approve(group: Any) -> Any:
        return exit_handoff_place_fn(
            group,
            broker,
            session_factory=AsyncSessionLocal,
            account_ref=lambda: ACCOUNT_REF,
        )(dry_run=False, symbol=SYMBOL)

    # The original card is approved by a later click and its sell goes out.
    first = await approve(card)
    assert first == {"success": True, "order_id": "uuid-card"}
    # Its approval then fails after the request and rolls back: the card is
    # clickable again. A second click must not post.
    second = await approve(card)
    assert second["pre_send_blocked"] is True
    assert second["reason_codes"] == ["exit_handoff_prior_attempt_unresolved"]
    # A card that does not hold the claim -- what a "new card" would be if one
    # could exist -- must not post either.
    other = await approve(
        SimpleNamespace(exit_handoff_id=handoff_id, proposal_id=uuid.uuid4())
    )
    assert other["reason_codes"] == ["exit_handoff_claim_lost"]

    assert sells == [SYMBOL], "the position was sold more than once"
    handoff = await _handoff_row(handoff_id)
    assert handoff.state == "card_claimed" and handoff.send_attempts == 1
    assert handoff.send_attempts_proven_unsent == 0
    assert handoff.fills_unmeasured is True
