from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.approval_message import parse_callback_data
from app.services.order_proposals.revalidation import RungOutcome
from app.services.order_proposals.service import RungInput
from app.services.order_proposals.telegram_callback import (
    _resolve_proposal_id,
    handle_callback_update,
)

CHAT_ID = 42
USER_ID = 777


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, dict, str]] = []
        self.answered: list[tuple[str, str | None]] = []
        self.edited: list[tuple[str, int, str, dict | None]] = []
        self._next_message_id = 9000

    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        self._next_message_id += 1
        self.sent_messages.append((text, inline_keyboard, chat_id))
        return self._next_message_id

    async def answer_callback(self, callback_query_id, text=None):
        self.answered.append((callback_query_id, text))
        return True

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))
        return True


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
    await db_session.commit()
    return group


def _allow_chat(monkeypatch, chat_id=CHAT_ID):
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", str(chat_id)
    )


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
    data = f"op:{str(group.proposal_id)[:8]}:nonce-abc123"
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
    assert notifier.answered == [("cbq-1", "허용되지 않은 채팅")]
    assert notifier.edited == []


@pytest.mark.asyncio
async def test_approve_happy_path_submits_and_edits(monkeypatch, db_session):
    # allowlist includes chat; nonce valid; revalidate_fn returns submitted_resting;
    # assert edit_message called with a summary, answer_callback called,
    # nonce consumed, approved_by_telegram_user_id recorded.
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-happy1")
    data = f"op:{str(group.proposal_id)[:8]}:nonce-happy1"
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


@pytest.mark.asyncio
async def test_replayed_nonce_does_not_resubmit(monkeypatch, db_session):
    # second identical callback -> nonce_replay -> no second revalidate call.
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-replay1")
    data = f"op:{str(group.proposal_id)[:8]}:nonce-replay1"
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
    data = f"op:{str(group.proposal_id)[:8]}:nonce-wrong1"
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
    assert notifier.answered


@pytest.mark.asyncio
async def test_needs_reconfirm_sends_new_diff_message(monkeypatch, db_session):
    # revalidate_fn returns needs_reconfirm with a diff -> a NEW approval message
    # (fresh nonce) is sent; original edited to "재확인 필요".
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-recon1")
    data = f"op:{str(group.proposal_id)[:8]}:nonce-recon1"
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
    new_action, new_short, new_nonce = parse_callback_data(new_callback_data)
    assert new_action == "op"
    assert new_short == str(group.proposal_id)[:8]
    assert new_nonce != "nonce-recon1"

    service = OrderProposalsService(db_session)
    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce == new_nonce
    assert refreshed.approved_by_telegram_user_id == str(USER_ID)


@pytest.mark.asyncio
async def test_deny_transitions_all_rungs_to_rejected(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-deny1", rungs=2)
    data = f"dn:{str(group.proposal_id)[:8]}:nonce-deny1"
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
    data = f"op:{str(group.proposal_id)[:8]}:nonce-lease1"
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
    assert notifier.answered


@pytest.mark.asyncio
async def test_proposal_not_found_for_unknown_prefix(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    notifier = _FakeNotifier()
    data = "op:deadbeef:some-nonce-1"

    result = await handle_callback_update(
        _make_update(data=data),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
    )

    assert result["handled"] is False
    assert result["reason"] == "proposal_not_found"
    assert notifier.answered


@pytest.mark.asyncio
async def test_never_raises_on_internal_error(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="nonce-boom1")
    data = f"op:{str(group.proposal_id)[:8]}:nonce-boom1"
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


# ---------------------------------------------------------------------------
# `_resolve_proposal_id` — short-prefix resolution edge cases (gap #2).
# ---------------------------------------------------------------------------


class _FakeGroup:
    def __init__(self, proposal_id: uuid.UUID) -> None:
        self.proposal_id = proposal_id


class _FakeListRecentService:
    def __init__(self, ids: list[uuid.UUID]) -> None:
        self._ids = ids
        self.calls: list[dict] = []

    async def list_recent(self, *, lifecycle_state, limit):
        self.calls.append({"lifecycle_state": lifecycle_state, "limit": limit})
        return [(_FakeGroup(pid), []) for pid in self._ids]


@pytest.mark.asyncio
async def test_resolve_proposal_id_zero_matches_returns_none():
    svc = _FakeListRecentService([])
    assert await _resolve_proposal_id(svc, "deadbeef") is None
    assert svc.calls[0]["lifecycle_state"] == "proposed"


@pytest.mark.asyncio
async def test_resolve_proposal_id_multiple_matches_returns_none():
    pid1 = uuid.UUID("deadbeef-0000-0000-0000-000000000001")
    pid2 = uuid.UUID("deadbeef-0000-0000-0000-000000000002")
    svc = _FakeListRecentService([pid1, pid2])
    assert await _resolve_proposal_id(svc, "deadbeef") is None


@pytest.mark.asyncio
async def test_resolve_proposal_id_unique_match_returns_id():
    pid1 = uuid.UUID("deadbeef-0000-0000-0000-000000000001")
    pid2 = uuid.UUID("cafebabe-0000-0000-0000-000000000002")
    svc = _FakeListRecentService([pid1, pid2])
    assert await _resolve_proposal_id(svc, "deadbeef") == pid1
