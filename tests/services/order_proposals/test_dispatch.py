from __future__ import annotations

import contextlib
import functools
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.db import AsyncSessionLocal
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals.approval_message import (
    build_approval_dispatch_messages,
    parse_callback_data,
)
from app.services.order_proposals.approval_window import (
    ApprovalWindowCode,
    ApprovalWindowDecision,
    SubmissionSessionEvidence,
    evaluate_approval_window,
)
from app.services.order_proposals.auto_approve import AutoApproveLimits
from app.services.order_proposals.dispatch import (
    dispatch_proposal,
    publish_approval_messages,
    send_proposal_for_approval,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.revalidation import RungOutcome, revalidate_and_submit
from app.services.order_proposals.service import RungInput
from app.services.trading_policy_service import policy_version_stamp
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session

CHAT_ID = "chat-99"


@pytest.fixture(autouse=True)
def _known_market_session(monkeypatch):
    monkeypatch.setattr(
        dispatch_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(
        revalidation_module, "evaluate_approval_window", allow_known_session
    )


@pytest.mark.asyncio
async def test_card_name_resolution_reuses_notification_source_and_fails_open(
    monkeypatch,
):
    group = SimpleNamespace(symbol="005930", market="equity_kr")

    async def resolved(market: str, symbol: str) -> str:
        assert (market, symbol) == ("kr", "005930")
        return "삼성전자"

    monkeypatch.setattr(dispatch_module, "resolve_display_name_db", resolved)
    assert await dispatch_module._resolve_card_display_name(group) == "삼성전자"

    async def unavailable(_market: str, _symbol: str) -> str:
        raise RuntimeError("name lookup unavailable")

    monkeypatch.setattr(dispatch_module, "resolve_display_name_db", unavailable)
    assert await dispatch_module._resolve_card_display_name(group) is None


class _FakeNotifier:
    def __init__(self, *, message_id: int | None = 5001) -> None:
        self.sent_messages: list[tuple[str, dict | None, str]] = []
        self.parse_modes: list[str | None] = []
        self.edited_messages: list[tuple[str, int, str, dict | None]] = []
        self.auto_veto_mirrors: list[dict] = []
        self._message_id = message_id

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        self.sent_messages.append((text, inline_keyboard, chat_id))
        self.parse_modes.append(parse_mode)
        message_id = self._message_id
        if self._message_id is not None:
            self._message_id += 1
            return TelegramMethodResult(
                ok=True,
                message_id=message_id,
                status_code=200,
                error_code=None,
                error_classification=None,
                payload_chars=telegram_text_length(text),
            )
        return TelegramMethodResult.failed(
            payload_chars=telegram_text_length(text),
            failure_code="telegram_error_400",
            status_code=400,
            error_code=400,
        )

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited_messages.append((chat_id, message_id, text, reply_markup))
        return TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def send_auto_veto_card_mirror(self, **kwargs):
        self.auto_veto_mirrors.append(kwargs)
        return True


class _RaisingNotifier:
    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        raise RuntimeError("telegram down")


class _FailAtNotifier(_FakeNotifier):
    def __init__(self, *, fail_at: int, message_id: int = 9000) -> None:
        super().__init__(message_id=message_id)
        self._fail_at = fail_at

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        if len(self.sent_messages) + 1 == self._fail_at:
            self.sent_messages.append((text, inline_keyboard, chat_id))
            self.parse_modes.append(parse_mode)
            return TelegramMethodResult.failed(
                payload_chars=telegram_text_length(text),
                failure_code="telegram_error_400",
                status_code=400,
                error_code=400,
            )
        return await super().send_approval_message(
            text,
            inline_keyboard,
            chat_id=chat_id,
            parse_mode=parse_mode,
        )


class _CommittedBatchNotifier(_FakeNotifier):
    def __init__(self) -> None:
        super().__init__(message_id=6500)
        self.visible_member_counts: list[int] = []

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        button = inline_keyboard["inline_keyboard"][0][0]
        if button["text"] == "전체 승인":
            parsed = parse_callback_data(button["callback_data"])
            async with AsyncSessionLocal() as session:
                service = OrderProposalsService(session)
                batch_id = await service.resolve_approval_batch_id_prefix(
                    parsed.subject_short
                )
                assert batch_id is not None
                _batch, proposals = await service.get_approval_batch_display(batch_id)
                self.visible_member_counts.append(len(proposals))
        return await super().send_approval_message(
            text, inline_keyboard, chat_id=chat_id, parse_mode=parse_mode
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_long_thesis_sends_only_one_compact_button_card() -> None:
    group = OrderProposal(
        proposal_id=uuid.uuid4(),
        approval_nonce="unit-nonce1",
        market="equity_kr",
        symbol="259960",
        side="buy",
        order_type="limit",
        action="place",
        thesis="가" * 4444,
        strategy="분할 매도",
    )
    rung = OrderProposalRung(
        rung_index=0,
        side="buy",
        quantity=Decimal("1"),
        limit_price=Decimal("100000"),
    )
    binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce=group.approval_nonce,
        attempt_id=uuid.uuid4(),
        card_kind=ApprovalCardKind.MANUAL,
        current_membership_revision=None,
    )
    messages = build_approval_dispatch_messages(
        group=group, rungs=[rung], binding=binding
    )
    successful_notifier = _FakeNotifier(message_id=9200)
    sent = await publish_approval_messages(
        notifier=successful_notifier,
        messages=messages,
        chat_id=CHAT_ID,
    )

    assert sent.card_published is True
    assert sent.message_id == 9200
    assert len(successful_notifier.sent_messages) == 1
    assert successful_notifier.sent_messages[0][1]["inline_keyboard"]


def _session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


async def _seed_proposal(
    db_session,
    *,
    source_asof=None,
    market="equity_kr",
    account_mode="kis_live",
    action=None,
    target_broker_order_id=None,
    target_order_snapshot=None,
    rungs=None,
    broker_account_id=None,
    thesis="test thesis",
    strategy=None,
):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market=market,
        account_mode=account_mode,
        side="buy",
        order_type="limit",
        proposer="p",
        thesis=thesis,
        strategy=strategy,
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

    isolated_chat_id = f"chat-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        isolated_chat_id,
    )
    group = await _seed_proposal(db_session)
    notifier = _FakeNotifier(message_id=5001)
    now = datetime(2026, 7, 10, 9, 20, tzinfo=UTC)

    dispatch = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    assert dispatch.ok is True
    assert dispatch.message_id == 5001
    assert len(notifier.sent_messages) == 1
    text, keyboard, chat_id = notifier.sent_messages[0]
    assert chat_id == isolated_chat_id
    assert "승인" in text
    assert keyboard["inline_keyboard"]

    service = OrderProposalsService(db_session)
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is not None
    assert refreshed.source_asof["approval_message_id"] == 5001
    assert refreshed.source_asof["approval_chat_id"] == isolated_chat_id
    assert refreshed.source_asof["approval_sent_at"] == now.isoformat()
    assert refreshed.source_asof["approval_window_policy_stamp"]


@pytest.mark.asyncio
async def test_dispatch_expired_returns_typed_result_without_nonce_or_telegram(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(db_session)
    deadline = datetime(2026, 7, 23, 4, 55, tzinfo=UTC)
    group.valid_until = deadline
    await db_session.commit()
    notifier = _FakeNotifier()

    result = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=deadline,
        service_factory=_session_factory(db_session),
    )

    assert isinstance(result, ApprovalWindowDecision)
    assert result.code is ApprovalWindowCode.EXPIRED
    assert notifier.sent_messages == []
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.approval_nonce is None
    assert refreshed.lifecycle_state == "expired"
    assert refreshed.approval_dispatch_state == "failed"
    assert (
        refreshed.approval_dispatch_failure_code
        == "EXPIRED/now_at_or_after_valid_until"
    )
    assert refreshed.approval_dispatch_attempted_at == deadline
    assert refreshed.approval_dispatch_published_at is None
    assert [rung.state for rung in rungs] == ["expired"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("known", "expected", "expected_failure"),
    [
        (
            True,
            ApprovalWindowCode.DEFER_SESSION_CLOSED,
            "DEFER_SESSION_CLOSED/submission_session_closed",
        ),
        (
            False,
            ApprovalWindowCode.CALENDAR_UNKNOWN,
            "CALENDAR_UNKNOWN/nxt_capability_stale",
        ),
    ],
)
async def test_dispatch_closed_or_unknown_session_is_durably_blocked_without_telegram(
    monkeypatch, db_session, known, expected, expected_failure
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(db_session)
    now = datetime(2026, 7, 23, 8, 50, tzinfo=UTC)
    group.valid_until = now + timedelta(days=2)
    await db_session.commit()
    notifier = _FakeNotifier()

    async def evaluator(group, *, now):
        async def resolver(group, *, now):
            return SubmissionSessionEvidence(
                known=known,
                source="test",
                current_session="closed" if known else "unknown",
                allowed_sessions=("regular",),
                allowed_now=False,
                next_allowed_at=now + timedelta(hours=12) if known else None,
                detail=None if known else "nxt_capability_stale",
            )

        return await evaluate_approval_window(group, now=now, session_resolver=resolver)

    result = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
        window_evaluator=evaluator,
    )

    assert isinstance(result, ApprovalWindowDecision)
    assert result.code is expected
    assert notifier.sent_messages == []
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.approval_nonce is None
    assert refreshed.approval_nonce_used_at is None
    assert refreshed.approval_dispatch_state == "failed"
    assert refreshed.approval_dispatch_failure_code == expected_failure
    assert refreshed.approval_dispatch_attempted_at == now
    assert refreshed.approval_dispatch_published_at is None
    assert [rung.state for rung in rungs] == ["pending_approval"]


@pytest.mark.asyncio
async def test_manual_dispatch_freezes_published_batch_and_starts_next_card(
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
    fourth = await _seed_proposal(db_session)
    notifier = _FakeNotifier(message_id=6000)
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)

    first_result = await send_proposal_for_approval(
        first.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )
    assert first_result.message_id == 6000
    assert len(notifier.sent_messages) == 1

    second_result = await send_proposal_for_approval(
        second.proposal_id,
        notifier=notifier,
        now=now.replace(minute=1),
        service_factory=_session_factory(db_session),
    )
    assert second_result.message_id == 6001
    assert len(notifier.sent_messages) == 3
    summary_text, summary_keyboard, summary_chat = notifier.sent_messages[-1]
    assert summary_chat == batch_chat_id
    assert "제안: 2건" in summary_text
    assert summary_keyboard["inline_keyboard"][0][0]["text"] == "전체 승인"

    third_result = await send_proposal_for_approval(
        third.proposal_id,
        notifier=notifier,
        now=now.replace(minute=2),
        service_factory=_session_factory(db_session),
    )
    assert third_result.message_id == 6003
    assert len(notifier.sent_messages) == 4
    assert notifier.edited_messages == []

    fourth_result = await send_proposal_for_approval(
        fourth.proposal_id,
        notifier=notifier,
        now=now.replace(minute=3),
        service_factory=_session_factory(db_session),
    )
    assert fourth_result.message_id == 6004
    assert len(notifier.sent_messages) == 6
    next_summary_text, next_summary_keyboard, next_summary_chat = (
        notifier.sent_messages[-1]
    )
    assert next_summary_chat == batch_chat_id
    assert "제안: 2건" in next_summary_text
    assert next_summary_keyboard["inline_keyboard"][0][0]["text"] == "전체 승인"
    assert next_summary_text != summary_text
    assert notifier.edited_messages == []


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

    dispatch = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    assert dispatch.ok is False
    assert dispatch.failure_code == "telegram_allowlist_empty"
    assert notifier.sent_messages == []

    service = OrderProposalsService(db_session)
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is None
    assert refreshed.approval_dispatch_state == "failed"


@pytest.mark.asyncio
async def test_send_proposal_for_approval_send_failure_is_durable_and_invalidates_nonce(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(db_session)
    notifier = _FakeNotifier(message_id=None)
    now = datetime(2026, 7, 10, 9, 23, tzinfo=UTC)

    dispatch = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    assert dispatch.ok is False
    assert dispatch.failure_code == "approval_card_dispatch_failed"

    service = OrderProposalsService(db_session)
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is None
    assert refreshed.approval_dispatch_state == "failed"
    assert refreshed.approval_dispatch_failure_code == "approval_card_dispatch_failed"
    assert refreshed.approval_dispatch_payload_chars == dispatch.payload_chars
    assert refreshed.source_asof is None


@pytest.mark.asyncio
async def test_long_thesis_dispatches_one_card_and_preserves_db_text(
    monkeypatch, db_session
):
    from app.core.config import settings

    chat_id = f"long-thesis-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )
    thesis = "가" * 4444
    group = await _seed_proposal(
        db_session,
        thesis=thesis,
        strategy="분할 매도",
    )
    notifier = _FakeNotifier(message_id=9100)
    sent = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 7, 23, 2, 17, 50, tzinfo=UTC),
        service_factory=_session_factory(db_session),
    )

    assert sent.ok is True
    assert sent.payload_chars < 4096
    assert len(notifier.sent_messages) == 1
    assert notifier.sent_messages[0][1]["inline_keyboard"]
    assert thesis not in notifier.sent_messages[0][0]
    refreshed, _ = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.thesis == thesis


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
    loaded_policy_version = policy_version_stamp()["version"]
    assert (
        refreshed.source_asof["auto_approved"]["policy_version"]
        == loaded_policy_version
    )
    text, keyboard, _chat_id = notifier.sent_messages[0]
    assert "자동 접수됨" in text
    assert f"auto:policy@{loaded_policy_version}" in text
    assert keyboard["inline_keyboard"][0][0]["text"] == "취소"
    assert keyboard["inline_keyboard"][0][0]["callback_data"].startswith("vc:")
    assert notifier.auto_veto_mirrors == [
        {
            "symbol": "005930",
            "market": "equity_kr",
            "quantities": ["1"],
            "prices": [str(limit_price)],
            "thesis_summary": "resting entry",
            "policy_version": loaded_policy_version,
            # §141차: the Discord mirror title tracks the action.
            "action": "place",
        }
    ]

    async def duplicate_must_not_revalidate(**kwargs):
        raise AssertionError("an already-submitted proposal must not dispatch twice")

    duplicate = await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 7, 14, 1, 0, 1, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=duplicate_must_not_revalidate,
    )
    assert duplicate.ok is False
    assert duplicate.failure_code == "proposal_not_pending_approval"
    assert len(notifier.sent_messages) == 1


@pytest.mark.asyncio
async def test_dispatch_auto_eligible_qqq_records_cap_observation(
    monkeypatch, db_session
):
    from app.core.config import settings

    now = datetime(2026, 8, 14, 22, 35, tzinfo=UTC)
    limits = AutoApproveLimits(
        min_distance_pct=Decimal("3"),
        per_order_cap=Decimal("1000"),
        daily_cap=Decimal("800"),
        policy_version="2026-08-14.1",
        policy_content_hash="51c789434f6a",
    )
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(dispatch_module, "limits_for_market", lambda _market: limits)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="QQQ",
        market="equity_us",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="cap-observation-fixture",
        thesis="resting entry",
        broker_account_id=f"cap-observation-{uuid.uuid4()}",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("741.41"), None)],
        now=now,
        valid_until=now + timedelta(hours=1),
    )
    await db_session.commit()

    async def fake_revalidate(*, service, proposal_id, now, eligibility_gate):
        fresh_group, rungs = await service.get_proposal(proposal_id)
        decision = await eligibility_gate(
            group=fresh_group,
            rung=rungs[0],
            preview={
                "success": True,
                "current_price": "800",
                "price": "741.41",
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
            broker_order_id="cap-observation-order",
            correlation_id="cap-observation-correlation",
            idempotency_key="cap-observation-idempotency",
            approval_hash_digest="cap-observation-digest",
            now=now,
        )
        return [RungOutcome(0, "submitted_resting", {})]

    await dispatch_proposal(
        group.proposal_id,
        notifier=_FakeNotifier(),
        now=now,
        service_factory=_session_factory(db_session),
        revalidate_fn=fake_revalidate,
    )

    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.source_asof["auto_approved"]["cap_observations"] == [
        {
            "rung_index": 0,
            "daily_cap": "800",
            "daily_notional_before": "0",
            "daily_notional_after": "741.41",
            "per_order_cap": "1000",
            "notional": "741.41",
            "policy_version": "2026-08-14.1",
            "content_hash": "51c789434f6a",
            "evaluated_at": now.isoformat(),
        }
    ]
    assert "auto_approve_rejections" not in refreshed.source_asof


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
    audit = refreshed.source_asof["auto_approve_rejections"][-1]
    evidence = audit["rungs"][0]
    assert evidence["rung_index"] == 0
    assert evidence["reason_code"] == "distance_below_minimum"
    expected_inputs = {
        "mode": "off",
        "policy_version": policy_version_stamp()["version"],
        "current_price": "101",
        "limit_price": "100",
        "quantity": "10",
    }
    assert {key: evidence["inputs"][key] for key in expected_inputs} == expected_inputs


@pytest.mark.asyncio
async def test_dispatch_auto_tag_rejection_persists_safe_token_evidence_and_card(
    monkeypatch, db_session
):
    """A manual fallback keeps token + location, never the matched prose."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    raw_match_context = "private-context-policy_deviation-marker"
    group = await _seed_proposal(
        db_session,
        broker_account_id="dispatch-auto-tag-evidence",
        thesis="ordinary support retest",
        strategy="ladder",
        source_asof={"context": {"tags": [raw_match_context]}},
    )

    async def fake_revalidate(*, service, proposal_id, now, eligibility_gate):
        fresh_group, rungs = await service.get_proposal(proposal_id)
        decision = await eligibility_gate(
            group=fresh_group,
            rung=rungs[0],
            preview={
                "success": True,
                "current_price": "110",
                "price": "100",
                "quantity": "10",
            },
            now=now,
        )
        assert decision.reason == "approval_required_tag"
        return [RungOutcome(0, "approval_required", {"reason": decision.reason})]

    notifier = _FakeNotifier()
    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=fake_revalidate,
    )

    refreshed, _rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    audit = refreshed.source_asof["auto_approve_rejections"][-1]
    evidence = audit["rungs"][0]
    assert evidence["reason_code"] == "approval_required_tag"
    assert evidence["inputs"]["tags"] == ["policy_deviation"]
    assert evidence["inputs"]["tag_matches"] == [
        {
            "token": "policy_deviation",
            "field": "source_asof",
            "path": "$.context.tags[0]",
            "kind": "json_value",
            "char_start": raw_match_context.index("policy_deviation"),
        }
    ]
    assert raw_match_context not in json.dumps(audit)

    card_text = notifier.sent_messages[0][0]
    assert "자동 승인 제외" in card_text
    assert "approval_required_tag" in card_text
    assert "policy_deviation" in card_text
    assert "source_asof.context.tags[0]" in card_text
    assert raw_match_context not in card_text


@pytest.mark.asyncio
async def test_missing_auto_veto_thesis_never_revalidates_or_submits(
    monkeypatch, db_session
):
    """CARD_FIELDS: no thesis means manual approval before any broker edge."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _seed_proposal(
        db_session,
        broker_account_id=f"missing-thesis-{uuid.uuid4()}",
        thesis=" ",
    )

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("missing thesis must block before broker revalidation")

    notifier = _FakeNotifier()
    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=must_not_revalidate,
    )

    assert "주문 제안 승인" in notifier.sent_messages[-1][0]
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "pending_approval"
    assert "auto_approved" not in (refreshed.source_asof or {})
    assert "주문 제안 승인" in notifier.sent_messages[0][0]
    evidence = refreshed.source_asof["auto_approve_rejections"][-1]["rungs"]
    assert evidence == [
        {
            "rung_index": 0,
            "reason_code": "auto_veto_thesis_missing",
            "inputs": {
                "policy_version": policy_version_stamp()["version"],
                "thesis_present": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_dispatch_records_multi_rung_fallback_reason_for_manual_card(
    monkeypatch, db_session
):
    """The revalidation short-circuit is observable without a broker call."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"multi-rung-{uuid.uuid4().hex}",
    )
    group = await _seed_proposal(
        db_session,
        broker_account_id=f"multi-rung-{uuid.uuid4()}",
        rungs=[
            RungInput(0, "buy", Decimal("10"), Decimal("100"), None),
            RungInput(1, "buy", Decimal("10"), Decimal("99"), None),
        ],
    )

    async def multi_rung_fallback(**_kwargs):
        return [
            RungOutcome(
                0, "approval_required", {"reason": "multi_rung_requires_approval"}
            ),
            RungOutcome(
                1, "approval_required", {"reason": "multi_rung_requires_approval"}
            ),
        ]

    await dispatch_proposal(
        group.proposal_id,
        notifier=_FakeNotifier(),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=multi_rung_fallback,
    )

    refreshed, _rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    evidence = refreshed.source_asof["auto_approve_rejections"][-1]["rungs"]
    assert [row["reason_code"] for row in evidence] == [
        "multi_rung_requires_approval",
        "multi_rung_requires_approval",
    ]
    assert all(row["inputs"]["pending_rung_count"] == "2" for row in evidence)


@pytest.mark.asyncio
async def test_dispatch_records_eligibility_error_without_raw_exception_detail(
    monkeypatch, db_session
):
    """A fail-closed gate error retains its typed cause, not exception prose."""
    from app.core.config import settings

    raw_error = "private-eligibility-error-must-not-escape"
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"eligibility-error-{uuid.uuid4().hex}",
    )
    group = await _seed_proposal(
        db_session, broker_account_id=f"eligibility-error-{uuid.uuid4()}"
    )

    async def failed_gate(**_kwargs):
        return [
            RungOutcome(
                0,
                "approval_required",
                {"reason": "eligibility_error", "error": raw_error},
            )
        ]

    await dispatch_proposal(
        group.proposal_id,
        notifier=_FakeNotifier(),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=failed_gate,
    )

    refreshed, _rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    evidence = refreshed.source_asof["auto_approve_rejections"][-1]["rungs"][0]
    assert evidence["reason_code"] == "eligibility_error"
    assert evidence["inputs"]["eligibility_error"] is True
    assert raw_error not in json.dumps(evidence)


@pytest.mark.asyncio
async def test_dispatch_records_toss_freeze_without_entering_revalidation(
    monkeypatch, db_session
):
    """A verified-fill freeze is a manual fallback with durable reason input."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"toss-freeze-{uuid.uuid4().hex}",
    )
    group = await _seed_proposal(
        db_session,
        account_mode="toss_live",
        broker_account_id=f"toss-freeze-{uuid.uuid4()}",
    )

    async def frozen_lane(self, group, *, now):
        return {"state": "frozen"}

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("freeze must stop revalidation")

    monkeypatch.setattr(
        OrderProposalsService,
        "active_toss_auto_submission_freeze",
        frozen_lane,
    )
    await dispatch_proposal(
        group.proposal_id,
        notifier=_FakeNotifier(),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=must_not_revalidate,
    )

    refreshed, _rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    evidence = refreshed.source_asof["auto_approve_rejections"][-1]["rungs"][0]
    assert evidence["reason_code"] == "toss_auto_submission_frozen"
    assert evidence["inputs"]["toss_auto_submission_frozen"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account_mode,market",
    [
        ("toss_live", "equity_kr"),
        ("kis_live", "equity_kr"),
        ("upbit", "crypto"),
    ],
)
@pytest.mark.parametrize("action", ["cancel", "replace"])
async def test_dispatch_auto_approve_cancel_replace_never_mutates_before_gate(
    monkeypatch, db_session, account_mode, market, action
):
    """ROB-972 round-1 regression -- the 07-20 incident.

    Auto-dispatch previously reached ``_cancel_and_confirm_target`` (a real
    broker cancel) for cancel/replace proposals without ever consulting the
    eligibility gate, because ``revalidate_and_submit`` only threaded
    ``eligibility_gate`` into the ``place`` branch.

    §141차 note: the gate no longer rejects cancel/replace *categorically*, so
    this fixture no longer relies on that. It pins the ordering invariant the
    incident was actually about -- no broker mutation before the gate has had
    its say -- using a proposal that fails a gate for an unrelated reason (no
    thesis, so no renderable veto card). If a future change ever lets the
    broker cancel run first, ``cancel_fn`` raises and this test goes red for
    ALL three veto-capable account/market pairs.

    Deliberately exercises the REAL ``revalidate_and_submit`` (not a fake
    ``revalidate_fn`` that calls ``eligibility_gate`` by hand, as the other
    tests in this module do) through ``dispatch_proposal``'s real auto-
    approve branch end to end -- only the broker/network edges
    (``fetch_target_fn``/``cancel_target_fn``/``place_order_fn``/
    ``opposite_pending_check_fn``) are faked. This is the shape of coverage
    the round-1 audit found missing: a fake ``revalidate_fn`` that manually
    invokes ``eligibility_gate`` can never catch a bug in
    ``revalidate_and_submit`` not calling it at all for a given action.
    """
    from app.core.config import settings
    from app.services.order_proposals.target_order import TargetOrderSnapshot

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    # Unique per-parametrization chat id -- batches scope by chat, and this
    # module's tests commit real rows to the shared test DB, so a fixed
    # CHAT_ID lets an earlier parametrization's proposal leak into this run's
    # 10-minute batch window and add an unrelated "전체 승인" batch-summary
    # send (see `_unique_chat` in test_mcp_order_proposal_tools.py).
    chat_id = f"chat-gate-{account_mode}-{action}-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )

    symbol = "KRW-AVAX" if market == "crypto" else "005930"
    side = "sell"
    limit_price = Decimal("43000") if action == "replace" else Decimal("42000")
    target_snapshot = TargetOrderSnapshot(
        broker_order_id="broker-gate-1",
        symbol=symbol,
        side=side,
        order_type="limit",
        limit_price="42000",
        remaining_quantity="3.5",
        status="open",
        observed_at=datetime.now(UTC).isoformat(),
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market=market,
        account_mode=account_mode,
        side=side,
        order_type="limit",
        proposer="p",
        broker_account_id=f"gate-{account_mode}-{action}-{uuid.uuid4()}",
        action=action,
        target_broker_order_id="broker-gate-1",
        target_order_snapshot=target_snapshot.to_payload(),
        rungs=[RungInput(0, side, Decimal("3.5"), limit_price, None)],
    )
    await db_session.commit()

    cancel_calls: list[dict] = []

    async def fetch_fn(**kwargs):
        return target_snapshot

    async def cancel_fn(**kwargs):
        cancel_calls.append(kwargs)
        raise AssertionError(
            "BROKER_CANCEL must never fire before the eligibility gate runs"
        )

    async def place_fn(**kwargs):
        if kwargs.get("dry_run"):
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": str(limit_price),
                "quantity": "3.5",
            }
        raise AssertionError("live submit must never fire before the gate runs")

    async def no_opposite_pending(**kwargs):
        return None

    notifier = _FakeNotifier(message_id=7700)
    result = await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=functools.partial(
            revalidate_and_submit,
            fetch_target_fn=fetch_fn,
            cancel_target_fn=cancel_fn,
            place_order_fn=place_fn,
            opposite_pending_check_fn=no_opposite_pending,
        ),
    )

    assert cancel_calls == []
    assert result.ok is True  # nonce minted + ordinary approval message sent
    # Falls back to the ordinary human-approval message ("주문 제안 승인"),
    # never the auto-approved "자동 접수됨" veto message -- TELEGRAM_APPROVAL_SEND
    # for the human-click flow, not a post-hoc veto button on an already-sent order.
    assert len(notifier.sent_messages) == 1
    text, _keyboard, _chat_id = notifier.sent_messages[0]
    assert "주문 제안 승인" in text
    assert "자동 접수됨" not in text

    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "pending_approval"
    assert "auto_approved" not in (refreshed.source_asof or {})


def _real_revalidate(**broker_fns):
    """Drive the REAL ``revalidate_and_submit`` with dispatch's window contract.

    ``dispatch_proposal`` threads ``window_evaluator``/``expected_policy_stamp``
    /``now_fn`` into the revalidator only when ``revalidate_fn is
    revalidate_and_submit`` -- an identity check no ``functools.partial`` can
    satisfy. A test that fakes the broker edges must therefore supply that
    contract itself; otherwise the first dispatch of a proposal dies at
    ``approval_window_policy_stamp_missing`` (the stamp is only persisted by a
    prior approval send) long before it reaches whatever is under test.
    """

    async def _run(*, service, proposal_id, now, eligibility_gate):
        group, _rungs = await service.get_proposal(proposal_id)
        stamp = (await allow_known_session(group, now=now)).policy_stamp
        return await revalidate_and_submit(
            service=service,
            proposal_id=proposal_id,
            now=now,
            eligibility_gate=eligibility_gate,
            window_evaluator=allow_known_session,
            expected_policy_stamp=stamp,
            now_fn=lambda: now,
            **broker_fns,
        )

    return _run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account_mode,market,symbol",
    [
        ("kis_live", "equity_kr", "005930"),
        ("upbit", "crypto", "KRW-AVAX"),
    ],
)
async def test_s141_auto_approved_cancel_executes_and_reports_as_cancelled(
    monkeypatch, db_session, account_mode, market, symbol
):
    """§141차 ③ — an eligible cancel goes through without a Telegram tap.

    Also pins the half of the change that is easy to miss: revalidation has
    already cancelled at the broker by the time ``dispatch_proposal`` decides
    whether the auto lane "completed". A cancel's terminal rung result is
    ``cancelled``, not ``submitted_*``. If that is not recognised, the operator
    gets an *approval card* asking them to authorise a cancel that already
    happened.
    """
    from app.core.config import settings
    from app.services.order_proposals.target_order import TargetOrderSnapshot

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE_MODE", "expanded")
    chat_id = f"chat-s141-cancel-{account_mode}-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )

    target_snapshot = TargetOrderSnapshot(
        broker_order_id="broker-s141-cancel",
        symbol=symbol,
        side="sell",
        order_type="limit",
        limit_price="42000",
        remaining_quantity="1",
        status="open",
        observed_at=datetime.now(UTC).isoformat(),
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market=market,
        account_mode=account_mode,
        side="sell",
        order_type="limit",
        proposer="p",
        thesis="ladder invalidated, pull the resting sell",
        broker_account_id=f"s141-cancel-{account_mode}-{uuid.uuid4()}",
        action="cancel",
        target_broker_order_id="broker-s141-cancel",
        target_order_snapshot=target_snapshot.to_payload(),
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("42000"), None)],
    )
    await db_session.commit()

    cancel_calls: list[dict] = []
    cancelled_snapshot = TargetOrderSnapshot(
        broker_order_id="broker-s141-cancel",
        symbol=symbol,
        side="sell",
        order_type="limit",
        limit_price="42000",
        remaining_quantity="1",
        status="cancelled",
        observed_at=datetime.now(UTC).isoformat(),
    )

    async def fetch_fn(**kwargs):
        return cancelled_snapshot if cancel_calls else target_snapshot

    async def cancel_fn(**kwargs):
        cancel_calls.append(kwargs)
        return {"success": True}

    async def place_fn(**kwargs):
        raise AssertionError("a cancel proposal must never place an order")

    notifier = _FakeNotifier(message_id=7710)
    result = await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=_real_revalidate(
            fetch_target_fn=fetch_fn,
            cancel_target_fn=cancel_fn,
            place_order_fn=place_fn,
        ),
        cancel_target_fn=cancel_fn,
        fetch_target_fn=fetch_fn,
    )

    assert result.ok is True
    assert len(cancel_calls) == 1
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "cancelled"
    assert "auto_approved" in (refreshed.source_asof or {})

    assert len(notifier.sent_messages) == 1
    text, keyboard, _chat = notifier.sent_messages[0]
    assert "자동 취소됨" in text
    assert "주문 제안 승인" not in text
    # No undo button: the retired order cannot be un-cancelled.
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    assert not any("callback_data" in button for button in buttons)


@pytest.mark.asyncio
async def test_s156_marketable_take_profit_replace_cancels_then_places_the_new_rung(
    monkeypatch, db_session
):
    """§156 — the objective marketable profit exception survives revalidation."""
    from app.core.config import settings
    from app.services.order_proposals.target_order import TargetOrderSnapshot

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE_MODE", "expanded")
    chat_id = f"chat-s141-replace-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )

    target_snapshot = TargetOrderSnapshot(
        broker_order_id="broker-s141-replace",
        symbol="005930",
        side="sell",
        order_type="limit",
        limit_price="42000",
        remaining_quantity="1",
        status="open",
        observed_at=datetime.now(UTC).isoformat(),
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        thesis="reprice the resting sell up one rung",
        broker_account_id=f"s141-replace-{uuid.uuid4()}",
        action="replace",
        target_broker_order_id="broker-s141-replace",
        target_order_snapshot=target_snapshot.to_payload(),
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("43000"), None)],
    )
    await db_session.commit()

    cancel_calls: list[dict] = []
    submits: list[dict] = []
    cancelled_snapshot = TargetOrderSnapshot(
        broker_order_id="broker-s141-replace",
        symbol="005930",
        side="sell",
        order_type="limit",
        limit_price="42000",
        remaining_quantity="1",
        status="cancelled",
        observed_at=datetime.now(UTC).isoformat(),
    )

    async def fetch_fn(**kwargs):
        return cancelled_snapshot if cancel_calls else target_snapshot

    async def cancel_fn(**kwargs):
        cancel_calls.append(kwargs)
        return {"success": True}

    async def place_fn(**kwargs):
        if kwargs.get("dry_run"):
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "43000",
                "quantity": "1",
                # Marketable sell, but §156 permits it only because this fresh
                # broker preview proves profit after the KR round trip.
                "current_price": "43000",
                "avg_buy_price": "38000",
            }
        assert cancel_calls, "the original must be cancelled before the replacement"
        submits.append(kwargs)
        return {
            "success": True,
            "broker_order_id": "broker-s141-new",
            "status": "resting",
        }

    notifier = _FakeNotifier(message_id=7711)
    result = await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=_real_revalidate(
            fetch_target_fn=fetch_fn,
            cancel_target_fn=cancel_fn,
            place_order_fn=place_fn,
        ),
        cancel_target_fn=cancel_fn,
        fetch_target_fn=fetch_fn,
    )

    assert result.ok is True
    assert len(cancel_calls) == 1
    assert len(submits) == 1
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state in {"acked", "resting"}
    assert "auto_approved" in (refreshed.source_asof or {})
    assert (
        refreshed.source_asof["auto_approved"]["eligibility"][0]["marketability"]
        == "marketable_profit_take"
    )

    text, keyboard, _chat = notifier.sent_messages[0]
    assert "자동 정정 접수됨" in text
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    assert any(
        button["text"] == "취소" and "callback_data" in button for button in buttons
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rung_quantity,rung_price,avg_buy_price,expected_reason",
    [
        # 100 * 43000 = 4,300,000 > KR per_order_cap 2,000,000
        (Decimal("100"), Decimal("43000"), "38000", "per_order_cap_exceeded"),
        # §156 permits a marketable sell only when it is a fee-netted profit.
        # Equal cost basis is inside the inclusive break-even band, so this
        # remains carded before either leg of the replace can touch a broker.
        (Decimal("1"), Decimal("41000"), "41000", "breakeven_band"),
    ],
)
async def test_s141_replace_failing_a_place_gate_never_touches_the_broker(
    monkeypatch, db_session, rung_quantity, rung_price, avg_buy_price, expected_reason
):
    """§141차 ② — every place gate still stands, and still stands *before* the
    two-leg broker mutation. A carded replace must leave the original resting.
    """
    from app.core.config import settings
    from app.services.order_proposals.target_order import TargetOrderSnapshot

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE_MODE", "expanded")
    chat_id = f"chat-s141-gate-{expected_reason}-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )

    target_snapshot = TargetOrderSnapshot(
        broker_order_id="broker-s141-gate",
        symbol="005930",
        side="sell",
        order_type="limit",
        limit_price="42000",
        remaining_quantity=str(rung_quantity),
        status="open",
        observed_at=datetime.now(UTC).isoformat(),
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        thesis="reprice the resting sell",
        broker_account_id=f"s141-gate-{expected_reason}-{uuid.uuid4()}",
        action="replace",
        target_broker_order_id="broker-s141-gate",
        target_order_snapshot=target_snapshot.to_payload(),
        rungs=[RungInput(0, "sell", rung_quantity, rung_price, None)],
    )
    await db_session.commit()

    async def fetch_fn(**kwargs):
        return target_snapshot

    async def cancel_fn(**kwargs):
        raise AssertionError("a carded replace must leave the original resting")

    async def place_fn(**kwargs):
        if kwargs.get("dry_run"):
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": str(rung_price),
                "quantity": str(rung_quantity),
                "current_price": "42000",
                "avg_buy_price": avg_buy_price,
            }
        raise AssertionError("a carded replace must never submit")

    notifier = _FakeNotifier(message_id=7712)
    result = await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=_real_revalidate(
            fetch_target_fn=fetch_fn,
            cancel_target_fn=cancel_fn,
            place_order_fn=place_fn,
        ),
    )

    assert result.ok is True
    text, _keyboard, _chat = notifier.sent_messages[0]
    assert "주문 제안 승인" in text
    assert "자동" not in text.splitlines()[0]

    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "pending_approval"
    assert "auto_approved" not in (refreshed.source_asof or {})
    evidence = refreshed.source_asof["auto_approve_rejections"][-1]["rungs"][0]
    assert evidence["reason_code"] == expected_reason


@pytest.mark.asyncio
async def test_s141_cancel_of_an_order_this_account_cannot_read_is_not_auto_approved(
    monkeypatch, db_session
):
    """§141차 ③ — ownership is proven by the broker read, not by the proposal.

    ``fetch_target_order`` looks the id up in *this account's* order history and
    raises when it is not there uniquely. That happens before the eligibility
    gate, so a cancel aimed at an order this account cannot read never reaches
    ``BROKER_CANCEL`` and never gets auto-approved.
    """
    from app.core.config import settings
    from app.services.order_proposals.errors import OrderProposalError
    from app.services.order_proposals.target_order import TargetOrderSnapshot

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE_MODE", "expanded")
    chat_id = f"chat-s141-foreign-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )

    target_snapshot = TargetOrderSnapshot(
        broker_order_id="broker-not-ours",
        symbol="005930",
        side="sell",
        order_type="limit",
        limit_price="42000",
        remaining_quantity="1",
        status="open",
        observed_at=datetime.now(UTC).isoformat(),
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        thesis="pull a sell that is not on this account's book",
        broker_account_id=f"s141-foreign-{uuid.uuid4()}",
        action="cancel",
        target_broker_order_id="broker-not-ours",
        target_order_snapshot=target_snapshot.to_payload(),
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("42000"), None)],
    )
    await db_session.commit()

    async def fetch_fn(**kwargs):
        raise OrderProposalError("target broker order not found uniquely")

    async def cancel_fn(**kwargs):
        raise AssertionError("never cancel an order this account cannot read")

    async def place_fn(**kwargs):
        raise AssertionError("a cancel proposal must never place an order")

    notifier = _FakeNotifier(message_id=7713)
    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=_real_revalidate(
            fetch_target_fn=fetch_fn,
            cancel_target_fn=cancel_fn,
            place_order_fn=place_fn,
        ),
    )

    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "pending_approval"
    assert "auto_approved" not in (refreshed.source_asof or {})
    text, _keyboard, _chat = notifier.sent_messages[0]
    assert "주문 제안 승인" in text


@pytest.mark.asyncio
async def test_s141_auto_approved_cancel_consumes_no_daily_budget(
    monkeypatch, db_session
):
    """§141차 ④ — the daily cap meters exposure taken on, and a cancel takes none.

    A cancel proposal's rung mirrors the *target* order's price and quantity, so
    a naive sum over auto-approved rungs would charge the cap twice for the same
    order: once when it was placed, again when it was pulled.
    """
    from app.services.order_proposals.target_order import TargetOrderSnapshot

    service = OrderProposalsService(db_session)
    account = f"s141-budget-{uuid.uuid4()}"
    now = datetime.now(UTC)
    target_snapshot = TargetOrderSnapshot(
        broker_order_id="broker-s141-budget",
        symbol="005930",
        side="sell",
        order_type="limit",
        limit_price="42000",
        remaining_quantity="10",
        status="open",
        observed_at=now.isoformat(),
    )
    cancel_group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        thesis="pull the resting sell",
        broker_account_id=account,
        action="cancel",
        target_broker_order_id="broker-s141-budget",
        target_order_snapshot=target_snapshot.to_payload(),
        rungs=[RungInput(0, "sell", Decimal("10"), Decimal("42000"), None)],
    )
    place_group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        thesis="ladder entry",
        broker_account_id=account,
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("40000"), None)],
    )
    for group in (cancel_group, place_group):
        await service.record_auto_approval(
            group.proposal_id,
            policy_version="test-policy",
            policy_content_hash=None,
            eligibility=[],
            outcomes=["submitted_resting"],
            now=now,
            evaluated_at=now,
        )
    await db_session.commit()

    refreshed, _rungs = await service.get_proposal(place_group.proposal_id)
    consumed = await service.auto_approved_daily_notional(refreshed, now=now)

    # Only the `place` group's 1 x 40,000 counts; the cancel's 10 x 42,000 does not.
    assert consumed == Decimal("40000")


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
        thesis="notification delivery compensation fixture",
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

    assert result.ok is False
    assert cancel_calls[0]["order_id"] == "broker-notify-failure"
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "cancelled"
    assert (
        refreshed.source_asof["auto_approved"]["notification_failure"]["outcomes"][0][
            "result"
        ]
        == "cancelled"
    )
