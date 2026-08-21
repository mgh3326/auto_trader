"""Shared fixtures for the durable Telegram callback inbox (W5).

Every fixture here talks to the *run-owned* test database through the real
``AsyncSessionLocal``/``engine`` — the inbox's whole point is that the
PostgreSQL row and the PostgreSQL advisory lock are the authority, so a
session-level double or an in-memory fake would prove nothing about the
contract under test.

No Telegram, broker, provider or Redis client is ever constructed: the
enqueue side effect is injected, the notifier is a fake, and
``revalidate_and_submit`` is replaced everywhere a proposal is exercised.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import approval_message as approval_messages
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals import telegram_callback as callback_module
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalPublication,
    DispatchBinding,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.service import RungInput
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session

CHAT_ID = 42
USER_ID = 777


@pytest.fixture(autouse=True)
def _known_market_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the approval window deterministic, exactly as the sibling suite does."""
    monkeypatch.setattr(
        callback_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(
        revalidation_module, "evaluate_approval_window", allow_known_session
    )


@pytest.fixture(autouse=True)
def _allowlisted_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        str(CHAT_ID),
        raising=False,
    )


class FakeNotifier:
    """Records every Telegram side effect; performs none."""

    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, dict | None, str]] = []
        self.answered: list[tuple[str, str | None]] = []
        self.edited: list[tuple[Any, int, str, dict | None]] = []
        self._next_message_id = 9000

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        self._next_message_id += 1
        self.sent_messages.append((text, inline_keyboard, chat_id))
        return TelegramMethodResult(
            ok=True,
            message_id=self._next_message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def answer_callback(self, callback_query_id, text=None):
        self.answered.append((callback_query_id, text))
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

    @property
    def external_calls(self) -> int:
        return len(self.sent_messages) + len(self.answered) + len(self.edited)


def make_update(
    *,
    data: str,
    chat_id: Any = CHAT_ID,
    user_id: Any = USER_ID,
    callback_id: str = "cbq-1",
    update_id: int = 1,
    message_id: int = 555,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


def session_factory_for(session) -> Callable[[], Any]:
    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[Any]:
        yield session

    return _factory


def _successful_publication(message_id: int) -> ApprovalPublication:
    return ApprovalPublication.published(
        payload_chars=100,
        method_result=TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=100,
        ),
    )


async def _publish_fixture_card(
    service: OrderProposalsService,
    group,
    *,
    nonce: str,
    card_kind: ApprovalCardKind = ApprovalCardKind.MANUAL,
    message_id: int = 555,
) -> DispatchBinding:
    if group.approval_nonce != nonce:
        await service.set_approval_nonce(group.proposal_id, nonce)
    attempt_id = uuid.uuid4()
    now = min(datetime.now(UTC), group.valid_until - timedelta(microseconds=1))
    window = await allow_known_session(group, now=now)
    binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce=nonce,
        attempt_id=attempt_id,
        card_kind=card_kind,
        current_membership_revision=group.approval_dispatch_membership_revision,
    )
    await service.start_approval_dispatch(
        group.proposal_id,
        attempt_id=attempt_id,
        binding=binding,
        now=now,
        payload_chars=100,
        context_message_count=0,
    )
    result = await service.finish_approval_dispatch(
        group.proposal_id,
        attempt_id=attempt_id,
        publication=_successful_publication(message_id),
        chat_id=str(CHAT_ID),
        now=now,
        approval_window_policy_stamp=window.policy_stamp,
    )
    assert result.ok
    return binding


async def seed_proposal(
    session, *, nonce: str = "nonce-abc123", symbol: str = "A", rungs: int = 1
):
    """Seed one published, approvable proposal and COMMIT it.

    Committed on purpose: the durable worker opens its own sessions, so a
    proposal parked in an uncommitted transaction would be invisible to it.
    """
    service = OrderProposalsService(session)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(i, "buy", Decimal("10"), Decimal("100"), None)
            for i in range(rungs)
        ],
    )
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
    return group


def proposal_callback_data(group, *, action: str = "op") -> str:
    return approval_messages.build_callback_data(
        action=action,
        proposal_id=group.proposal_id,
        nonce=group.approval_nonce,
        binding=DispatchBinding(
            attempt_id=group.approval_dispatch_attempt_id,
            card_kind=ApprovalCardKind(group.approval_dispatch_card_kind),
            membership_revision=group.approval_dispatch_membership_revision,
            membership_digest=group.approval_dispatch_membership_digest,
        ),
    )


async def seed_auto_veto_proposal(session, *, nonce: str, symbol: str = "005930"):
    """A resting auto-submitted order carrying a `vc` (veto/cancel) card."""
    service = OrderProposalsService(session)
    now = datetime.now(UTC)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        thesis="w5 auto-veto fixture",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("97000"), None)],
        source_asof={
            "auto_approved": {
                "policy_version": "test-policy",
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
        broker_order_id=f"broker-{uuid.uuid4().hex[:8]}",
        correlation_id=f"corr-{uuid.uuid4().hex[:8]}",
        idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        approval_hash_digest="digest-auto-1",
        now=now,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(
        service, group, nonce=nonce, card_kind=ApprovalCardKind.AUTO_VETO
    )
    await session.commit()
    return group


async def seed_loss_cut_proposal(session, monkeypatch, *, nonce: str):
    """A loss-cut proposal carrying an `lc` (confirm) card."""
    retro = type(
        "Retro",
        (),
        {
            "id": 42,
            "symbol": "AAPL",
            "trigger_type": "stop_loss",
            "created_at": datetime.now(UTC),
            "lesson": "손절 기준을 늦추지 않는다",
        },
    )()

    async def fake_lookup(_session, retrospective_id):
        assert retrospective_id == 42
        return retro

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    service = OrderProposalsService(session)
    group = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("99"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-1285",
    )
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
    await _publish_fixture_card(
        service,
        group,
        nonce=nonce,
        card_kind=ApprovalCardKind.LOSS_CUT_CONFIRMATION,
    )
    await session.commit()
    return group


async def seed_approval_batch(session, *, member_count: int = 2):
    """A `ba` (batch) card covering several published proposals."""
    from app.services.order_proposals.approval_message import build_batch_callback_data

    service = OrderProposalsService(session)
    now = datetime.now(UTC)
    groups = []
    registration = None
    for index in range(member_count):
        group = await seed_proposal(
            session,
            nonce=f"bm{uuid.uuid4().hex[:9]}",
            symbol=f"BW{index}",
        )
        groups.append(group)
        registration = await service.register_approval_batch_member(
            group.proposal_id,
            chat_id=str(CHAT_ID),
            approval_message_id=7100 + index,
            now=now + timedelta(seconds=index),
            summary_member_threshold=member_count,
        )
    await session.commit()
    assert registration is not None and registration.binding is not None
    batch = registration.batch
    result = await service.finish_approval_batch_dispatch(
        batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        publication=_successful_publication(7999),
        now=now + timedelta(seconds=member_count),
    )
    assert result.ok
    await session.commit()
    data = build_batch_callback_data(
        batch_id=batch.batch_id,
        nonce=batch.approval_nonce,
        binding=registration.binding,
    )
    return batch, groups, data


async def consume_nonce(proposal_id: uuid.UUID) -> None:
    """Spend an approval out of band, as a first click would have."""
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE review.order_proposals SET approval_nonce_used_at = now() "
                "WHERE proposal_id = :pid"
            ),
            {"pid": proposal_id},
        )
        await session.commit()


@pytest_asyncio.fixture
async def inbox_cleanup(_bootstrap_test_schema) -> AsyncIterator[list[uuid.UUID]]:
    """Delete every inbox row a test created, whatever the outcome."""
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    created: list[uuid.UUID] = []
    yield created
    if not created:
        return
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(TelegramCallbackInboxJob).where(
                TelegramCallbackInboxJob.job_id.in_(created)
            )
        )
        await session.commit()


async def load_job(job_id: uuid.UUID):
    """Read one inbox row through a fresh session (never a cached identity map)."""
    from sqlalchemy import select

    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(TelegramCallbackInboxJob).where(
                    TelegramCallbackInboxJob.job_id == job_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        session.expunge(row)
        return row
