"""ROB-1052 — notices-destination routing + auto-approve digest batching.

Covers the acceptance surface: per-class routing, the unconfigured fallback,
thread-only (forum topic) routing, digest batching (0/1/N items), no message
lost on a send failure of either destination, the veto re-render on a shared
digest message, and the expiry notice copy.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.monitoring.trade_notifier import TradeNotifier
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals.auto_digest import (
    notices_destination,
    render_auto_digest_chunks,
    send_order_proposal_expiry_notice,
    snapshot_auto_digest_item,
)
from app.services.order_proposals.dispatch import (
    dispatch_proposal,
    open_auto_digest_round,
    send_proposal_for_approval,
)
from app.services.order_proposals.revalidation import RungOutcome
from app.services.order_proposals.service import RungInput
from app.services.order_proposals.target_order import TargetOrderSnapshot
from app.services.order_proposals.telegram_callback import (
    handle_callback_update,
)
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session

CHAT_ID = "chat-99"
NOTICES_CHAT_ID = "notices-7"
NOTICES_THREAD_ID = "4242"
USER_ID = 777


@pytest.fixture(autouse=True)
def _known_market_session(monkeypatch):
    monkeypatch.setattr(
        dispatch_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(
        revalidation_module, "evaluate_approval_window", allow_known_session
    )
    import app.services.order_proposals.telegram_callback as callback_module

    monkeypatch.setattr(
        callback_module, "evaluate_approval_window", allow_known_session
    )


@pytest.fixture(autouse=True)
def _clear_notices_settings(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", "")
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", "")


def _allowlist(*chats: str) -> str:
    return ",".join(chats)


class _FakeNotifier:
    """Records sends, edits and answers; each send mints a new message id.

    ``first_message_id`` must differ between tests that share a chat id: the
    test database persists proposals across tests, so a digest member lookup
    on (chat_id, message_id) could otherwise match a stale earlier digest.
    """

    def __init__(
        self, *, fail_sends: bool = False, first_message_id: int = 8000
    ) -> None:
        self.sent_messages: list[tuple[str, dict | None, str, int | None]] = []
        self.edited: list[tuple[str, int, str, dict | None]] = []
        self.answered: list[tuple[str, str | None]] = []
        self.auto_veto_mirrors: list[dict] = []
        self._next_message_id = first_message_id
        self._fail_sends = fail_sends

    async def send_approval_message(
        self,
        text,
        inline_keyboard,
        *,
        chat_id,
        parse_mode="Markdown",
        message_thread_id=None,
    ):
        self.sent_messages.append(
            (text, inline_keyboard, str(chat_id), message_thread_id)
        )
        if self._fail_sends:
            return TelegramMethodResult.failed(
                payload_chars=telegram_text_length(text),
                failure_code="telegram_transport_error",
            )
        self._next_message_id += 1
        return TelegramMethodResult(
            ok=True,
            message_id=self._next_message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((str(chat_id), int(message_id), text, reply_markup))
        return TelegramMethodResult(
            ok=True,
            message_id=int(message_id),
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def answer_callback(self, callback_query_id, text=None):
        self.answered.append((callback_query_id, text))
        return True

    async def send_auto_veto_card_mirror(self, **kwargs):
        self.auto_veto_mirrors.append(kwargs)


def _session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _factory():
        try:
            yield db_session
        except BaseException:
            await db_session.rollback()
            raise

    return _factory


async def _auto_proposal(db_session, *, symbol: str, side: str = "buy"):
    service = OrderProposalsService(db_session)
    price = Decimal("97000") if side == "buy" else Decimal("103000")
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side=side,
        order_type="limit",
        proposer="digest-test",
        thesis=f"thesis {symbol}",
        broker_account_id=f"digest-{symbol}-{uuid.uuid4()}",
        rungs=[RungInput(0, side, Decimal("1"), price, None)],
    )
    await db_session.commit()
    return group


def _auto_revalidate(*, broker_suffix: str):
    async def fake_revalidate(*, service, proposal_id, now, eligibility_gate):
        fresh_group, rungs = await service.get_proposal(proposal_id)
        decision = await eligibility_gate(
            group=fresh_group,
            rung=rungs[0],
            preview={
                "success": True,
                "current_price": "100000",
                "price": str(rungs[0].limit_price),
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
            broker_order_id=f"broker-{broker_suffix}",
            correlation_id=f"corr-{broker_suffix}",
            idempotency_key=f"idem-{broker_suffix}",
            approval_hash_digest=f"digest-{broker_suffix}",
            now=now,
        )
        return [RungOutcome(0, "submitted_resting", {})]

    return fake_revalidate


async def _dispatch_auto(
    db_session,
    group,
    notifier,
    *,
    now: datetime | None = None,
    broker_suffix: str,
):
    return await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=now or datetime(2026, 9, 30, 1, 0, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=_auto_revalidate(broker_suffix=broker_suffix),
    )


# ── destination resolution (unit, no DB) ────────────────────────────


def test_notices_destination_unconfigured_falls_back(monkeypatch):
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    dest = notices_destination()
    assert dest.configured is False
    assert dest.chat_id == CHAT_ID
    assert dest.message_thread_id is None


def test_notices_destination_separate_chat(monkeypatch):
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    dest = notices_destination()
    assert dest.configured is True
    assert dest.chat_id == NOTICES_CHAT_ID
    assert dest.message_thread_id is None


def test_notices_destination_thread_only_uses_approval_chat(monkeypatch):
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", NOTICES_THREAD_ID
    )
    dest = notices_destination()
    assert dest.configured is True
    assert dest.chat_id == CHAT_ID
    assert dest.message_thread_id == int(NOTICES_THREAD_ID)


def test_notices_destination_malformed_thread_is_unconfigured(monkeypatch):
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", "not-an-int"
    )
    dest = notices_destination()
    assert dest.configured is False
    assert dest.chat_id == CHAT_ID
    assert dest.message_thread_id is None


# ── per-class routing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_standalone_auto_notice_routes_to_notices_chat_and_thread(
    monkeypatch, db_session
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", NOTICES_THREAD_ID
    )
    group = await _auto_proposal(db_session, symbol="005930")
    notifier = _FakeNotifier()

    await _dispatch_auto(db_session, group, notifier, broker_suffix="s1")

    assert len(notifier.sent_messages) == 1
    text, keyboard, chat_id, thread_id = notifier.sent_messages[0]
    assert chat_id == NOTICES_CHAT_ID
    assert thread_id == int(NOTICES_THREAD_ID)
    assert "자동 접수됨" in text
    assert keyboard["inline_keyboard"][0][0]["callback_data"].startswith("vc:")
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "resting"
    assert refreshed.approval_dispatch_state == "sent_current"


@pytest.mark.asyncio
async def test_standalone_auto_notice_fallback_when_unconfigured(
    monkeypatch, db_session
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    group = await _auto_proposal(db_session, symbol="000660")
    notifier = _FakeNotifier()

    await _dispatch_auto(db_session, group, notifier, broker_suffix="s2")

    assert len(notifier.sent_messages) == 1
    _text, _kb, chat_id, thread_id = notifier.sent_messages[0]
    assert chat_id == CHAT_ID
    assert thread_id is None


@pytest.mark.asyncio
async def test_thread_only_keeps_chat_but_routes_to_topic(monkeypatch, db_session):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", NOTICES_THREAD_ID
    )
    group = await _auto_proposal(db_session, symbol="035420")
    notifier = _FakeNotifier()

    await _dispatch_auto(db_session, group, notifier, broker_suffix="s3")

    assert len(notifier.sent_messages) == 1
    _text, _kb, chat_id, thread_id = notifier.sent_messages[0]
    assert chat_id == CHAT_ID
    assert thread_id == int(NOTICES_THREAD_ID)


@pytest.mark.asyncio
async def test_pending_approval_card_stays_on_approval_chat(monkeypatch, db_session):
    """A human-decision card must never enter the notices destination."""
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", False)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", NOTICES_THREAD_ID
    )
    group = await _auto_proposal(db_session, symbol="051910")
    notifier = _FakeNotifier()

    result = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 9, 30, 1, 0, tzinfo=UTC),
        service_factory=_session_factory(db_session),
    )

    assert result.ok is True
    assert len(notifier.sent_messages) == 1
    text, keyboard, chat_id, thread_id = notifier.sent_messages[0]
    assert chat_id == CHAT_ID
    assert thread_id is None
    # Human card keeps approve/deny buttons -- not a digest notice.
    actions = {
        btn["callback_data"].split(":")[0]
        for row in keyboard["inline_keyboard"]
        for btn in row
        if "callback_data" in btn
    }
    assert actions == {"op", "dn"}


# ── digest batching: 0 / 1 / N items ────────────────────────────────


@pytest.mark.asyncio
async def test_digest_round_zero_items_sends_nothing(db_session):
    notifier = _FakeNotifier()
    async with open_auto_digest_round(
        notifier=notifier, service_factory=_session_factory(db_session)
    ):
        pass
    assert notifier.sent_messages == []


@pytest.mark.asyncio
async def test_digest_round_batches_two_items_into_one_message(monkeypatch, db_session):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    first = await _auto_proposal(db_session, symbol="005930")
    second = await _auto_proposal(db_session, symbol="000660")
    notifier = _FakeNotifier(first_message_id=8100)

    async with open_auto_digest_round(
        notifier=notifier, service_factory=_session_factory(db_session)
    ):
        await _dispatch_auto(db_session, first, notifier, broker_suffix="d1")
        await _dispatch_auto(db_session, second, notifier, broker_suffix="d2")

    assert len(notifier.sent_messages) == 1
    text, keyboard, chat_id, thread_id = notifier.sent_messages[0]
    assert chat_id == NOTICES_CHAT_ID
    assert thread_id is None
    assert "자동승인 2건" in text
    assert "005930" in text
    assert "000660" in text
    buttons = [
        btn["callback_data"] for row in keyboard["inline_keyboard"] for btn in row
    ]
    assert len(buttons) == 2
    assert all(data.startswith("vc:") for data in buttons)

    service = OrderProposalsService(db_session)
    digest_refs = []
    for group in (first, second):
        refreshed, rungs = await service.get_proposal(group.proposal_id)
        assert rungs[0].state == "resting"
        assert refreshed.approval_dispatch_state == "sent_current"
        digest_refs.append(refreshed.source_asof["auto_digest"])
    assert digest_refs[0] == digest_refs[1]
    assert digest_refs[0]["chat_id"] == NOTICES_CHAT_ID
    assert digest_refs[0]["message_id"] is not None


@pytest.mark.asyncio
async def test_digest_round_single_item_sends_card_without_digest_ref(
    monkeypatch, db_session
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    group = await _auto_proposal(db_session, symbol="068270")
    notifier = _FakeNotifier(first_message_id=8200)

    async with open_auto_digest_round(
        notifier=notifier, service_factory=_session_factory(db_session)
    ):
        await _dispatch_auto(db_session, group, notifier, broker_suffix="d3")

    assert len(notifier.sent_messages) == 1
    text, keyboard, chat_id, _thread = notifier.sent_messages[0]
    assert chat_id == NOTICES_CHAT_ID
    assert "068270" in text
    buttons = [
        btn["callback_data"] for row in keyboard["inline_keyboard"] for btn in row
    ]
    assert len(buttons) == 1 and buttons[0].startswith("vc:")
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "resting"
    assert refreshed.approval_dispatch_state == "sent_current"
    # A one-item digest is just a card -- no shared-message link needed.
    assert "auto_digest" not in (refreshed.source_asof or {})


# ── send failure: no notice is silently lost ────────────────────────


@pytest.mark.asyncio
async def test_digest_send_failure_compensates_every_member(monkeypatch, db_session):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    first = await _auto_proposal(db_session, symbol="005930")
    second = await _auto_proposal(db_session, symbol="000660")
    notifier = _FakeNotifier(fail_sends=True, first_message_id=8300)

    async def cancel_fn(**kwargs):
        return {"success": True}

    async def fetch_fn(**kwargs):
        return TargetOrderSnapshot(
            broker_order_id=kwargs["order_id"],
            symbol="005930",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="0",
            status="cancelled",
            observed_at=kwargs["now"].isoformat(),
        )

    async with open_auto_digest_round(
        notifier=notifier,
        service_factory=_session_factory(db_session),
        cancel_target_fn=cancel_fn,
        fetch_target_fn=fetch_fn,
    ):
        await _dispatch_auto(db_session, first, notifier, broker_suffix="f1")
        await _dispatch_auto(db_session, second, notifier, broker_suffix="f2")

    service = OrderProposalsService(db_session)
    for group in (first, second):
        refreshed, rungs = await service.get_proposal(group.proposal_id)
        # The resting orders were pulled back: no orphan broker orders and
        # no phantom "published" veto card.  The failure is durable.
        assert rungs[0].state == "cancelled"
        auto = refreshed.source_asof["auto_approved"]
        assert auto["notification_failure"]["error"]
        assert "auto_digest" not in refreshed.source_asof


@pytest.mark.asyncio
async def test_standalone_send_failure_keeps_compensation_path(monkeypatch, db_session):
    """The standalone (no round) failure keeps today's cancel compensation."""
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    group = await _auto_proposal(db_session, symbol="032830")
    notifier = _FakeNotifier(fail_sends=True)

    async def cancel_fn(**kwargs):
        return {"success": True}

    async def fetch_fn(**kwargs):
        return TargetOrderSnapshot(
            broker_order_id=kwargs["order_id"],
            symbol="032830",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="0",
            status="cancelled",
            observed_at=kwargs["now"].isoformat(),
        )

    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime(2026, 9, 30, 1, 0, tzinfo=UTC),
        service_factory=_session_factory(db_session),
        revalidate_fn=_auto_revalidate(broker_suffix="f3"),
        cancel_target_fn=cancel_fn,
        fetch_target_fn=fetch_fn,
    )

    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert rungs[0].state == "cancelled"
    assert refreshed.source_asof["auto_approved"]["notification_failure"]["error"]


# ── veto on a shared digest re-renders, keeping sibling buttons ─────


@pytest.mark.asyncio
async def test_auto_veto_re_renders_digest_and_keeps_sibling_button(
    monkeypatch, db_session
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    # The notices chat must be allowlisted for its callbacks to authorize.
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        _allowlist(CHAT_ID, NOTICES_CHAT_ID),
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    first = await _auto_proposal(db_session, symbol="005930")
    second = await _auto_proposal(db_session, symbol="000660")
    notifier = _FakeNotifier(first_message_id=8400)

    async with open_auto_digest_round(
        notifier=notifier, service_factory=_session_factory(db_session)
    ):
        await _dispatch_auto(db_session, first, notifier, broker_suffix="v1")
        await _dispatch_auto(db_session, second, notifier, broker_suffix="v2")

    _text, keyboard, digest_chat, _thread = notifier.sent_messages[0]
    digest_message_id = notifier._next_message_id
    buttons = [
        btn["callback_data"] for row in keyboard["inline_keyboard"] for btn in row
    ]
    assert len(buttons) == 2

    async def cancel_fn(**kwargs):
        return {"success": True}

    async def fetch_fn(**kwargs):
        return TargetOrderSnapshot(
            broker_order_id=kwargs["order_id"],
            symbol="005930",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="0",
            status="cancelled",
            observed_at=kwargs["now"].isoformat(),
        )

    def _veto_update(callback_data: str) -> dict:
        return {
            "callback_query": {
                "id": f"cbq-{uuid.uuid4()}",
                "from": {"id": USER_ID},
                "message": {
                    "chat": {"id": digest_chat},
                    "message_id": digest_message_id,
                },
                "data": callback_data,
            }
        }

    result = await handle_callback_update(
        _veto_update(buttons[0]),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        veto_cancel_fn=cancel_fn,
        veto_fetch_fn=fetch_fn,
    )
    assert result["handled"] is True
    assert result["reason"] == "auto_veto_cancelled"

    # One edit on the SHARED message: the vetoed member shows its outcome
    # while the live sibling keeps a working vc button.
    assert len(notifier.edited) == 1
    edited_chat, edited_mid, edited_text, edited_kb = notifier.edited[0]
    assert edited_chat == digest_chat
    assert edited_mid == digest_message_id
    assert "취소됨" in edited_text
    remaining = [
        btn["callback_data"] for row in edited_kb["inline_keyboard"] for btn in row
    ]
    assert remaining == [buttons[1]]

    # The second veto still works -- its nonce was not destroyed.
    second_result = await handle_callback_update(
        _veto_update(buttons[1]),
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        notifier=notifier,
        veto_cancel_fn=cancel_fn,
        veto_fetch_fn=fetch_fn,
    )
    assert second_result["handled"] is True
    assert second_result["reason"] == "auto_veto_cancelled"
    assert len(notifier.edited) == 2
    last_kb = notifier.edited[-1][3]
    assert [btn for row in last_kb["inline_keyboard"] for btn in row] == []


# ── expiry notice copy ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expiry_notice_sends_to_notices_destination(monkeypatch):
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", NOTICES_THREAD_ID
    )
    notifier = _FakeNotifier()
    await send_order_proposal_expiry_notice(
        notifier=notifier, text="⏰ 승인 만료됨 — ABC"
    )
    assert len(notifier.sent_messages) == 1
    text, kb, chat_id, thread_id = notifier.sent_messages[0]
    assert chat_id == NOTICES_CHAT_ID
    assert thread_id == int(NOTICES_THREAD_ID)
    assert "승인 만료" in text


@pytest.mark.asyncio
async def test_expiry_notice_is_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    notifier = _FakeNotifier()
    await send_order_proposal_expiry_notice(
        notifier=notifier, text="⏰ 승인 만료됨 — ABC"
    )
    # Unconfigured means byte-identical pre-split behavior: no extra message.
    assert notifier.sent_messages == []


# ── notifier-level fill routing ─────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_telegram_fallback_routes_to_notices():
    TradeNotifier._instance = None
    TradeNotifier._initialized = False
    notifier = TradeNotifier()
    try:
        notifier.configure(
            bot_token="t",
            chat_ids=["chat-a", "chat-b"],
            enabled=True,
            notices_chat_id=NOTICES_CHAT_ID,
            notices_thread_id=int(NOTICES_THREAD_ID),
            notices_configured=True,
        )
        with patch(
            "app.monitoring.trade_notifier.notifier.send_telegram",
            new=AsyncMock(return_value=True),
        ) as mock_send:
            ok = await notifier._dispatch(
                discord_embed=None,
                telegram_message="fill text",
                market_type="kr",
                telegram_destination="notices",
            )
        assert ok is True
        kwargs = mock_send.await_args.kwargs
        assert kwargs["chat_ids"] == [NOTICES_CHAT_ID]
        assert kwargs["message_thread_id"] == int(NOTICES_THREAD_ID)
    finally:
        TradeNotifier._instance = None
        TradeNotifier._initialized = False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_telegram_fallback_fans_out_when_unconfigured():
    TradeNotifier._instance = None
    TradeNotifier._initialized = False
    notifier = TradeNotifier()
    try:
        notifier.configure(
            bot_token="t",
            chat_ids=["chat-a", "chat-b"],
            enabled=True,
        )
        with patch(
            "app.monitoring.trade_notifier.notifier.send_telegram",
            new=AsyncMock(return_value=True),
        ) as mock_send:
            ok = await notifier._dispatch(
                discord_embed=None,
                telegram_message="fill text",
                market_type="kr",
                telegram_destination="notices",
            )
        assert ok is True
        kwargs = mock_send.await_args.kwargs
        assert kwargs["chat_ids"] == ["chat-a", "chat-b"]
        assert "message_thread_id" not in kwargs
    finally:
        TradeNotifier._instance = None
        TradeNotifier._initialized = False


# ── chunking respects the Telegram text limit ───────────────────────


def test_digest_chunks_respect_telegram_text_limit():
    from app.telegram_contract import (
        TELEGRAM_SEND_MESSAGE_TEXT_LIMIT,
        telegram_text_length,
    )

    items = []
    for i in range(60):
        group = type(
            "G",
            (),
            {
                "proposal_id": uuid.uuid4(),
                "symbol": f"SYM{i:02d}",
                "market": "equity_kr",
                "account_mode": "kis_live",
                "broker_account_id": f"acct-{i}",
                "side": "buy",
                "action": "place",
                "target_broker_order_id": None,
                "valid_until": datetime.now(UTC) + timedelta(hours=1),
            },
        )()
        rungs = [
            type("R", (), {"rung_index": 0, "quantity": "1", "limit_price": "100"})()
        ]
        items.append(
            snapshot_auto_digest_item(
                group=group,
                rungs=rungs,
                attempt_id=None,
                binding=None,
                veto_nonce=None,
                vetoable=False,
                result="submitted_resting",
                policy_version="t",
                display_name=None,
                payload_chars=0,
            )
        )
    chunks = render_auto_digest_chunks(items)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert telegram_text_length(chunk.text) <= TELEGRAM_SEND_MESSAGE_TEXT_LIMIT
    # every item lands in exactly one chunk
    assert sum(len(c.items) for c in chunks) == len(items)


# ── B1 regression: the header separator + part digits belong to the budget ──


def _sized_digest_item(index: int, acct_len: int) -> Any:
    from app.services.order_proposals.auto_digest import AutoDigestItem

    return AutoDigestItem(
        proposal_id=uuid.uuid4(),
        attempt_id=None,
        vetoable=False,
        callback_data=None,
        payload_chars=0,
        symbol=f"SYM{index:03d}",
        display_name=None,
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id="a" * acct_len,
        side="buy",
        action="place",
        target_broker_order_id=None,
        quantities=["#1 1"],
        prices=["#1 100"],
        result="submitted_resting",
        thesis_summary="t",
        valid_until_text="2026-09-30 10:00",
        policy_version="t",
        detail_url=None,
    )


def test_digest_chunks_never_exceed_limit_near_boundary():
    """Sizes landing exactly at the limit must not leak a 4097-char chunk."""
    import random

    from app.telegram_contract import (
        TELEGRAM_SEND_MESSAGE_TEXT_LIMIT,
        telegram_text_length,
    )

    # The exact probe shape that exposed the bug: 42 items, acct 1..60 chars.
    rng = random.Random(187)
    items = [_sized_digest_item(i, rng.randint(1, 60)) for i in range(42)]
    chunks = render_auto_digest_chunks(items)
    assert all(
        telegram_text_length(chunk.text) <= TELEGRAM_SEND_MESSAGE_TEXT_LIMIT
        for chunk in chunks
    )
    assert sum(len(c.items) for c in chunks) == len(items)

    # Wider fuzz: many size profiles, every chunk must fit.
    for seed in range(300):
        trial = random.Random(seed)
        trial_items = [
            _sized_digest_item(i, trial.randint(1, 60))
            for i in range(trial.randint(20, 60))
        ]
        for chunk in render_auto_digest_chunks(trial_items):
            assert telegram_text_length(chunk.text) <= TELEGRAM_SEND_MESSAGE_TEXT_LIMIT


def test_digest_single_oversized_item_block_is_bounded():
    """A lone block that outgrows a chunk is shortened, never sent oversized."""
    from app.services.order_proposals.auto_digest import AutoDigestItem
    from app.telegram_contract import (
        TELEGRAM_SEND_MESSAGE_TEXT_LIMIT,
        telegram_text_length,
    )

    oversized = AutoDigestItem(
        proposal_id=uuid.uuid4(),
        attempt_id=None,
        vetoable=False,
        callback_data=None,
        payload_chars=0,
        symbol="HUGE",
        display_name=None,
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id="acct",
        side="buy",
        action="place",
        target_broker_order_id=None,
        quantities=[f"#{i} 1" for i in range(400)],
        prices=[f"#{i} 100" for i in range(400)],
        result="submitted_resting",
        thesis_summary="t",
        valid_until_text="2026-09-30 10:00",
        policy_version="t",
        detail_url=None,
    )
    chunks = render_auto_digest_chunks([oversized])
    assert len(chunks) == 1
    assert telegram_text_length(chunks[0].text) <= TELEGRAM_SEND_MESSAGE_TEXT_LIMIT


# ── B2 regression: flush failures alert and expose real outcomes ───


@pytest.mark.asyncio
async def test_digest_send_failure_alerts_and_records_outcomes(monkeypatch, db_session):
    """A failed digest keeps the standalone-path operator alert and state."""
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    first = await _auto_proposal(db_session, symbol="005930")
    second = await _auto_proposal(db_session, symbol="000660")
    notifier = _FakeNotifier(fail_sends=True, first_message_id=8400)

    async def cancel_fn(**kwargs):
        return {"success": True}

    async def fetch_fn(**kwargs):
        return TargetOrderSnapshot(
            broker_order_id=kwargs["order_id"],
            symbol="005930",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="0",
            status="cancelled",
            observed_at=kwargs["now"].isoformat(),
        )

    alerts: list[dict[str, Any]] = []

    class _Alert:
        def __init__(self, proposal_id):
            self._pid = proposal_id

        def as_dict(self):
            return {
                "state": "sent",
                "channel": "discord",
                "proposal_id": str(self._pid),
            }

    async def fake_alert(
        proposal_id, *, dispatch_state, dispatch_failure_code, now, service_factory
    ):
        alerts.append(
            {
                "proposal_id": proposal_id,
                "dispatch_state": dispatch_state,
                "dispatch_failure_code": dispatch_failure_code,
            }
        )
        return _Alert(proposal_id)

    monkeypatch.setattr(dispatch_module, "send_approval_dispatch_alert", fake_alert)

    async with open_auto_digest_round(
        notifier=notifier,
        service_factory=_session_factory(db_session),
        cancel_target_fn=cancel_fn,
        fetch_target_fn=fetch_fn,
    ) as digest_round:
        await _dispatch_auto(db_session, first, notifier, broker_suffix="g1")
        await _dispatch_auto(db_session, second, notifier, broker_suffix="g2")

    # Same alert the tooling fires for a failed standalone card — per member.
    assert {a["proposal_id"] for a in alerts} == {
        first.proposal_id,
        second.proposal_id,
    }
    assert all(a["dispatch_state"] == "failed" for a in alerts)
    # The collected outcomes let the caller rewrite its stale "pending".
    assert set(digest_round.outcomes) == {
        first.proposal_id,
        second.proposal_id,
    }
    for outcome in digest_round.outcomes.values():
        assert outcome["state"] == "failed"
        assert outcome["ok"] is False
        assert outcome["operator_alert"]["state"] == "sent"


@pytest.mark.asyncio
async def test_digest_allowlist_empty_alerts_and_records_outcomes(
    monkeypatch, db_session
):
    """The empty-destination flush branch alerts exactly like a failed send."""
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "")
    group = await _auto_proposal(db_session, symbol="005930")
    notifier = _FakeNotifier(first_message_id=8500)

    async def cancel_fn(**kwargs):
        return {"success": True}

    async def fetch_fn(**kwargs):
        return TargetOrderSnapshot(
            broker_order_id=kwargs["order_id"],
            symbol="005930",
            side="buy",
            order_type="limit",
            limit_price="97000",
            remaining_quantity="0",
            status="cancelled",
            observed_at=kwargs["now"].isoformat(),
        )

    alerts: list[dict[str, Any]] = []

    class _Alert:
        def as_dict(self):
            return {"state": "sent", "channel": "discord"}

    async def fake_alert(
        proposal_id, *, dispatch_state, dispatch_failure_code, now, service_factory
    ):
        alerts.append(
            {
                "proposal_id": proposal_id,
                "dispatch_failure_code": dispatch_failure_code,
            }
        )
        return _Alert()

    monkeypatch.setattr(dispatch_module, "send_approval_dispatch_alert", fake_alert)

    async with open_auto_digest_round(
        notifier=notifier,
        service_factory=_session_factory(db_session),
        cancel_target_fn=cancel_fn,
        fetch_target_fn=fetch_fn,
    ) as digest_round:
        await _dispatch_auto(db_session, group, notifier, broker_suffix="h1")

    assert len(alerts) == 1
    assert alerts[0]["proposal_id"] == group.proposal_id
    assert alerts[0]["dispatch_failure_code"] == "telegram_allowlist_empty"
    outcome = digest_round.outcomes[group.proposal_id]
    assert outcome["state"] == "failed"
    assert outcome["failure_code"] == "telegram_allowlist_empty"


def test_reconcile_pending_digest_outcomes_swaps_real_states():
    """Post-scope reconcile turns a buffered 'pending' into the flush outcome."""
    from app.mcp_server.tooling.support_reserve_net_consumer_tool import (
        _reconcile_pending_digest_outcomes,
    )

    pid_ok = uuid.uuid4()
    pid_failed = uuid.uuid4()
    pid_missing = uuid.uuid4()
    completed = [
        {"proposal_id": str(pid_ok), "approval_dispatch": {"state": "pending"}},
        {
            "proposal_id": str(pid_failed),
            "approval_dispatch": {"state": "pending"},
        },
        {
            "proposal_id": str(pid_missing),
            "approval_dispatch": {"state": "pending"},
        },
        {"proposal_id": "x", "approval_dispatch": {"state": "sent"}},
        {"proposal_id": "y"},
    ]
    digest_round = type(
        "R",
        (),
        {
            "outcomes": {
                pid_ok: {"state": "sent", "ok": True},
                pid_failed: {
                    "state": "failed",
                    "failure_code": "telegram_dispatch_failed",
                    "ok": False,
                    "operator_alert": {"state": "sent"},
                },
            }
        },
    )()
    _reconcile_pending_digest_outcomes(completed, digest_round)
    assert completed[0]["approval_dispatch"] == {"state": "sent", "ok": True}
    assert completed[1]["approval_dispatch"]["state"] == "failed"
    assert completed[1]["approval_dispatch"]["operator_alert"] == {"state": "sent"}
    # An item the flush never finalized keeps its pending marker.
    assert completed[2]["approval_dispatch"] == {"state": "pending"}
    # Non-pending entries are never touched.
    assert completed[3]["approval_dispatch"] == {"state": "sent"}
    assert "approval_dispatch" not in completed[4]


# ── N2: non-positive thread ids are unconfigured ────────────────────


def test_notices_destination_nonpositive_thread_is_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", "")
    for raw in ("0", "-5"):
        monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", raw)
        dest = notices_destination()
        assert dest.configured is False
        assert dest.message_thread_id is None
    # A non-positive thread with a notices chat degrades to chat-only.
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID", NOTICES_CHAT_ID
    )
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID", "-5")
    dest = notices_destination()
    assert dest.configured is True
    assert dest.chat_id == NOTICES_CHAT_ID
    assert dest.message_thread_id is None
