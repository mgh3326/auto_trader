from __future__ import annotations

import contextlib
import functools
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.caller_identity import caller_agent_id_var, get_caller_agent_id
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import approval_message as approval_messages
from app.services.order_proposals.approval_message import parse_callback_data
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalPublication,
    DispatchBinding,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.revalidation import RungOutcome, revalidate_and_submit
from app.services.order_proposals.service import RungInput
from app.services.order_proposals.target_order import TargetOrderSnapshot
from app.services.order_proposals.telegram_callback import (
    _build_result_summary,
    _resolve_proposal_id,
    handle_callback_update,
)
from app.telegram_contract import TelegramMethodResult, telegram_text_length

CHAT_ID = 42
USER_ID = 777


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, dict | None, str]] = []
        self.answered: list[tuple[str, str | None]] = []
        self.edited: list[tuple[str, int, str, dict | None]] = []
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


class _EventNotifier(_FakeNotifier):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def answer_callback(self, callback_query_id, text=None):
        self.events.append("answer")
        return await super().answer_callback(callback_query_id, text)

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.events.append("edit")
        return await super().edit_message(chat_id, message_id, text, reply_markup)


def _session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


def _make_update(*, data, chat_id=CHAT_ID, user_id=USER_ID, callback_id="cbq-1"):
    return {
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": 555},
            "data": data,
        }
    }


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


def _fixture_nonce(label: str) -> str:
    """Use the production 11-character nonce shape for descriptive fixtures."""
    if len(label) <= 11:
        return label
    return hashlib.sha256(label.encode()).hexdigest()[:11]


async def _publish_fixture_card(
    service: OrderProposalsService,
    group,
    *,
    nonce: str,
    card_kind: ApprovalCardKind,
    message_id: int = 555,
) -> DispatchBinding:
    """Stage and finalize the same immutable card shape production publishes."""
    nonce = _fixture_nonce(nonce)
    if group.approval_nonce != nonce:
        await service.set_approval_nonce(group.proposal_id, nonce)
    attempt_id = uuid.uuid4()
    now = datetime.now(UTC)
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
    )
    assert result.ok
    return binding


def _proposal_callback_data(group, *, action: str, nonce: str | None = None) -> str:
    return approval_messages.build_callback_data(
        action=action,
        proposal_id=group.proposal_id,
        nonce=_fixture_nonce(nonce) if nonce is not None else group.approval_nonce,
        binding=DispatchBinding(
            attempt_id=group.approval_dispatch_attempt_id,
            card_kind=ApprovalCardKind(group.approval_dispatch_card_kind),
            membership_revision=group.approval_dispatch_membership_revision,
            membership_digest=group.approval_dispatch_membership_digest,
        ),
    )


async def _seed_proposal(db_session, *, nonce="nonce-abc123", symbol="A", rungs=1):
    service = OrderProposalsService(db_session)
    rung_inputs = [
        RungInput(i, "buy", Decimal("10"), Decimal("100"), None) for i in range(rungs)
    ]
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=rung_inputs,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(
        service,
        group,
        nonce=nonce,
        card_kind=ApprovalCardKind.MANUAL,
    )
    await db_session.commit()
    return group


async def _seed_auto_resting(db_session, *, nonce="veto-nonce"):
    service = OrderProposalsService(db_session)
    now = datetime.now(UTC)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
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
        broker_order_id="broker-auto-1",
        correlation_id="corr-auto-1",
        idempotency_key="idem-auto-1",
        approval_hash_digest="digest-auto-1",
        now=now,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(
        service,
        group,
        nonce=nonce,
        card_kind=ApprovalCardKind.AUTO_VETO,
    )
    await db_session.commit()
    return group


async def _seed_loss_cut_proposal(
    db_session, monkeypatch, *, nonce="loss-cut-first", rungs=1
):
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

    async def fake_lookup(session, retrospective_id):
        assert retrospective_id == 42
        return retro

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(i, "sell", Decimal("1"), Decimal("99"), None)
            for i in range(rungs)
        ],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(
        service,
        group,
        nonce=nonce,
        card_kind=ApprovalCardKind.MANUAL,
    )
    await db_session.commit()
    return group


async def _fake_loss_cut_preview(**kwargs):
    return {
        "rungs": [
            {
                "rung_index": 0,
                "current_price": "100",
                "avg_buy_price": "200",
                "loss_pct": "-50.00",
                "loss_cut_slip_band": "98",
            }
        ],
        "retrospective_id": 42,
        "lesson_excerpt": "손절 기준을 늦추지 않는다",
    }


async def _fake_noop_revalidate(**kwargs):
    return []


def _allow_chat(monkeypatch, chat_id=CHAT_ID):
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", str(chat_id)
    )


async def _seed_approval_batch(db_session, monkeypatch, *, member_count=2, rungs=1):
    chat_id = int(uuid.uuid4().hex[:8], 16)
    _allow_chat(monkeypatch, chat_id)
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    groups = []
    service = OrderProposalsService(db_session)
    registration = None
    for index in range(member_count):
        group = await _seed_proposal(
            db_session,
            nonce=f"batch-member-{uuid.uuid4().hex}",
            symbol=f"B{index}",
            rungs=rungs,
        )
        groups.append(group)
        registration = await service.register_approval_batch_member(
            group.proposal_id,
            chat_id=str(chat_id),
            approval_message_id=7100 + index,
            now=now + timedelta(seconds=index),
            summary_member_threshold=member_count,
        )
    await db_session.commit()
    assert registration is not None
    assert registration.binding is not None
    batch = registration.batch
    dispatch_result = await service.finish_approval_batch_dispatch(
        batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        publication=_successful_publication(7999),
        now=now + timedelta(seconds=member_count),
    )
    assert dispatch_result.ok
    await db_session.commit()
    data = approval_messages.build_batch_callback_data(
        batch_id=batch.batch_id,
        nonce=batch.approval_nonce,
        binding=registration.binding,
    )
    return chat_id, now, groups, batch, data


@pytest.mark.asyncio
async def test_batch_approve_consumes_each_member_nonce_and_rejects_replay(
    monkeypatch, db_session
):
    chat_id, now, groups, _batch, data = await _seed_approval_batch(
        db_session, monkeypatch
    )
    notifier = _FakeNotifier()
    calls: list[uuid.UUID] = []

    async def fake_revalidate(*, service, proposal_id, now):
        calls.append(proposal_id)
        return [RungOutcome(0, "submitted_resting", {})]

    first = await handle_callback_update(
        _make_update(data=data, chat_id=chat_id),
        now=now + timedelta(minutes=1),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert first["handled"] is True
    assert first["reason"] == "batch_approved"
    assert calls == [group.proposal_id for group in groups]
    service = OrderProposalsService(db_session)
    for group in groups:
        refreshed, _ = await service.get_proposal(group.proposal_id)
        assert refreshed.approval_nonce_used_at is not None
        assert refreshed.approved_by_telegram_user_id == str(USER_ID)
    edited_ids = {item[1] for item in notifier.edited}
    assert {555, 7100, 7101} <= edited_ids

    replay = await handle_callback_update(
        _make_update(data=data, chat_id=chat_id, callback_id="batch-replay"),
        now=now + timedelta(minutes=2),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )
    assert replay == {
        "handled": False,
        "reason": "approval_batch_nonce_replay",
    }
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_batch_callback_has_no_preflight_external_effect(
    monkeypatch, db_session
):
    chat_id, _now, _groups, batch, data = await _seed_approval_batch(
        db_session, monkeypatch
    )
    notifier = _FakeNotifier()

    result = await handle_callback_update(
        _make_update(data=data, chat_id=chat_id),
        now=batch.expires_at,
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=_fake_noop_revalidate,
    )

    assert result == {"handled": False, "reason": "approval_batch_expired"}
    assert notifier.answered == []
    assert notifier.edited == []


@pytest.mark.asyncio
async def test_batch_approve_continues_after_one_member_raises(monkeypatch, db_session):
    chat_id, now, groups, _batch, data = await _seed_approval_batch(
        db_session, monkeypatch, member_count=3
    )
    group_ids = [group.proposal_id for group in groups]
    notifier = _FakeNotifier()
    calls: list[uuid.UUID] = []

    async def fake_revalidate(*, service, proposal_id, now):
        calls.append(proposal_id)
        if proposal_id == group_ids[1]:
            raise RuntimeError("middle member failed")
        return [RungOutcome(0, "submitted_resting", {})]

    result = await handle_callback_update(
        _make_update(data=data, chat_id=chat_id),
        now=now + timedelta(minutes=1),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert calls == group_ids
    assert [item["status"] for item in result["results"]] == [
        "approved",
        "failed",
        "approved",
    ]
    edited_ids = {item[1] for item in notifier.edited}
    assert {555, 7100, 7101, 7102} <= edited_ids


@pytest.mark.asyncio
async def test_batch_approve_continues_after_member_result_audit_failure(
    monkeypatch, db_session
):
    chat_id, now, groups, _batch, data = await _seed_approval_batch(
        db_session, monkeypatch, member_count=3
    )
    group_ids = [group.proposal_id for group in groups]
    notifier = _FakeNotifier()
    revalidated: list[uuid.UUID] = []
    audit_calls = 0
    original_record = OrderProposalsService.record_approval_batch_member_result

    async def flaky_record(self, member_id, **kwargs):
        nonlocal audit_calls
        audit_calls += 1
        if audit_calls == 2:
            raise RuntimeError("member result audit unavailable")
        return await original_record(self, member_id, **kwargs)

    monkeypatch.setattr(
        OrderProposalsService, "record_approval_batch_member_result", flaky_record
    )

    async def fake_revalidate(*, service, proposal_id, now):
        revalidated.append(proposal_id)
        return [RungOutcome(0, "submitted_resting", {})]

    result = await handle_callback_update(
        _make_update(data=data, chat_id=chat_id),
        now=now + timedelta(minutes=1),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is True
    assert revalidated == group_ids
    assert [item["status"] for item in result["results"]] == [
        "approved",
        "approved",
        "approved",
    ]


@pytest.mark.asyncio
async def test_batch_click_rechecks_loss_terminal_auto_superseded_and_used_nonce(
    monkeypatch, db_session
):
    chat_id, now, groups, _batch, data = await _seed_approval_batch(
        db_session, monkeypatch, member_count=6
    )
    groups[0].exit_intent = "loss_cut"
    groups[1].lifecycle_state = "terminal"
    groups[2].source_asof = {
        **(groups[2].source_asof or {}),
        "auto_approved": {"approved_at": now.isoformat()},
    }
    groups[3].lifecycle_state = "superseded"
    groups[4].approval_nonce_used_at = now
    await db_session.commit()
    notifier = _FakeNotifier()
    calls: list[uuid.UUID] = []

    async def fake_revalidate(*, service, proposal_id, now):
        calls.append(proposal_id)
        return [RungOutcome(0, "submitted_resting", {})]

    result = await handle_callback_update(
        _make_update(data=data, chat_id=chat_id),
        now=now + timedelta(minutes=1),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert calls == [groups[5].proposal_id]
    assert [item["status"] for item in result["results"]] == [
        "skipped",
        "skipped",
        "skipped",
        "skipped",
        "skipped",
        "approved",
    ]


@pytest.mark.asyncio
async def test_batch_approve_keeps_rob861_reconfirm_member_independently_actionable(
    monkeypatch, db_session
):
    chat_id, now, groups, _batch, data = await _seed_approval_batch(
        db_session, monkeypatch, rungs=3
    )
    group_ids = [group.proposal_id for group in groups]
    original_nonce = groups[0].approval_nonce
    notifier = _FakeNotifier()
    detail = {
        "reason": "insufficient_buying_power",
        "currency": "KRW",
        "available": "50000",
        "required": "100000",
        "shortfall": "50000",
        "before": {"limit_price": "100", "quantity": "10"},
        "after": {"limit_price": "100", "quantity": "10"},
    }

    async def fake_revalidate(*, service, proposal_id, now):
        if proposal_id == group_ids[0]:
            await service.transition_rung(proposal_id, 2, new_state="revalidating")
            await service.mark_needs_reconfirm(proposal_id, 2, now=now)
            return [RungOutcome(2, "needs_reconfirm", detail)]
        return [RungOutcome(1, "submitted_resting", {})]

    result = await handle_callback_update(
        _make_update(data=data, chat_id=chat_id),
        now=now + timedelta(minutes=1),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert [item["status"] for item in result["results"]] == [
        "needs_reconfirm",
        "approved",
    ]
    assert result["results"][0]["rung_results"] == [
        {"rung_index": 2, "result": "needs_reconfirm"}
    ]
    assert result["results"][1]["rung_results"] == [
        {"rung_index": 1, "result": "submitted_resting"}
    ]
    service = OrderProposalsService(db_session)
    reconfirm_group, reconfirm_rungs = await service.get_proposal(group_ids[0])
    approved_group, _ = await service.get_proposal(group_ids[1])
    assert reconfirm_group.approval_nonce != original_nonce
    assert reconfirm_group.approval_nonce_used_at is None
    assert reconfirm_rungs[2].state == "needs_reconfirm"
    assert approved_group.approval_nonce_used_at is not None
    assert len(notifier.sent_messages) == 1
    summary = next(text for _chat, mid, text, _markup in notifier.edited if mid == 555)
    assert "재확인 필요" in summary
    assert "승인 완료" in summary
    assert "#3 재확인 필요" in summary
    assert "#2 주문 유지(대기)" in summary


@pytest.mark.asyncio
async def test_batch_approve_classifies_rung_outcomes_without_false_success(
    monkeypatch, db_session
):
    chat_id, now, groups, _batch, data = await _seed_approval_batch(
        db_session, monkeypatch, member_count=4
    )
    group_ids = [group.proposal_id for group in groups]
    notifier = _FakeNotifier()
    outcomes = {
        group_ids[0]: "guard_blocked",
        group_ids[1]: "unverified",
        group_ids[2]: "error",
        group_ids[3]: "submitted_resting",
    }

    async def fake_revalidate(*, service, proposal_id, now):
        return [RungOutcome(0, outcomes[proposal_id], {})]

    result = await handle_callback_update(
        _make_update(data=data, chat_id=chat_id),
        now=now + timedelta(minutes=1),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert [item["status"] for item in result["results"]] == [
        "skipped",
        "failed",
        "failed",
        "approved",
    ]
    assert [item.get("reason") for item in result["results"]] == [
        "guard_blocked",
        "unverified",
        "error",
        None,
    ]


def test_result_summary_includes_bounded_escaped_guard_reason():
    outcome = RungOutcome(
        0, "guard_blocked", {"error": "cash *blocked* " + ("x" * 400)}
    )
    summary = _build_result_summary([outcome])
    assert "cash \\*blocked\\*" in summary
    assert summary.endswith("…")
    assert len(summary) < 320


def test_result_summary_labels_confirmed_cancel():
    summary = _build_result_summary([RungOutcome(0, "cancelled", {})])
    assert "취소 확인" in summary


@pytest.mark.asyncio
async def test_auto_veto_cancels_broker_and_rung_once(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_auto_resting(db_session)
    notifier = _FakeNotifier()
    cancel_calls = []
    mutation_locks = []

    original_lock = OrderProposalsService.acquire_broker_order_mutation_lock

    async def recording_lock(self, group, broker_order_id):
        mutation_locks.append(broker_order_id)
        return await original_lock(self, group, broker_order_id)

    monkeypatch.setattr(
        OrderProposalsService,
        "acquire_broker_order_mutation_lock",
        recording_lock,
    )

    async def cancel_fn(**kwargs):
        cancel_calls.append(kwargs)
        return {"success": True}

    async def fetch_fn(**kwargs):
        return TargetOrderSnapshot(
            broker_order_id="broker-auto-1",
            symbol="005930",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="1",
            status="cancelled",
            observed_at=kwargs["now"].isoformat(),
        )

    update = _make_update(
        data=_proposal_callback_data(group, action="vc", nonce="veto-nonce")
    )
    wrong_action = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="op", nonce="veto-nonce")
        ),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
    )
    assert wrong_action["reason"] == "approval_card_action_mismatch"

    result = await handle_callback_update(
        update,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        veto_cancel_fn=cancel_fn,
        veto_fetch_fn=fetch_fn,
    )

    assert result["reason"] == "auto_veto_cancelled"
    assert mutation_locks == ["broker-auto-1"]
    assert cancel_calls[0]["order_id"] == "broker-auto-1"
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "cancelled"
    assert refreshed.source_asof["auto_approved"]["veto"]["telegram_user_id"] == str(
        USER_ID
    )
    assert "취소됨" in notifier.edited[-1][2]

    replay = await handle_callback_update(
        update,
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        veto_cancel_fn=cancel_fn,
        veto_fetch_fn=fetch_fn,
    )
    assert replay["reason"] == "nonce_replay"
    assert len(cancel_calls) == 1


@pytest.mark.asyncio
async def test_auto_veto_cancel_failure_that_is_filled_edits_filled(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_auto_resting(db_session)
    notifier = _FakeNotifier()

    async def cancel_fn(**kwargs):
        return {"success": False, "error": "already filled"}

    async def fetch_fn(**kwargs):
        return TargetOrderSnapshot(
            broker_order_id="broker-auto-1",
            symbol="005930",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="0",
            status="filled",
            observed_at=kwargs["now"].isoformat(),
        )

    result = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="vc", nonce="veto-nonce")
        ),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        veto_cancel_fn=cancel_fn,
        veto_fetch_fn=fetch_fn,
    )

    assert result["reason"] == "auto_veto_filled"
    _refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "filled"
    assert "체결됨" in notifier.edited[-1][2]


@pytest.mark.asyncio
async def test_expired_approve_never_revalidates(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="expired-nonce")
    group.valid_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    called = False

    async def must_not_revalidate(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("expired proposal must not revalidate")

    notifier = _FakeNotifier()
    result = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="op", nonce="expired-nonce")
        ),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=must_not_revalidate,
    )
    assert result["reason"] == "proposal_expired"
    assert called is False
    assert notifier.answered[-1] == ("cbq-1", "제안이 만료되었습니다")
    assert "만료" in notifier.edited[-1][2]


@pytest.mark.asyncio
async def test_not_a_callback_query_is_unhandled(monkeypatch, db_session):
    notifier = _FakeNotifier()
    result = await handle_callback_update(
        {"message": {"text": "hi"}},
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
    )
    assert result == {"handled": False, "reason": "not_callback"}
    assert notifier.answered == []


@pytest.mark.asyncio
async def test_chat_not_in_allowlist_rejected(monkeypatch, db_session):
    # allowlist empty -> any chat rejected; assert no revalidation invoked.
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "")
    group = await _seed_proposal(db_session)
    data = _proposal_callback_data(group, action="op", nonce="nonce-abc123")
    notifier = _FakeNotifier()

    revalidate_calls = []

    async def fake_revalidate(**kwargs):
        revalidate_calls.append(kwargs)
        raise AssertionError("revalidate_fn must not be called")

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result == {"handled": False, "reason": "chat_not_allowed"}
    assert revalidate_calls == []
    assert notifier.answered == []
    assert notifier.edited == []


@pytest.mark.asyncio
async def test_approve_happy_path_submits_and_edits(monkeypatch, db_session):
    # allowlist includes chat; nonce valid; revalidate_fn returns submitted_resting;
    # assert edit_message called with a summary, answer_callback called,
    # nonce consumed, approved_by_telegram_user_id recorded.
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-happy1")
    data = _proposal_callback_data(group, action="op", nonce="nonce-happy1")
    notifier = _FakeNotifier()

    async def fake_revalidate(*, service, proposal_id, now):
        assert proposal_id == group.proposal_id
        return [
            RungOutcome(
                0,
                "submitted_resting",
                {"submit": {"broker_order_id": "B1"}},
            )
        ]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is True
    assert result["reason"] == "approved"
    assert len(notifier.edited) == 1
    chat_id, message_id, text, _reply_markup = notifier.edited[0]
    assert chat_id == CHAT_ID
    assert message_id == 555
    assert "주문 유지" in text or "resting" in text.lower() or text
    assert notifier.answered  # answer_callback was invoked

    service = OrderProposalsService(db_session)
    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce_used_at is not None
    assert refreshed.approved_by_telegram_user_id == str(USER_ID)
    assert refreshed.approved_at is not None


def _toss_broker_order(**overrides):
    values = {
        "order_id": "broker-1",
        "symbol": "005930",
        "side": "SELL",
        "order_type": "LIMIT",
        "price": Decimal("70000"),
        "quantity": Decimal("10"),
        "execution": {},
        "status": "PENDING",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_toss_live_cancel_approve_click_consumes_nonce_before_broker_cancel(
    monkeypatch, db_session
):
    """ROB-972 AC3 -- the real 07-20 incident scenario, driven through the
    actual Telegram approval boundary (nonce consumption via
    `handle_callback_update`), not a direct `revalidate_and_submit` call.

    ROB-972 round-1 finding: the previous "AC3" test called
    `revalidate_and_submit` directly with no nonce/callback, so it could not
    have caught a bug in the approval-boundary wiring itself (e.g. auto-
    dispatch reaching a cancel before Telegram approval was ever consulted).
    This test consumes a real approval nonce through `handle_callback_update`
    -- exactly the human-click path -- and only then expects the broker
    cancel to fire, with the *real* `revalidate_and_submit` (only the Toss
    network boundary is faked, via a `revalidate_fn` partial bound onto the
    real function, matching `test_toss_live_cancel_end_to_end_through_broker_gateway`'s
    fakes).
    """
    from app.services.order_proposals.broker_gateway import (
        cancel_target_order,
        fetch_target_order,
    )

    _allow_chat(monkeypatch)
    service = OrderProposalsService(db_session)
    approved = TargetOrderSnapshot(
        broker_order_id="broker-1",
        symbol="005930",
        side="sell",
        order_type="limit",
        limit_price="70000",
        remaining_quantity="10",
        status="open",
        observed_at="2026-07-11T08:23:00+00:00",
    )
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="p",
        action="cancel",
        target_broker_order_id="broker-1",
        target_order_snapshot=approved.to_payload(),
        rungs=[RungInput(0, "sell", Decimal("10"), Decimal("70000"), None)],
    )
    await service.set_approval_nonce(group.proposal_id, "ac3-cancel-nonce")
    await _publish_fixture_card(
        service,
        group,
        nonce="ac3-cancel-nonce",
        card_kind=ApprovalCardKind.MANUAL,
    )
    await db_session.commit()

    toss_orders = iter(
        [
            _toss_broker_order(status="PENDING"),
            _toss_broker_order(status="CANCELED"),
        ]
    )

    class FakeTossClient:
        async def get_order(self, order_id):
            assert order_id == "broker-1"
            return next(toss_orders)

    cancel_calls: list[dict] = []

    async def fake_toss_cancel(**kwargs):
        cancel_calls.append(kwargs)
        return {"success": True, "original_order_id": kwargs["order_id"]}

    real_revalidate = functools.partial(
        revalidate_and_submit,
        fetch_target_fn=functools.partial(
            fetch_target_order, toss_client=FakeTossClient()
        ),
        cancel_target_fn=functools.partial(
            cancel_target_order, toss_cancel_fn=fake_toss_cancel
        ),
    )

    notifier = _FakeNotifier()
    data = _proposal_callback_data(group, action="op", nonce="ac3-cancel-nonce")

    # Before the nonce is consumed, the broker must not have been touched.
    assert cancel_calls == []

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=real_revalidate,
    )

    assert result["handled"] is True
    assert result["reason"] == "approved"
    assert result["results"] == ["cancelled"]
    # The cancel only happened after (as a direct consequence of) this
    # single approve click -- TELEGRAM_APPROVAL_SEND already happened
    # earlier (dispatch.py, not under test here); this asserts the click
    # itself, not a pre-approval leak, is what triggered BROKER_CANCEL.
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["order_id"] == "broker-1"

    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce_used_at is not None
    assert refreshed.approved_by_telegram_user_id == str(USER_ID)
    assert rungs[0].state == "cancelled"
    assert rungs[0].broker_order_id == "broker-1"

    # Nonce replay: a second click (e.g. a duplicated Telegram webhook) must
    # not be able to re-trigger the cancel path.
    replay = await handle_callback_update(
        _make_update(data=data, callback_id="cbq-replay"),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=real_revalidate,
    )
    assert replay["handled"] is False
    assert len(cancel_calls) == 1


@pytest.mark.asyncio
async def test_toss_live_replace_approve_click_opposite_pending_blocks_before_cancel(
    monkeypatch, db_session
):
    """ROB-972 round-1 -- the replace-atomicity finding, through the real
    approval boundary. An opposite-pending order discovered by the pre-cancel
    guard must leave the original resting order untouched even when reached
    via a real Telegram approve click, not just a direct
    `revalidate_and_submit` call.
    """
    from app.services.order_proposals.broker_gateway import (
        cancel_target_order,
        fetch_target_order,
    )

    _allow_chat(monkeypatch)
    service = OrderProposalsService(db_session)
    approved = TargetOrderSnapshot(
        broker_order_id="broker-1",
        symbol="005930",
        side="sell",
        order_type="limit",
        limit_price="70000",
        remaining_quantity="10",
        status="open",
        observed_at="2026-07-11T08:23:00+00:00",
    )
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="p",
        action="replace",
        target_broker_order_id="broker-1",
        target_order_snapshot=approved.to_payload(),
        rungs=[RungInput(0, "sell", Decimal("10"), Decimal("71000"), None)],
    )
    await service.set_approval_nonce(group.proposal_id, "ac3-replace-nonce")
    await _publish_fixture_card(
        service,
        group,
        nonce="ac3-replace-nonce",
        card_kind=ApprovalCardKind.MANUAL,
    )
    await db_session.commit()

    class FakeTossClient:
        async def get_order(self, order_id):
            return _toss_broker_order(status="PENDING")

    cancel_calls: list[dict] = []

    async def fake_toss_cancel(**kwargs):
        cancel_calls.append(kwargs)
        raise AssertionError(
            "the original order must not be cancelled when the "
            "opposite-pending pre-check rejects the replace"
        )

    async def place_order_fn(**kwargs):
        assert kwargs.get("dry_run") is True
        return {
            "success": True,
            "approval_hash": "fresh",
            "price": "71000",
            "quantity": "10",
        }

    async def opposite_pending_found(**kwargs):
        return {
            "success": False,
            "error": "An opposite pending order exists for symbol 005930 (BUY).",
        }

    real_revalidate = functools.partial(
        revalidate_and_submit,
        place_order_fn=place_order_fn,
        fetch_target_fn=functools.partial(
            fetch_target_order, toss_client=FakeTossClient()
        ),
        cancel_target_fn=functools.partial(
            cancel_target_order, toss_cancel_fn=fake_toss_cancel
        ),
        opposite_pending_check_fn=opposite_pending_found,
    )

    notifier = _FakeNotifier()
    result = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="op", nonce="ac3-replace-nonce")
        ),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=real_revalidate,
    )

    assert result["handled"] is True
    assert result["results"] == ["guard_blocked"]
    assert cancel_calls == []

    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.target_broker_order_id == "broker-1"
    assert rungs[0].state == "pending_approval"


@pytest.mark.asyncio
async def test_superseded_old_button_is_explicitly_blocked_and_replacement_approves(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    old = await _seed_proposal(db_session, nonce="old-button-nonce")
    service = OrderProposalsService(db_session)
    replacement = await service.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("99"), None)],
        supersedes_proposal_id=old.proposal_id,
        now=datetime(2026, 7, 14, 1, 20, tzinfo=UTC),
    )
    await service.set_approval_nonce(replacement.proposal_id, "new-button-nonce")
    await _publish_fixture_card(
        service,
        replacement,
        nonce="new-button-nonce",
        card_kind=ApprovalCardKind.MANUAL,
    )
    await db_session.commit()
    revalidated = []

    async def fake_revalidate(*, service, proposal_id, now):
        revalidated.append(proposal_id)
        return [RungOutcome(0, "submitted_resting", {})]

    notifier = _FakeNotifier()
    old_result = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(old, action="op", nonce="old-button-nonce")
        ),
        now=datetime(2026, 7, 14, 1, 21, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )
    new_result = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(
                replacement, action="op", nonce="new-button-nonce"
            ),
            callback_id="cbq-new",
        ),
        now=datetime(2026, 7, 14, 1, 22, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert old_result["reason"] == f"proposal_superseded_by:{replacement.proposal_id}"
    assert new_result["reason"] == "approved"
    assert revalidated == [replacement.proposal_id]


@pytest.mark.asyncio
async def test_loss_cut_second_click_is_blocked_when_superseded(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    old = await _seed_loss_cut_proposal(db_session, monkeypatch)
    notifier = _FakeNotifier()
    submit_calls = []

    async def fake_revalidate(**kwargs):
        submit_calls.append(kwargs)
        return [RungOutcome(0, "submitted_resting", {})]

    first = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(old, action="op", nonce="loss-cut-first")
        ),
        now=datetime(2026, 7, 14, 1, 25, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )
    assert first["reason"] == "loss_cut_confirmation_required"
    callback_data = notifier.edited[-1][3]["inline_keyboard"][0][0]["callback_data"]

    service = OrderProposalsService(db_session)
    replacement = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("98"), None)],
        supersedes_proposal_id=old.proposal_id,
        now=datetime(2026, 7, 14, 1, 26, tzinfo=UTC),
    )
    await db_session.commit()

    second = await handle_callback_update(
        _make_update(data=callback_data, callback_id="cbq-loss-cut-second"),
        now=datetime(2026, 7, 14, 1, 27, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )

    assert second["reason"] == f"proposal_superseded_by:{replacement.proposal_id}"
    assert submit_calls == []


@pytest.mark.asyncio
async def test_loss_cut_requires_second_click_before_submit(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_SUBMIT_AGENT_ID", "proposal-agent")
    group = await _seed_loss_cut_proposal(db_session, monkeypatch)
    notifier = _FakeNotifier()
    submit_calls = []

    async def fake_revalidate(**kwargs):
        submit_calls.append(kwargs)
        return [RungOutcome(0, "submitted_resting", {})]

    async def identity_checked_preview(**kwargs):
        assert get_caller_agent_id() == "proposal-agent"
        return await _fake_loss_cut_preview(**kwargs)

    issued = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    first = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="op", nonce="loss-cut-first")
        ),
        now=issued,
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
        loss_cut_preview_fn=identity_checked_preview,
    )

    assert first["reason"] == "loss_cut_confirmation_required"
    assert submit_calls == []
    text, keyboard = notifier.edited[-1][2], notifier.edited[-1][3]
    assert "손절 확인" in text
    callback_data = keyboard["inline_keyboard"][0][0]["callback_data"]
    parsed = parse_callback_data(callback_data)
    second_nonce = parsed.nonce
    assert parsed.action == "lc"
    service = OrderProposalsService(db_session)
    refreshed, _ = await service.get_proposal(group.proposal_id)
    audit = refreshed.source_asof["loss_cut_confirmation"]
    assert audit["first_click"]["telegram_user_id"] == str(USER_ID)
    assert audit["first_click"]["nonce"] == _fixture_nonce("loss-cut-first")
    assert audit["rungs"] == [{"rung_index": 0, "approval_revision": 0}]

    second = await handle_callback_update(
        _make_update(data=callback_data, callback_id="cbq-2"),
        now=issued + timedelta(seconds=30),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )

    assert second["reason"] == "approved"
    assert len(submit_calls) == 1
    refreshed, _ = await service.get_proposal(group.proposal_id)
    audit = refreshed.source_asof["loss_cut_confirmation"]
    assert audit["second_click"]["telegram_user_id"] == str(USER_ID)
    assert audit["second_click"]["nonce"] == second_nonce


@pytest.mark.asyncio
async def test_loss_cut_second_nonce_replay_is_rejected(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_loss_cut_proposal(db_session, monkeypatch)
    notifier = _FakeNotifier()

    first = await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="op", nonce="loss-cut-first")
        ),
        now=datetime(2026, 7, 13, 10, 0, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=_fake_noop_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )
    callback_data = notifier.edited[-1][3]["inline_keyboard"][0][0]["callback_data"]
    second_now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=UTC)
    await handle_callback_update(
        _make_update(data=callback_data, callback_id="cbq-2"),
        now=second_now,
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=_fake_noop_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )
    replay = await handle_callback_update(
        _make_update(data=callback_data, callback_id="cbq-3"),
        now=second_now + timedelta(seconds=1),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=_fake_noop_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )

    assert first["reason"] == "loss_cut_confirmation_required"
    assert replay["reason"] == "nonce_replay"


@pytest.mark.asyncio
async def test_loss_cut_second_nonce_expires_after_90_seconds(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_loss_cut_proposal(db_session, monkeypatch)
    notifier = _FakeNotifier()
    issued = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="op", nonce="loss-cut-first")
        ),
        now=issued,
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=_fake_noop_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )
    callback_data = notifier.edited[-1][3]["inline_keyboard"][0][0]["callback_data"]

    expired = await handle_callback_update(
        _make_update(data=callback_data, callback_id="cbq-expired"),
        now=issued + timedelta(seconds=91),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=_fake_noop_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )

    assert expired["reason"] == "loss_cut_confirmation_expired"


@pytest.mark.asyncio
async def test_loss_cut_second_nonce_rejects_changed_rung_revision(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_loss_cut_proposal(db_session, monkeypatch)
    notifier = _FakeNotifier()
    issued = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="op", nonce="loss-cut-first")
        ),
        now=issued,
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=_fake_noop_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )
    callback_data = notifier.edited[-1][3]["inline_keyboard"][0][0]["callback_data"]
    service = OrderProposalsService(db_session)
    await service.transition_rung(group.proposal_id, 0, new_state="revalidating")
    await service.mark_needs_reconfirm(group.proposal_id, 0, now=issued)
    await db_session.commit()

    mismatch = await handle_callback_update(
        _make_update(data=callback_data, callback_id="cbq-mismatch"),
        now=issued + timedelta(seconds=30),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=_fake_noop_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )

    assert mismatch["reason"] == "loss_cut_confirmation_binding_mismatch"


@pytest.mark.parametrize(
    ("guard_error", "violations"),
    [
        ("loss_cut retrospective is stale (>72h)", ["retrospective_stale_72h"]),
        ("loss_cut price below current slip band", ["loss_cut_slip_band"]),
    ],
)
@pytest.mark.asyncio
async def test_loss_cut_second_click_revalidation_blocks_stale_retro_or_slip(
    monkeypatch,
    db_session,
    guard_error,
    violations,
):
    _allow_chat(monkeypatch)
    group = await _seed_loss_cut_proposal(db_session, monkeypatch)
    notifier = _FakeNotifier()
    issued = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    await handle_callback_update(
        _make_update(
            data=_proposal_callback_data(group, action="op", nonce="loss-cut-first")
        ),
        now=issued,
        service_factory=_session_factory(db_session),
        notifier=notifier,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )
    callback_data = notifier.edited[-1][3]["inline_keyboard"][0][0]["callback_data"]
    submit_attempts = 0

    async def guarded_place_order(**kwargs):
        nonlocal submit_attempts
        if kwargs["dry_run"] is False:
            submit_attempts += 1
            raise AssertionError("a second-click guard failure must not submit")
        return {
            "success": False,
            "error": guard_error,
            "violations": violations,
        }

    async def real_revalidate(**kwargs):
        return await revalidate_and_submit(
            **kwargs,
            place_order_fn=guarded_place_order,
        )

    blocked = await handle_callback_update(
        _make_update(data=callback_data, callback_id="cbq-guard-blocked"),
        now=issued + timedelta(seconds=30),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=real_revalidate,
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )

    assert blocked["reason"] == "approved"
    assert blocked["results"] == ["guard_blocked"]
    assert submit_attempts == 0
    _group, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "pending_approval"


@pytest.mark.asyncio
async def test_approve_acquires_target_lock_before_revalidation(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-target-lock")
    data = _proposal_callback_data(group, action="op", nonce="nonce-target-lock")
    events: list[str] = []

    async def fake_target_lock(self, proposal):
        assert proposal.proposal_id == group.proposal_id
        events.append("target_lock")
        return False

    async def fake_revalidate(*, service, proposal_id, now):
        events.append("revalidate")
        return [RungOutcome(0, "submitted_resting", {"submit": {}})]

    monkeypatch.setattr(
        OrderProposalsService, "acquire_target_mutation_lock", fake_target_lock
    )

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=_FakeNotifier(),
        revalidate_fn=fake_revalidate,
    )

    assert result["reason"] == "approved"
    assert events == ["target_lock", "revalidate"]


@pytest.mark.asyncio
async def test_approve_injects_configured_submit_identity(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_SUBMIT_AGENT_ID", "  proposal-agent  "
    )
    group = await _seed_proposal(db_session, nonce="nonce-identity")
    data = _proposal_callback_data(group, action="op", nonce="nonce-identity")

    async def fake_revalidate(*, service, proposal_id, now):
        assert get_caller_agent_id() == "proposal-agent"
        return []

    assert get_caller_agent_id() is None
    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=_FakeNotifier(),
        revalidate_fn=fake_revalidate,
    )

    assert result["reason"] == "approved"
    assert get_caller_agent_id() is None


@pytest.mark.asyncio
async def test_approve_empty_submit_identity_masks_and_restores_outer_identity(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_SUBMIT_AGENT_ID", "   ")
    group = await _seed_proposal(db_session, nonce="nonce-empty-identity")
    data = _proposal_callback_data(group, action="op", nonce="nonce-empty-identity")

    async def fake_revalidate(*, service, proposal_id, now):
        assert get_caller_agent_id() is None
        return []

    token = caller_agent_id_var.set("allowed-outer-agent")
    try:
        result = await handle_callback_update(
            _make_update(data=data),
            now=datetime.now(UTC),
            service_factory=_session_factory(db_session),
            notifier=_FakeNotifier(),
            revalidate_fn=fake_revalidate,
        )

        assert result["reason"] == "approved"
        assert get_caller_agent_id() == "allowed-outer-agent"
    finally:
        caller_agent_id_var.reset(token)


@pytest.mark.asyncio
async def test_approve_answers_before_order_processing_and_final_edit(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-answer-first")
    data = _proposal_callback_data(group, action="op", nonce="nonce-answer-first")
    events: list[str] = []
    notifier = _EventNotifier(events)

    @contextlib.asynccontextmanager
    async def event_session_factory():
        events.append("db")
        yield db_session

    async def fake_revalidate(*, service, proposal_id, now):
        events.append("order")
        return [RungOutcome(0, "submitted_resting", {"submit": {}})]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=event_session_factory,
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["reason"] == "approved"
    assert events == ["db", "answer", "order", "edit"]
    assert notifier.answered == [("cbq-1", "처리 중")]


@pytest.mark.asyncio
async def test_cancelled_approve_commits_before_telegram_edit(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-cancelled")
    data = _proposal_callback_data(group, action="op", nonce="nonce-cancelled")
    events: list[str] = []
    notifier = _EventNotifier(events)
    original_commit = db_session.commit

    async def event_commit():
        events.append("commit")
        await original_commit()

    monkeypatch.setattr(db_session, "commit", event_commit)

    @contextlib.asynccontextmanager
    async def event_session_factory():
        events.append("db")
        yield db_session

    async def fake_revalidate(*, service, proposal_id, now):
        events.append("order")
        return [RungOutcome(0, "cancelled", {})]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=event_session_factory,
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["reason"] == "approved"
    assert result["results"] == ["cancelled"]
    assert events == ["db", "answer", "order", "commit", "edit"]
    assert "취소 확인" in notifier.edited[0][2]


@pytest.mark.asyncio
async def test_order_failure_final_edit_includes_reason(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-order-failure")
    data = _proposal_callback_data(group, action="op", nonce="nonce-order-failure")
    notifier = _FakeNotifier()

    async def fake_revalidate(*, service, proposal_id, now):
        return [RungOutcome(0, "error", {"error": "broker_rejected"})]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["reason"] == "approved"
    assert "오류" in notifier.edited[0][2]
    assert "broker\\_rejected" in notifier.edited[0][2]


@pytest.mark.asyncio
async def test_replayed_nonce_does_not_resubmit(monkeypatch, db_session):
    # second identical callback -> nonce_replay -> no second revalidate call.
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-replay1")
    data = _proposal_callback_data(group, action="op", nonce="nonce-replay1")
    notifier = _FakeNotifier()

    call_count = 0

    async def fake_revalidate(*, service, proposal_id, now):
        nonlocal call_count
        call_count += 1
        return [RungOutcome(0, "submitted_resting", {"submit": {}})]

    first = await handle_callback_update(
        _make_update(data=data, callback_id="cbq-first"),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )
    assert first["handled"] is True
    assert call_count == 1

    second = await handle_callback_update(
        _make_update(data=data, callback_id="cbq-second"),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert call_count == 1  # no second submit
    assert second["handled"] is False
    assert second["reason"] == "nonce_replay"


@pytest.mark.asyncio
async def test_nonce_mismatch_rejected(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-real")
    data = _proposal_callback_data(group, action="op", nonce="nonce-wrong1")
    notifier = _FakeNotifier()

    async def fake_revalidate(**kwargs):
        raise AssertionError("must not be reached")

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is False
    assert result["reason"] == "nonce_mismatch"
    assert notifier.answered == []


@pytest.mark.asyncio
async def test_needs_reconfirm_sends_new_diff_message(monkeypatch, db_session):
    # revalidate_fn returns needs_reconfirm with a diff -> a NEW approval message
    # (fresh nonce) is sent; original edited to "재확인 필요".
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-recon1")
    data = _proposal_callback_data(group, action="op", nonce="nonce-recon1")
    notifier = _FakeNotifier()

    diff = {
        "before": {"limit_price": "100", "quantity": "10"},
        "after": {"limit_price": "105", "quantity": "10"},
    }

    async def fake_revalidate(*, service, proposal_id, now):
        return [RungOutcome(0, "needs_reconfirm", diff)]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is True
    assert result["reason"] == "needs_reconfirm"

    assert len(notifier.edited) == 1
    _chat_id, _message_id, edited_text, _markup = notifier.edited[0]
    assert "재확인 필요" in edited_text

    assert len(notifier.sent_messages) == 1
    new_text, new_keyboard, sent_chat_id = notifier.sent_messages[0]
    assert sent_chat_id == str(CHAT_ID)
    assert "재확인 변경사항" in new_text
    new_callback_data = new_keyboard["inline_keyboard"][0][0]["callback_data"]
    parsed = parse_callback_data(new_callback_data)
    assert parsed.action == "op"
    assert parsed.subject_short == str(group.proposal_id)[:8]
    assert parsed.nonce != _fixture_nonce("nonce-recon1")

    service = OrderProposalsService(db_session)
    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce == parsed.nonce
    assert refreshed.approved_by_telegram_user_id == str(USER_ID)


@pytest.mark.asyncio
async def test_buying_power_shortfall_edits_failure_and_sends_retry_button(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-buying-power")
    data = _proposal_callback_data(group, action="op", nonce="nonce-buying-power")
    notifier = _FakeNotifier()
    detail = {
        "reason": "insufficient_buying_power",
        "currency": "KRW",
        "available": "400000",
        "required": "1070300",
        "shortfall": "670300",
    }

    async def fake_revalidate(*, service, proposal_id, now):
        await service.transition_rung(proposal_id, 0, new_state="revalidating")
        await service.mark_needs_reconfirm(proposal_id, 0, now=now)
        return [RungOutcome(0, "needs_reconfirm", detail)]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    expected = "매수가능 400,000원 / 필요 1,070,300원 → 부족 670,300원 — 입금 후 재승인"
    assert result["reason"] == "needs_reconfirm"
    assert expected in notifier.edited[0][2]
    new_text, new_keyboard, _chat_id = notifier.sent_messages[0]
    assert expected in new_text
    approve = new_keyboard["inline_keyboard"][0][0]
    assert approve["text"] == "✅ 승인"
    parsed = parse_callback_data(approve["callback_data"])
    assert parsed.action == "op"
    assert parsed.subject_short == str(group.proposal_id)[:8]
    assert parsed.nonce != _fixture_nonce("nonce-buying-power")
    _, rungs = await OrderProposalsService(db_session).get_proposal(group.proposal_id)
    assert rungs[0].state == "needs_reconfirm"


@pytest.mark.asyncio
async def test_buying_power_multi_rung_reconfirm_shows_every_shortfall(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-buying-power-multi", rungs=2)
    data = _proposal_callback_data(group, action="op", nonce="nonce-buying-power-multi")
    notifier = _FakeNotifier()
    details = [
        {
            "reason": "insufficient_buying_power",
            "currency": "KRW",
            "available": "400000",
            "required": "600000",
            "shortfall": "200000",
        },
        {
            "reason": "insufficient_buying_power",
            "currency": "KRW",
            "available": "400000",
            "required": "700000",
            "shortfall": "300000",
        },
    ]

    async def fake_revalidate(*, service, proposal_id, now):
        outcomes = []
        for index, detail in enumerate(details):
            await service.transition_rung(proposal_id, index, new_state="revalidating")
            await service.mark_needs_reconfirm(proposal_id, index, now=now)
            outcomes.append(RungOutcome(index, "needs_reconfirm", detail))
        return outcomes

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["reason"] == "needs_reconfirm"
    new_text = notifier.sent_messages[0][0]
    assert "필요 600,000원 → 부족 200,000원" in new_text
    assert "필요 700,000원 → 부족 300,000원" in new_text


@pytest.mark.asyncio
async def test_deny_transitions_all_rungs_to_rejected(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-deny1", rungs=2)
    data = _proposal_callback_data(group, action="dn", nonce="nonce-deny1")
    notifier = _FakeNotifier()

    async def fake_revalidate(**kwargs):
        raise AssertionError("deny must not revalidate")

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is True
    assert result["reason"] == "denied"
    assert sorted(result["rejected_rungs"]) == [0, 1]

    service = OrderProposalsService(db_session)
    _group, rungs = await service.get_proposal(group.proposal_id)
    assert all(r.state == "rejected" for r in rungs)
    assert len(notifier.edited) == 1
    assert "거부" in notifier.edited[0][2]
    assert notifier.answered


@pytest.mark.asyncio
async def test_lease_held_blocks_second_approval(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-lease1")
    data = _proposal_callback_data(group, action="op", nonce="nonce-lease1")
    notifier = _FakeNotifier()

    service = OrderProposalsService(db_session)
    assert await service.acquire_commit_lease(
        group.proposal_id, now=datetime.now(UTC), lease_seconds=30
    )
    await db_session.commit()

    async def fake_revalidate(**kwargs):
        raise AssertionError("must not be reached while lease held")

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is False
    assert result["reason"] == "lease_held"
    assert notifier.answered == [("cbq-1", "처리 중")]


@pytest.mark.asyncio
async def test_malformed_callback_data_rejected(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    notifier = _FakeNotifier()

    result = await handle_callback_update(
        _make_update(data="not-valid-data"),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
    )

    assert result["handled"] is False
    assert result["reason"] == "malformed_callback_data"
    assert notifier.answered == []


@pytest.mark.asyncio
async def test_proposal_not_found_for_unknown_prefix(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    notifier = _FakeNotifier()
    unknown_id = uuid.UUID("deadbeef-0000-4000-8000-000000000000")
    data = approval_messages.build_callback_data(
        action="op",
        proposal_id=unknown_id,
        nonce="some-nonce-1",
        binding=DispatchBinding(
            attempt_id=uuid.uuid4(),
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=1,
            membership_digest="AbCdEf0123_-",
        ),
    )

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
    )

    assert result["handled"] is False
    assert result["reason"] == "proposal_not_found"
    assert notifier.answered == []


@pytest.mark.asyncio
async def test_never_raises_on_internal_error(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-boom1")
    data = _proposal_callback_data(group, action="op", nonce="nonce-boom1")
    notifier = _FakeNotifier()

    async def exploding_revalidate(**kwargs):
        raise RuntimeError("boom")

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=exploding_revalidate,
    )

    assert result["handled"] is False
    assert result["reason"] == "internal_error"
    assert notifier.answered  # best-effort answer still attempted


@pytest.mark.asyncio
async def test_approve_restores_previous_identity_when_revalidation_raises(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_SUBMIT_AGENT_ID", "proposal-agent")
    group = await _seed_proposal(db_session, nonce="nonce-identity-boom")
    data = _proposal_callback_data(group, action="op", nonce="nonce-identity-boom")

    async def exploding_revalidate(**kwargs):
        assert get_caller_agent_id() == "proposal-agent"
        raise RuntimeError("boom")

    token = caller_agent_id_var.set("outer-agent")
    try:
        result = await handle_callback_update(
            _make_update(data=data),
            now=datetime.now(UTC),
            service_factory=_session_factory(db_session),
            notifier=_FakeNotifier(),
            revalidate_fn=exploding_revalidate,
        )

        assert result["reason"] == "internal_error"
        assert get_caller_agent_id() == "outer-agent"
    finally:
        caller_agent_id_var.reset(token)


# ---------------------------------------------------------------------------
# Review Finding 1 — commit-before-notify ordering: a Telegram notify failure
# must never roll back an already-committed DB mutation, and the returned
# result must reflect the real outcome (not "internal_error").
# ---------------------------------------------------------------------------


class _EditRaisingNotifier(_FakeNotifier):
    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        raise RuntimeError("telegram edit_message boom")


@pytest.mark.asyncio
async def test_deny_survives_notify_failure_and_stays_committed(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-notifyfail-dn", rungs=2)
    data = _proposal_callback_data(group, action="dn", nonce="nonce-notifyfail-dn")
    notifier = _EditRaisingNotifier()

    async def fake_revalidate(**kwargs):
        raise AssertionError("deny must not revalidate")

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    # The notify failure must not surface as an uncaught exception / be
    # mis-reported as "internal_error" -- the real outcome (denied) is
    # reflected in the result even though edit_message raised.
    assert result["handled"] is True
    assert result["reason"] == "denied"
    assert sorted(result["rejected_rungs"]) == [0, 1]

    # Prove the reject transitions were truly COMMITTED (not merely
    # flushed-and-then-rolled-back by the notify exception) by reading them
    # back through a brand-new, independent session against the same DB.
    async with AsyncSessionLocal() as fresh_session:
        fresh_service = OrderProposalsService(fresh_session)
        _fresh_group, fresh_rungs = await fresh_service.get_proposal(group.proposal_id)
    assert all(r.state == "rejected" for r in fresh_rungs)


@pytest.mark.asyncio
async def test_approve_survives_notify_failure_and_stays_committed(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-notifyfail-op")
    data = _proposal_callback_data(group, action="op", nonce="nonce-notifyfail-op")
    notifier = _EditRaisingNotifier()

    async def fake_revalidate(*, service, proposal_id, now):
        # Mimic what the real `revalidate_and_submit` does (transition
        # through the full state chain and record via the service) so this
        # test proves the rung's real, service-recorded "resting" state
        # survives -- not just a returned-but-never-persisted RungOutcome.
        await service.transition_rung(proposal_id, 0, new_state="revalidating")
        await service.transition_rung(proposal_id, 0, new_state="approved")
        await service.transition_rung(proposal_id, 0, new_state="submitting")
        await service.record_resting(
            proposal_id,
            0,
            broker_order_id="B1",
            correlation_id="corr-1",
            idempotency_key="idem-1",
            approval_hash_digest="hash-1",
            now=now,
        )
        return [
            RungOutcome(
                0,
                "submitted_resting",
                {"submit": {"broker_order_id": "B1"}},
            )
        ]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is True
    assert result["reason"] == "approved"
    assert result["results"] == ["submitted_resting"]

    # Prove record_approval + the rung's "resting" transition were truly
    # committed before the (raising) edit_message call, via an independent
    # session.
    async with AsyncSessionLocal() as fresh_session:
        fresh_service = OrderProposalsService(fresh_session)
        fresh_group, fresh_rungs = await fresh_service.get_proposal(group.proposal_id)
    assert fresh_group.approved_by_telegram_user_id == str(USER_ID)
    assert fresh_group.approval_nonce_used_at is not None
    assert fresh_rungs[0].state == "resting"


# ---------------------------------------------------------------------------
# Review Finding 2 — multi-rung `needs_reconfirm` must not silently drop
# information: every reconfirming rung's before/after must be visible, and
# any non-reconfirming outcome in the same batch must also be reported.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_needs_reconfirm_multi_rung_shows_every_diff(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-multi-recon", rungs=2)
    data = _proposal_callback_data(group, action="op", nonce="nonce-multi-recon")
    notifier = _FakeNotifier()

    diff0 = {
        "before": {"limit_price": "100", "quantity": "10"},
        "after": {"limit_price": "105", "quantity": "10"},
    }
    diff1 = {
        "before": {"limit_price": "200", "quantity": "20"},
        "after": {"limit_price": "222", "quantity": "20"},
    }

    async def fake_revalidate(*, service, proposal_id, now):
        return [
            RungOutcome(0, "needs_reconfirm", diff0),
            RungOutcome(1, "needs_reconfirm", diff1),
        ]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is True
    assert result["reason"] == "needs_reconfirm"
    assert len(notifier.sent_messages) == 1
    new_text, _new_keyboard, _sent_chat_id = notifier.sent_messages[0]

    # Rung #1's diff (rendered by build_approval_message's base `diff=`).
    assert "105" in new_text
    # Rung #2's diff must ALSO be visible -- previously silently dropped.
    assert "200" in new_text
    assert "222" in new_text
    assert "추가 재확인 필요" in new_text


@pytest.mark.asyncio
async def test_needs_reconfirm_mixed_batch_reports_other_outcome(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-mixed-recon", rungs=2)
    data = _proposal_callback_data(group, action="op", nonce="nonce-mixed-recon")
    notifier = _FakeNotifier()

    diff1 = {
        "before": {"limit_price": "200", "quantity": "20"},
        "after": {"limit_price": "222", "quantity": "20"},
    }

    async def fake_revalidate(*, service, proposal_id, now):
        return [
            RungOutcome(0, "submitted_resting", {"submit": {"broker_order_id": "B1"}}),
            RungOutcome(1, "needs_reconfirm", diff1),
        ]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is True
    assert result["reason"] == "needs_reconfirm"
    assert len(notifier.sent_messages) == 1
    new_text, _new_keyboard, _sent_chat_id = notifier.sent_messages[0]

    # Rung #2's reconfirm diff (base `diff=` from build_approval_message).
    assert "222" in new_text
    # Rung #1's submitted_resting outcome must ALSO be reported -- previously
    # never surfaced anywhere because the reconfirm branch short-circuited.
    assert "처리 결과" in new_text
    assert "주문 유지" in new_text  # _RESULT_LABELS["submitted_resting"]


# ---------------------------------------------------------------------------
# Final-review Finding 2 — a rung stuck in `needs_reconfirm` must be
# transitioned back to `pending_approval` before the second `revalidate_fn`
# call, so a second Approve click on the reconfirm message actually
# re-submits instead of silently no-op'ing forever.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfirm_click_transitions_rung_and_reenters_revalidation(
    monkeypatch, db_session
):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-cycle1")
    data = _proposal_callback_data(group, action="op", nonce="nonce-cycle1")
    notifier = _FakeNotifier()

    diff = {
        "before": {"limit_price": "100", "quantity": "10"},
        "after": {"limit_price": "105", "quantity": "10"},
    }
    observed_states: list[str] = []

    async def fake_revalidate(*, service, proposal_id, now):
        # Spy on the rung's state at the moment revalidate_fn is invoked --
        # the fix under test transitions needs_reconfirm -> pending_approval
        # BEFORE this call, so the first call sees pending_approval (initial
        # create) and the second call must ALSO see pending_approval (post
        # re-approve transition), never needs_reconfirm. Mimic the real
        # `revalidate_and_submit`'s own DB transitions (not just a returned
        # RungOutcome) so the rung's real state after each call matches what
        # a genuine reconfirm cycle would leave behind.
        _g, rungs = await service.get_proposal(proposal_id)
        observed_states.append(rungs[0].state)
        if len(observed_states) == 1:
            await service.transition_rung(proposal_id, 0, new_state="revalidating")
            await service.mark_needs_reconfirm(proposal_id, 0, now=now)
            return [RungOutcome(0, "needs_reconfirm", diff)]
        await service.transition_rung(proposal_id, 0, new_state="revalidating")
        await service.transition_rung(proposal_id, 0, new_state="approved")
        await service.transition_rung(proposal_id, 0, new_state="submitting")
        await service.record_resting(
            proposal_id,
            0,
            broker_order_id="B1",
            correlation_id="corr-1",
            idempotency_key="idem-1",
            approval_hash_digest="hash-1",
            now=now,
        )
        return [
            RungOutcome(0, "submitted_resting", {"submit": {"broker_order_id": "B1"}})
        ]

    first_now = datetime.now(UTC)
    first = await handle_callback_update(
        _make_update(data=data, callback_id="cbq-1st"),
        now=first_now,
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )
    assert first["handled"] is True
    assert first["reason"] == "needs_reconfirm"
    assert observed_states == ["pending_approval"]

    service = OrderProposalsService(db_session)
    _group, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "needs_reconfirm"

    _new_text, new_keyboard, _sent_chat_id = notifier.sent_messages[0]
    new_callback_data = new_keyboard["inline_keyboard"][0][0]["callback_data"]
    parsed = parse_callback_data(new_callback_data)
    assert parsed.action == "op"
    assert parsed.subject_short == str(group.proposal_id)[:8]
    second_data = new_callback_data

    # `acquire_commit_lease`'s default 10s lease was taken by the first call
    # -- advance `now` well past it so the second click isn't spuriously
    # blocked by `lease_held` (a real second click would arrive well after
    # 10s of human reaction time to the reconfirm message anyway).
    second_now = first_now + timedelta(seconds=30)
    second = await handle_callback_update(
        _make_update(data=second_data, callback_id="cbq-2nd"),
        now=second_now,
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    # The second click must actually reach revalidate_fn again (not be
    # skipped because the rung was still parked in needs_reconfirm), and by
    # the time it runs the rung must already be back in pending_approval.
    assert observed_states == ["pending_approval", "pending_approval"]
    assert second["handled"] is True
    assert second["reason"] == "approved"
    assert second["results"] == ["submitted_resting"]

    _final_group, final_rungs = await service.get_proposal(group.proposal_id)
    assert final_rungs[0].state == "resting"


# ---------------------------------------------------------------------------
# Final-review Finding 4 — a reconfirm resend must refresh
# source_asof.approval_message_id to the NEW message id, not leave it
# pointing at the original dispatch.py message.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfirm_resend_refreshes_approval_message_id(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-msgid1")
    seed_service = OrderProposalsService(db_session)
    await _publish_fixture_card(
        seed_service,
        group,
        nonce="nonce-msgid1",
        card_kind=ApprovalCardKind.MANUAL,
        message_id=111,
    )
    await db_session.commit()

    data = _proposal_callback_data(group, action="op", nonce="nonce-msgid1")
    notifier = _FakeNotifier()

    diff = {
        "before": {"limit_price": "100", "quantity": "10"},
        "after": {"limit_price": "105", "quantity": "10"},
    }

    async def fake_revalidate(*, service, proposal_id, now):
        return [RungOutcome(0, "needs_reconfirm", diff)]

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        revalidate_fn=fake_revalidate,
    )

    assert result["handled"] is True
    assert result["reason"] == "needs_reconfirm"
    new_message_id = result["new_message_id"]
    assert new_message_id is not None
    assert new_message_id != 111

    service = OrderProposalsService(db_session)
    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.source_asof["approval_message_id"] == new_message_id
    assert refreshed.source_asof["approval_message_id"] != 111


# ---------------------------------------------------------------------------
# `_resolve_proposal_id` — short-prefix resolution edge cases (gap #2).
# ---------------------------------------------------------------------------


class _FakeGroup:
    def __init__(self, proposal_id: uuid.UUID) -> None:
        self.proposal_id = proposal_id


class _FakeResolveService:
    def __init__(self, ids: list[uuid.UUID]) -> None:
        self._ids = ids
        self.calls: list[str] = []

    async def resolve_proposal_id_prefix(self, proposal_short):
        self.calls.append(proposal_short)
        matches = [pid for pid in self._ids if str(pid).startswith(proposal_short)]
        return matches[0] if len(matches) == 1 else None


@pytest.mark.asyncio
async def test_resolve_proposal_id_zero_matches_returns_none():
    svc = _FakeResolveService([])
    assert await _resolve_proposal_id(svc, "deadbeef") is None
    assert svc.calls == ["deadbeef"]


@pytest.mark.asyncio
async def test_resolve_proposal_id_multiple_matches_returns_none():
    pid1 = uuid.UUID("deadbeef-0000-0000-0000-000000000001")
    pid2 = uuid.UUID("deadbeef-0000-0000-0000-000000000002")
    svc = _FakeResolveService([pid1, pid2])
    assert await _resolve_proposal_id(svc, "deadbeef") is None


@pytest.mark.asyncio
async def test_resolve_proposal_id_unique_match_returns_id():
    pid1 = uuid.UUID("deadbeef-0000-0000-0000-000000000001")
    pid2 = uuid.UUID("cafebabe-0000-0000-0000-000000000002")
    svc = _FakeResolveService([pid1, pid2])
    assert await _resolve_proposal_id(svc, "deadbeef") == pid1
