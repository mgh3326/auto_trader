from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.db import AsyncSessionLocal
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.approval_message import parse_callback_data
from app.services.order_proposals.dispatch import (
    dispatch_proposal,
    send_proposal_for_approval,
)
from app.services.order_proposals.revalidation import RungOutcome
from app.services.order_proposals.service import RungInput

CHAT_ID = "chat-99"


class _FakeNotifier:
    def __init__(self, *, message_id: int | None = 5001) -> None:
        self.sent_messages: list[tuple[str, dict, str]] = []
        self.edited_messages: list[tuple[str, int, str, dict | None]] = []
        self._message_id = message_id

    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        self.sent_messages.append((text, inline_keyboard, chat_id))
        message_id = self._message_id
        if self._message_id is not None:
            self._message_id += 1
        return message_id

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited_messages.append((chat_id, message_id, text, reply_markup))
        return True


class _RaisingNotifier:
    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        raise RuntimeError("telegram down")


class _CommittedBatchNotifier(_FakeNotifier):
    def __init__(self) -> None:
        super().__init__(message_id=6500)
        self.visible_member_counts: list[int] = []

    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        button = inline_keyboard["inline_keyboard"][0][0]
        if button["text"] == "전체 승인":
            _action, batch_short, _nonce = parse_callback_data(button["callback_data"])
            async with AsyncSessionLocal() as session:
                service = OrderProposalsService(session)
                batch_id = await service.resolve_approval_batch_id_prefix(batch_short)
                assert batch_id is not None
                _batch, proposals = await service.get_approval_batch_display(batch_id)
                self.visible_member_counts.append(len(proposals))
        return await super().send_approval_message(
            text, inline_keyboard, chat_id=chat_id
        )


def _session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


async def _seed_proposal(
    db_session,
    *,
    source_asof=None,
    action=None,
    target_broker_order_id=None,
    target_order_snapshot=None,
    rungs=None,
    broker_account_id=None,
):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=rungs or [RungInput(0, "buy", Decimal("10"), Decimal("100"), None)],
        source_asof=source_asof,
        action=action,
        target_broker_order_id=target_broker_order_id,
        target_order_snapshot=target_order_snapshot,
        broker_account_id=broker_account_id,
    )
    await db_session.commit()
    return group


@pytest.mark.asyncio
async def test_send_proposal_for_approval_mints_nonce_and_sends(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(db_session)
    notifier = _FakeNotifier(message_id=5001)
    now = datetime(2026, 7, 10, 9, 20, tzinfo=UTC)

    message_id = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    assert message_id == 5001
    assert len(notifier.sent_messages) == 1
    text, keyboard, chat_id = notifier.sent_messages[0]
    assert chat_id == CHAT_ID
    assert "승인" in text
    assert keyboard["inline_keyboard"]

    service = OrderProposalsService(db_session)
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is not None
    assert refreshed.source_asof["approval_message_id"] == 5001
    assert refreshed.source_asof["approval_chat_id"] == CHAT_ID
    assert refreshed.source_asof["approval_sent_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_manual_dispatch_sends_and_updates_same_chat_batch_summary(
    monkeypatch, db_session
):
    from app.core.config import settings

    batch_chat_id = f"batch-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", batch_chat_id
    )
    first = await _seed_proposal(db_session)
    second = await _seed_proposal(db_session)
    third = await _seed_proposal(db_session)
    notifier = _FakeNotifier(message_id=6000)
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)

    first_id = await send_proposal_for_approval(
        first.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )
    assert first_id == 6000
    assert len(notifier.sent_messages) == 1

    second_id = await send_proposal_for_approval(
        second.proposal_id,
        notifier=notifier,
        now=now.replace(minute=1),
        service_factory=_session_factory(db_session),
    )
    assert second_id == 6001
    assert len(notifier.sent_messages) == 3
    summary_text, summary_keyboard, summary_chat = notifier.sent_messages[-1]
    assert summary_chat == batch_chat_id
    assert "제안: 2건" in summary_text
    assert summary_keyboard["inline_keyboard"][0][0]["text"] == "전체 승인"

    third_id = await send_proposal_for_approval(
        third.proposal_id,
        notifier=notifier,
        now=now.replace(minute=2),
        service_factory=_session_factory(db_session),
    )
    assert third_id == 6003
    assert notifier.edited_messages
    assert notifier.edited_messages[-1][1] == 6002
    assert "제안: 3건" in notifier.edited_messages[-1][2]


@pytest.mark.asyncio
async def test_batch_summary_is_sent_only_after_membership_commit(
    monkeypatch, db_session
):
    from app.core.config import settings

    batch_chat_id = f"batch-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", batch_chat_id
    )
    first = await _seed_proposal(db_session)
    second = await _seed_proposal(db_session)
    notifier = _CommittedBatchNotifier()
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)

    await send_proposal_for_approval(
        first.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )
    await send_proposal_for_approval(
        second.proposal_id,
        notifier=notifier,
        now=now.replace(minute=1),
        service_factory=_session_factory(db_session),
    )

    assert notifier.visible_member_counts == [2]


@pytest.mark.asyncio
async def test_send_proposal_for_approval_renders_initial_replace_action(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(
        db_session,
        action="replace",
        target_broker_order_id="old-1",
        target_order_snapshot={
            "broker_order_id": "old-1",
            "symbol": "005930",
            "side": "buy",
            "order_type": "limit",
            "limit_price": "42000",
            "remaining_quantity": "3.5",
            "status": "open",
            "observed_at": "2026-07-11T00:00:00+00:00",
        },
        rungs=[RungInput(0, "buy", Decimal("3.5"), Decimal("43000"), None)],
    )
    notifier = _FakeNotifier()

    await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 7, 10, 9, 20, tzinfo=UTC),
        service_factory=_session_factory(db_session),
    )

    text, _, _ = notifier.sent_messages[0]
    assert "replace" in text
    assert "old-1" in text
    assert "변경 전: 수량 3.5 / 가격 ₩42,000" in text
    assert "변경 후: 수량 3.5 / 가격 ₩43,000" in text
    assert "재확인" not in text


@pytest.mark.asyncio
async def test_send_proposal_for_approval_preserves_existing_source_asof(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(
        db_session, source_asof={"resting_deadline": "2026-07-10T15:30:00+09:00"}
    )
    notifier = _FakeNotifier(message_id=7001)
    now = datetime(2026, 7, 10, 9, 21, tzinfo=UTC)

    await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    service = OrderProposalsService(db_session)
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.source_asof["resting_deadline"] == "2026-07-10T15:30:00+09:00"
    assert refreshed.source_asof["approval_message_id"] == 7001


@pytest.mark.asyncio
async def test_send_proposal_for_approval_empty_allowlist_is_noop(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "")
    group = await _seed_proposal(db_session)
    notifier = _FakeNotifier()
    now = datetime(2026, 7, 10, 9, 22, tzinfo=UTC)

    message_id = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    assert message_id is None
    assert notifier.sent_messages == []

    service = OrderProposalsService(db_session)
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is None


@pytest.mark.asyncio
async def test_send_proposal_for_approval_send_failure_returns_none_but_nonce_committed(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(db_session)
    notifier = _FakeNotifier(message_id=None)
    now = datetime(2026, 7, 10, 9, 23, tzinfo=UTC)

    message_id = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    assert message_id is None

    service = OrderProposalsService(db_session)
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is not None
    assert refreshed.source_asof is None


@pytest.mark.asyncio
async def test_dispatch_auto_gate_off_preserves_human_approval_flow(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", False)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(db_session)
    notifier = _FakeNotifier()

    async def must_not_revalidate(**kwargs):
        raise AssertionError("auto revalidation must stay disabled")

    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=must_not_revalidate,
    )

    assert "주문 제안 승인" in notifier.sent_messages[0][0]
    _group, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "pending_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_dispatch_auto_eligible_buy_or_sell_rests_without_approval(
    monkeypatch, db_session, side
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    service = OrderProposalsService(db_session)
    limit_price = Decimal("97000") if side == "buy" else Decimal("103000")
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side=side,
        order_type="limit",
        proposer="p",
        thesis="resting entry",
        broker_account_id=f"dispatch-auto-{side}-{uuid.uuid4()}",
        rungs=[RungInput(0, side, Decimal("1"), limit_price, None)],
    )
    await db_session.commit()

    async def fake_revalidate(*, service, proposal_id, now, eligibility_gate):
        fresh_group, rungs = await service.get_proposal(proposal_id)
        decision = await eligibility_gate(
            group=fresh_group,
            rung=rungs[0],
            preview={
                "success": True,
                "current_price": "100000",
                "price": str(limit_price),
                "quantity": "1",
            },
            now=now,
        )
        assert decision.eligible is True
        await service.transition_rung(proposal_id, 0, new_state="revalidating")
        await service.transition_rung(proposal_id, 0, new_state="approved")
        await service.transition_rung(proposal_id, 0, new_state="submitting")
        await service.record_resting(
            proposal_id,
            0,
            broker_order_id="broker-1",
            correlation_id="corr-1",
            idempotency_key="idem-1",
            approval_hash_digest="digest-1",
            now=now,
        )
        return [RungOutcome(0, "submitted_resting", {})]

    notifier = _FakeNotifier(message_id=8123)
    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=fake_revalidate,
    )

    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"
    assert refreshed.approved_by_telegram_user_id is None
    assert refreshed.source_asof["auto_approved"]["policy_version"] == "2026-07-17.2"
    text, keyboard, _chat_id = notifier.sent_messages[0]
    assert "자동 접수됨" in text
    assert "auto:policy@2026-07-17.2" in text
    assert keyboard["inline_keyboard"][0][0]["text"] == "취소"
    assert keyboard["inline_keyboard"][0][0]["callback_data"].startswith("vc:")

    async def duplicate_must_not_revalidate(**kwargs):
        raise AssertionError("an already-submitted proposal must not dispatch twice")

    duplicate = await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 7, 14, 1, 0, 1, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=duplicate_must_not_revalidate,
    )
    assert duplicate is None
    assert len(notifier.sent_messages) == 1


@pytest.mark.asyncio
async def test_dispatch_auto_ineligible_degrades_to_human_approval(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(
        db_session, broker_account_id="dispatch-auto-ineligible"
    )

    async def fake_revalidate(*, service, proposal_id, now, eligibility_gate):
        fresh_group, rungs = await service.get_proposal(proposal_id)
        decision = await eligibility_gate(
            group=fresh_group,
            rung=rungs[0],
            preview={
                "success": True,
                "current_price": "101",
                "price": "100",
                "quantity": "10",
            },
            now=now,
        )
        assert decision.reason == "distance_below_minimum"
        return [
            RungOutcome(
                0,
                "approval_required",
                {"reason": decision.reason},
            )
        ]

    notifier = _FakeNotifier()
    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=fake_revalidate,
    )

    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "pending_approval"
    assert "auto_approved" not in (refreshed.source_asof or {})
    assert "주문 제안 승인" in notifier.sent_messages[0][0]


@pytest.mark.asyncio
async def test_dispatch_auto_861_reconfirm_degrades_without_losing_state(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(db_session, broker_account_id="dispatch-auto-861")

    async def fake_revalidate(*, service, proposal_id, now, eligibility_gate):
        await service.transition_rung(proposal_id, 0, new_state="revalidating")
        await service.mark_needs_reconfirm(proposal_id, 0, now=now)
        return [
            RungOutcome(
                0,
                "needs_reconfirm",
                {"reason": "insufficient_buying_power"},
            )
        ]

    notifier = _FakeNotifier()
    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=fake_revalidate,
    )

    _refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "needs_reconfirm"
    assert notifier.sent_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_failure", ["none", "raises"])
async def test_auto_notify_failure_compensates_by_cancelling_live_order(
    monkeypatch, db_session, notify_failure
):
    from app.core.config import settings
    from app.services.order_proposals.target_order import TargetOrderSnapshot

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id=f"notify-failure-{uuid.uuid4()}",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("97000"), None)],
    )
    await db_session.commit()

    async def fake_revalidate(*, service, proposal_id, now, eligibility_gate):
        fresh_group, rungs = await service.get_proposal(proposal_id)
        decision = await eligibility_gate(
            group=fresh_group,
            rung=rungs[0],
            preview={"success": True, "current_price": "100000"},
            now=now,
        )
        assert decision.eligible is True
        await service.transition_rung(proposal_id, 0, new_state="revalidating")
        await service.transition_rung(proposal_id, 0, new_state="approved")
        await service.transition_rung(proposal_id, 0, new_state="submitting")
        await service.record_resting(
            proposal_id,
            0,
            broker_order_id="broker-notify-failure",
            correlation_id="corr",
            idempotency_key="idem",
            approval_hash_digest="digest",
            now=now,
        )
        return [RungOutcome(0, "submitted_resting", {})]

    cancel_calls = []

    async def cancel_fn(**kwargs):
        cancel_calls.append(kwargs)
        return {"success": True}

    async def fetch_fn(**kwargs):
        return TargetOrderSnapshot(
            broker_order_id="broker-notify-failure",
            symbol="005930",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="1",
            status="cancelled",
            observed_at=kwargs["now"].isoformat(),
        )

    notifier = (
        _FakeNotifier(message_id=None)
        if notify_failure == "none"
        else _RaisingNotifier()
    )
    result = await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=fake_revalidate,
        cancel_target_fn=cancel_fn,
        fetch_target_fn=fetch_fn,
    )

    assert result is None
    assert cancel_calls[0]["order_id"] == "broker-notify-failure"
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "cancelled"
    assert (
        refreshed.source_asof["auto_approved"]["notification_failure"]["outcomes"][0][
            "result"
        ]
        == "cancelled"
    )
