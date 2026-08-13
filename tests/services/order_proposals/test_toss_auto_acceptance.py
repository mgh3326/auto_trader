"""Offline-only acceptance fixtures for TOSS-AUTO-FULL.

These fixtures intentionally use only injected broker snapshots and a test
database.  They are executable evidence for the state-machine boundaries, not
permission to touch a Toss account; the live procedure is owned by the
operator runbook.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.approval_message import parse_callback_data
from app.services.order_proposals.dispatch import (
    dispatch_proposal,
    send_proposal_for_approval,
)
from app.services.order_proposals.revalidation import revalidate_and_submit
from app.services.order_proposals.service import RungInput
from app.services.order_proposals.target_order import TargetOrderSnapshot
from app.services.order_proposals.telegram_callback import handle_callback_update
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session


class _FixtureNotifier:
    """Telegram-shaped recorder; it deliberately has no network transport."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict | None, str, int]] = []
        self.edited: list[tuple[str, int, str, dict | None]] = []
        self._message_id = 7100

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        self._message_id += 1
        self.sent.append((text, inline_keyboard, chat_id, self._message_id))
        return TelegramMethodResult(
            ok=True,
            message_id=self._message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def answer_callback(self, callback_query_id, text=None):
        return True

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))
        return TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )


def _session_factory(db_session):
    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    return factory


def _callback_update(*, data: str, chat_id: str, message_id: int) -> dict:
    return {
        "callback_query": {
            "id": "offline-fixture-callback",
            "from": {"id": 7001},
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
            "data": data,
        }
    }


def _last_card(notifier: _FixtureNotifier) -> tuple[str, int]:
    for _text, keyboard, _chat_id, message_id in reversed(notifier.sent):
        if keyboard is None:
            continue
        callback = keyboard["inline_keyboard"][0][0]["callback_data"]
        if parse_callback_data(callback).action == "op":
            return callback, message_id
    raise AssertionError("offline fixture did not publish an individual approval card")


async def _seed_auto_resting_toss(db_session, *, account_id: str):
    now = datetime.now(UTC)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        broker_account_id=account_id,
        side="buy",
        order_type="limit",
        proposer="offline-acceptance-fixture",
        thesis="offline acceptance partial-fill fixture",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("97000"), None)],
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
        broker_order_id="offline-original-order",
        correlation_id="offline-correlation",
        idempotency_key="offline-idempotency",
        approval_hash_digest="offline-digest",
        now=now,
    )
    await db_session.commit()
    return group


def _target_snapshot(*, remaining: str, now: datetime) -> TargetOrderSnapshot:
    return TargetOrderSnapshot(
        broker_order_id="offline-original-order",
        symbol="005930",
        side="buy",
        order_type="limit",
        limit_price="97000",
        remaining_quantity=remaining,
        status="open",
        observed_at=now.isoformat(),
    )


@pytest.mark.asyncio
async def test_acceptance_b_fill_freeze_cancel_proposal_second_fill_then_terminal(
    monkeypatch, db_session
):
    """B fixture: first fill freezes; delayed approval sees second fill safely.

    Flow proved entirely offline:

    1. broker evidence of a first partial fill freezes the same Toss lane;
    2. a new automatic candidate falls back to a human card;
    3. a cancel proposal is published, then a second partial fill arrives
       before its click, so its stale remaining-quantity snapshot is rejected
       without a cancel mutation;
    4. a refreshed cancel proposal is approved and sees a terminal cancelled
       broker snapshot before its proposal rung closes.
    """
    now = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)
    account_id = f"offline-toss-{uuid.uuid4()}"
    chat_id = f"offline-toss-chat-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )

    original = await _seed_auto_resting_toss(db_session, account_id=account_id)
    service = OrderProposalsService(db_session)

    # First real-world event in the fixture: broker evidence, not a local
    # optimistic write, moves the original rung into partial and freezes auto.
    first_fill = await service.record_fill_evidence(
        broker_order_id="offline-original-order",
        correlation_id="offline-correlation",
        idempotency_key="offline-idempotency",
        filled_qty=Decimal("0.4"),
        terminal_state="partially_filled",
        now=now,
        account_mode="toss_live",
    )
    await db_session.commit()
    assert first_fill is not None
    original_after_first_fill, original_rungs = await service.get_proposal(
        original.proposal_id
    )
    assert original_rungs[0].state == "partially_filled"
    assert (
        original_after_first_fill.source_asof["auto_approved"][
            "toss_auto_submission_freeze"
        ]["state"]
        == "frozen"
    )

    # The account-wide freeze blocks a *new US-market* auto path before it
    # reaches revalidation.  It still produces a normal human card rather
    # than dropping the proposal.
    blocked_candidate = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        broker_account_id=account_id,
        side="buy",
        order_type="limit",
        proposer="offline-acceptance-fixture",
        thesis="would otherwise be auto-submitted",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("150"), None)],
    )
    await db_session.commit()

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("Toss fill freeze must stop auto revalidation")

    freeze_notifier = _FixtureNotifier()
    await dispatch_proposal(
        blocked_candidate.proposal_id,
        notifier=freeze_notifier,
        now=now,
        service_factory=_session_factory(db_session),
        revalidate_fn=must_not_revalidate,
        window_evaluator=allow_known_session,
        now_fn=lambda: now,
    )
    assert "주문 제안 승인" in freeze_notifier.sent[-1][0]

    # The operator takes a fresh target snapshot after the first fill and
    # creates a normal cancel proposal.  This is not auto-eligible: action
    # cancel stays outside auto approval by the existing action gate.
    first_snapshot = _target_snapshot(remaining="0.6", now=now)
    stale_cancel = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        broker_account_id=account_id,
        side="buy",
        order_type="limit",
        proposer="offline-acceptance-fixture",
        thesis="cancel the unfilled remainder after partial fill",
        action="cancel",
        target_broker_order_id="offline-original-order",
        target_order_snapshot=first_snapshot.to_payload(),
        rungs=[RungInput(0, "buy", Decimal("0.6"), Decimal("97000"), None)],
    )
    await db_session.commit()
    stale_notifier = _FixtureNotifier()
    stale_dispatch = await send_proposal_for_approval(
        stale_cancel.proposal_id,
        notifier=stale_notifier,
        now=now,
        service_factory=_session_factory(db_session),
        window_evaluator=allow_known_session,
        now_fn=lambda: now,
    )
    assert stale_dispatch.ok is True
    stale_callback, stale_message_id = _last_card(stale_notifier)
    assert parse_callback_data(stale_callback).action == "op"

    # Required adverse condition: a second fill happens while that first
    # approval card is waiting.  It changes the executable remainder 0.6→0.4.
    second_fill = await service.record_fill_evidence(
        broker_order_id="offline-original-order",
        correlation_id="offline-correlation",
        idempotency_key="offline-idempotency",
        filled_qty=Decimal("0.6"),
        terminal_state="partially_filled",
        now=now,
        account_mode="toss_live",
    )
    await db_session.commit()
    assert second_fill is not None
    _original_after_second_fill, original_rungs = await service.get_proposal(
        original.proposal_id
    )
    assert original_rungs[0].filled_qty == Decimal("0.6")

    stale_cancel_calls: list[dict] = []

    async def stale_fetch(**_kwargs):
        return _target_snapshot(remaining="0.4", now=now)

    async def must_not_cancel(**kwargs):
        stale_cancel_calls.append(kwargs)
        raise AssertionError("stale snapshot must reject before cancel mutation")

    async def stale_revalidate(**kwargs):
        return await revalidate_and_submit(
            **kwargs,
            fetch_target_fn=stale_fetch,
            cancel_target_fn=must_not_cancel,
            window_evaluator=allow_known_session,
            now_fn=lambda: now,
        )

    stale_result = await handle_callback_update(
        _callback_update(
            data=stale_callback,
            chat_id=chat_id,
            message_id=stale_message_id,
        ),
        now=now,
        service_factory=_session_factory(db_session),
        notifier=stale_notifier,
        revalidate_fn=stale_revalidate,
        window_evaluator=allow_known_session,
        now_fn=lambda: now,
    )
    assert stale_cancel_calls == []
    assert stale_result["results"] == ["error"]
    _stale_group, stale_rungs = await service.get_proposal(stale_cancel.proposal_id)
    assert stale_rungs[0].state == "rejected"
    assert stale_rungs[0].void_reason == "target_snapshot_mismatch:remaining_quantity"

    # A refreshed proposal now carries the second-fill remainder.  Its click
    # is allowed to call the injected cancellation edge, but only marks its
    # rung cancelled after a fresh terminal broker snapshot says cancelled.
    fresh_snapshot = _target_snapshot(remaining="0.4", now=now)
    fresh_cancel = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        broker_account_id=account_id,
        side="buy",
        order_type="limit",
        proposer="offline-acceptance-fixture",
        thesis="cancel refreshed unfilled remainder",
        action="cancel",
        target_broker_order_id="offline-original-order",
        target_order_snapshot=fresh_snapshot.to_payload(),
        rungs=[RungInput(0, "buy", Decimal("0.4"), Decimal("97000"), None)],
    )
    await db_session.commit()
    fresh_notifier = _FixtureNotifier()
    fresh_dispatch = await send_proposal_for_approval(
        fresh_cancel.proposal_id,
        notifier=fresh_notifier,
        now=now,
        service_factory=_session_factory(db_session),
        window_evaluator=allow_known_session,
        now_fn=lambda: now,
    )
    assert fresh_dispatch.ok is True
    fresh_callback, fresh_message_id = _last_card(fresh_notifier)

    fetch_count = 0
    terminal_cancel_calls: list[dict] = []

    async def terminal_fetch(**_kwargs):
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            return fresh_snapshot
        return TargetOrderSnapshot(
            broker_order_id="offline-original-order",
            symbol="005930",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="0",
            status="cancelled",
            observed_at=now.isoformat(),
        )

    async def terminal_cancel(**kwargs):
        terminal_cancel_calls.append(kwargs)
        return {"success": True, "replacement_order_id": "offline-cancel-request"}

    async def fresh_revalidate(**kwargs):
        return await revalidate_and_submit(
            **kwargs,
            fetch_target_fn=terminal_fetch,
            cancel_target_fn=terminal_cancel,
            window_evaluator=allow_known_session,
            now_fn=lambda: now,
        )

    final_result = await handle_callback_update(
        _callback_update(
            data=fresh_callback,
            chat_id=chat_id,
            message_id=fresh_message_id,
        ),
        now=now,
        service_factory=_session_factory(db_session),
        notifier=fresh_notifier,
        revalidate_fn=fresh_revalidate,
        window_evaluator=allow_known_session,
        now_fn=lambda: now,
    )
    assert final_result["results"] == ["cancelled"]
    assert len(terminal_cancel_calls) == 1
    assert fetch_count == 2  # fresh target validation, then broker terminal proof
    _fresh_group, fresh_rungs = await service.get_proposal(fresh_cancel.proposal_id)
    assert fresh_rungs[0].state == "cancelled"
