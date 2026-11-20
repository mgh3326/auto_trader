"""hk 5441 O1, O3 — an exit handoff card through the real approval callback.

Design ``docs/design/5438-go-execution-path.md`` O1, O3. The durable inbox dead
letters a callback whose notifier could not be resolved three times. That
happens before the approval core is entered, so nothing on the proposal
changes: the card is still published and its nonce is still unconsumed.

The handoff must therefore stay ``card_claimed`` -- releasing it would let a new
card be approved beside the old one -- and the position may be sold once.

REAL here
    the inbox ingress, the worker's attempt budget, dead letter and entry
    markers (``process_callback_job``), the approval core it calls by default
    (``handle_normalized_callback``: published-binding preflight, approval
    window, nonce consumption, commit lease, ``record_approval``), the default
    ``revalidate_and_submit`` with its rung transitions and submit
    classification, the card's send boundary (``exit_handoff_place_fn``: the
    committed fence, the pre-POST check, the proven-unsent record), the
    proposal service, the handoff claim and its settle step.

FAKED -- what lies below the card's send boundary, and Telegram
    * ``order_execution._place_order_impl``, the order layer. The stand-in
      previews without a request and, on a live call, awaits the pre-send hook
      last and then counts the sell, the way the real one calls the hook
      immediately before its request. ``sells`` is what would have reached the
      broker.
    * ``fetch_order_by_identifier``, the broker lookup the submit classifier
      makes for an Upbit result it cannot place. Here the broker is unreachable
      and every lookup is counted.
    * the Telegram notifier.
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
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.brokers.kis.pre_send import PreSendFreshnessError
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
    _account_control_row,  # noqa: F401 - autouse: the kill row and the journal lock
    _card,
    _handoff,
    _mandate,
    _reserved_live_buy,
    _set_state,
    _sleeve,
)
from tests.services.order_proposals.window_fakes import allow_known_session

from .conftest import (
    CHAT_ID,
    FakeNotifier,
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


async def _card_state(
    proposal_id: uuid.UUID,
) -> tuple[OrderProposal, OrderProposalRung]:
    """The card and its one rung, read through a fresh session."""
    async with AsyncSessionLocal() as session:
        group = await session.scalar(
            select(OrderProposal).where(OrderProposal.proposal_id == proposal_id)
        )
        assert group is not None
        rungs = list(
            (
                await session.scalars(
                    select(OrderProposalRung).where(
                        OrderProposalRung.proposal_pk == group.id
                    )
                )
            ).all()
        )
        assert len(rungs) == 1
        session.expunge_all()
        return group, rungs[0]


class _OrderLayer:
    """Stands in for ``order_execution._place_order_impl`` (see the module doc)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.mcp_server.tooling import order_execution

        self.sells: list[str] = []
        self.order_ids: list[str] = []
        monkeypatch.setattr(order_execution, "_place_order_impl", self)

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("dry_run") is not False:
            return {
                "success": True,
                "dry_run": True,
                "price": kwargs.get("price"),
                "quantity": kwargs.get("quantity"),
                "approval_hash": "card-preview",
            }
        try:
            # The order layer calls the pre-send hook, then makes the request.
            await kwargs["pre_send_hook"]()
        except PreSendFreshnessError as exc:
            return {
                "success": False,
                "pre_send_blocked": True,
                "reason_codes": list(exc.reason_codes),
                "error": exc.reason_codes[0],
                "dry_run": False,
            }
        self.sells.append(str(kwargs["symbol"]))
        self.order_ids.append(f"uuid-card-{uuid.uuid4().hex}")
        return {
            "success": True,
            "dry_run": False,
            "broker_status": "accepted",
            "order_id": self.order_ids[-1],
        }


class _UnreachableBroker:
    """The identifier lookup of the submit classifier, with the broker down."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services.brokers.upbit import orders as upbit_orders

        self.calls = 0

        async def fetch_order_by_identifier(_identifier: str) -> dict[str, Any]:
            self.calls += 1
            raise RuntimeError("broker unreachable")

        monkeypatch.setattr(
            upbit_orders, "fetch_order_by_identifier", fetch_order_by_identifier
        )


async def _deliver(
    inbox_cleanup: list[uuid.UUID], data: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """A new delivery of the card's button, run by the real worker and core.

    No ``handler=`` and no ``revalidate_fn=``: the worker resolves the real
    approval core, and the core its default ``revalidate_and_submit``.
    """
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    update_id = 680_000 + uuid.uuid4().int % 100_000

    async def no_kick(_job_id: uuid.UUID) -> None:
        return None

    queued = await ingest_callback_update(
        make_update(
            data=data, update_id=update_id, callback_id=f"cbq-{uuid.uuid4().hex}"
        ),
        now=now_kst(),
        enqueue_fn=no_kick,
    )
    assert queued.job_id is not None
    inbox_cleanup.append(queued.job_id)
    notifier = FakeNotifier()
    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: notifier)
    return await process_callback_job(queued.job_id)


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
    card = await _publish(await _card(sleeve, handoff_id), nonce="nonce-hk5441")
    before = await _proposal(card.proposal_id)
    data = proposal_callback_data(card)

    await _dead_letter_before_the_core(inbox_cleanup, data, monkeypatch)

    # Nothing on the proposal changed: the original card is still approvable.
    after = await _proposal(card.proposal_id)
    assert after.lifecycle_state == before.lifecycle_state
    assert after.approval_nonce == "nonce-hk5441"
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

    order_layer = _OrderLayer(monkeypatch)
    lookups = _UnreachableBroker(monkeypatch)

    # A later click on the original card: a new delivery, run by the real
    # worker, the real approval core and the real revalidation.
    first = await _deliver(inbox_cleanup, data, monkeypatch)

    assert first["status"] == "succeeded", first
    assert order_layer.sells == [SYMBOL]
    group, rung = await _card_state(card.proposal_id)
    assert rung.state == "resting"
    assert rung.broker_order_id == order_layer.order_ids[0]
    assert group.approval_nonce_used_at is not None
    handoff = await _handoff_row(handoff_id)
    assert handoff.state == "card_claimed" and handoff.proposal_id == card.proposal_id
    assert handoff.send_attempts == 1 and handoff.send_attempts_proven_unsent == 0
    assert handoff.fills_unmeasured is True

    # The same button again: the nonce is spent, the core is not entered far
    # enough to send, and nothing else is sold.
    second = await _deliver(inbox_cleanup, data, monkeypatch)
    assert second["status"] != "succeeded", second
    assert order_layer.sells == [SYMBOL], "the position was sold more than once"
    # A card that does not hold the claim -- what a "new card" would be if one
    # could exist -- must not post either, and none can be created.
    other = await exit_handoff_place_fn(
        SimpleNamespace(exit_handoff_id=handoff_id, proposal_id=uuid.uuid4()),
        order_layer,
        session_factory=AsyncSessionLocal,
        account_ref=lambda: ACCOUNT_REF,
    )(dry_run=False, symbol=SYMBOL)
    assert other["reason_codes"] == ["exit_handoff_claim_lost"]
    with pytest.raises(OrderProposalError, match="exit_handoff_not_ready"):
        await _card(sleeve, handoff_id)

    assert order_layer.sells == [SYMBOL], "the position was sold more than once"
    assert lookups.calls == 0
    handoff = await _handoff_row(handoff_id)
    assert handoff.state == "card_claimed" and handoff.send_attempts == 1


@pytest.mark.asyncio
async def test_o1_an_approval_that_rolled_back_after_the_request_cannot_send_again(
    proven: None,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approval core reached the request and then failed: its transaction
    rolled back, so the card reads ``pending_approval`` with a live nonce again.
    The send attempt it committed outside that transaction is what stops the
    next click, through the same real callback."""
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    card = await _publish(await _card(sleeve, handoff_id), nonce="nonce-hk5441")
    data = proposal_callback_data(card)
    order_layer = _OrderLayer(monkeypatch)
    lookups = _UnreachableBroker(monkeypatch)

    record_resting = OrderProposalsService.record_resting

    async def fails_after_the_request(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the outcome could not be written")

    monkeypatch.setattr(
        OrderProposalsService, "record_resting", fails_after_the_request
    )
    first = await _deliver(inbox_cleanup, data, monkeypatch)
    monkeypatch.setattr(OrderProposalsService, "record_resting", record_resting)

    # The core was entered, so the job is a dead letter: never retried.
    assert first["status"] == "dead_letter", first
    assert order_layer.sells == [SYMBOL]
    group, rung = await _card_state(card.proposal_id)
    assert rung.state == "pending_approval" and rung.broker_order_id is None
    assert group.approval_nonce == "nonce-hk5441"
    assert group.approval_nonce_used_at is None, "the rollback did not restore the card"
    handoff = await _handoff_row(handoff_id)
    assert handoff.send_attempts == 1 and handoff.send_attempts_proven_unsent == 0
    assert handoff.fills_unmeasured is True

    # The restored card is clicked again.
    await _deliver(inbox_cleanup, data, monkeypatch)

    assert order_layer.sells == [SYMBOL], "the restored card posted a second time"
    # This refusal is no proof of anything, so the classifier looked, as before.
    assert lookups.calls == 1
    _group, rung = await _card_state(card.proposal_id)
    assert rung.state == "unverified"
    handoff = await _handoff_row(handoff_id)
    assert handoff.state == "card_claimed"
    assert handoff.send_attempts == 1 and handoff.send_attempts_proven_unsent == 0
    async with AsyncSessionLocal() as session:
        settled = await settle_exit_handoff(
            session, handoff_id, expected_claim_count=1, reading=None, now=now_kst()
        )
        await session.commit()
    assert settled.outcome is SettleOutcome.HELD_UNMEASURED
    assert settled.state == "card_claimed"


@pytest.mark.asyncio
async def test_o1_a_card_stopped_by_a_buy_in_flight_ends_unsent_and_returns_the_handoff(
    proven: None,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 2486 review (65wJD0, 65t2wx): a live buy of the sleeve is reserved and
    unanswered, so the card's fence refuses before it counts anything, and the
    broker cannot be reached for the identifier lookup. The approval ends the
    card on the fence's proof instead of looking: no ``unverified`` rung, no
    idempotency key, and the handoff returns at the next settle."""
    sleeve = await _sleeve("live")
    mandate = await _mandate(sleeve, "live")
    await _reserved_live_buy(mandate)
    handoff_id = await _handoff(sleeve, mandate)
    await _set_state(handoff_id, "ready_for_card")
    card = await _publish(await _card(sleeve, handoff_id), nonce="nonce-hk5441")
    data = proposal_callback_data(card)
    order_layer = _OrderLayer(monkeypatch)
    lookups = _UnreachableBroker(monkeypatch)

    result = await _deliver(inbox_cleanup, data, monkeypatch)

    assert result["status"] == "succeeded", result
    assert order_layer.sells == []
    assert lookups.calls == 0, "a card that provably never posted was looked up"
    group, rung = await _card_state(card.proposal_id)
    assert rung.state == "rejected"
    assert rung.void_reason == "exit_handoff_buy_in_flight"
    assert rung.idempotency_key is None and rung.broker_order_id is None
    assert group.lifecycle_state == "rejected"
    assert group.approval_nonce_used_at is not None
    handoff = await _handoff_row(handoff_id)
    assert handoff.state == "card_claimed" and handoff.send_attempts == 0
    assert handoff.fills_unmeasured is False

    async with AsyncSessionLocal() as session:
        settled = await settle_exit_handoff(
            session, handoff_id, expected_claim_count=1, reading=None, now=now_kst()
        )
        await session.commit()
    assert settled.outcome is SettleOutcome.RETURNED_UNSENT
    assert settled.state == "ready_for_card"
    assert (await _handoff_row(handoff_id)).proposal_id is None

    # The ended card's button no longer approves anything.
    again = await _deliver(inbox_cleanup, data, monkeypatch)
    assert again["status"] != "succeeded", again
    assert order_layer.sells == []
    # The position is not stranded: a new card can take the handoff.
    replacement = await _card(sleeve, handoff_id)
    handoff = await _handoff_row(handoff_id)
    assert handoff.state == "card_claimed"
    assert handoff.proposal_id == replacement.proposal_id and handoff.claim_count == 2
