from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.dispatch import send_proposal_for_approval
from app.services.order_proposals.service import RungInput

CHAT_ID = "chat-99"


class _FakeNotifier:
    def __init__(self, *, message_id: int | None = 5001) -> None:
        self.sent_messages: list[tuple[str, dict, str]] = []
        self._message_id = message_id

    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        self.sent_messages.append((text, inline_keyboard, chat_id))
        return self._message_id


class _RaisingNotifier:
    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        raise RuntimeError("telegram down")


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
