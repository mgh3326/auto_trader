"""ROB-1281: kis_live approval dispatch recorded ``payload_chars=0``.

Production evidence (Sentry ``AUTO_TRADER-2EC``, 2026-08-17 22:51--22:58 KST,
release ``dd8db23be``) showed ``exception_type=OrderProposalError`` /
``failure_code=approval_dispatch_internal_error``: the auto-approve lane had
already submitted the order, KIS rejected it explicitly (``EGW00201``),
``revalidation.record_rejected`` terminalized the rung, and the fall-through to
the manual approval card then refused to mint a nonce.  ``payload_chars=0`` was
the hard-coded default of the MCP post-commit exception boundary
(``order_proposal_tools.py``), *not* a rendered card of zero length -- the card
builder never ran.

These tests pin three things:

* the not-dispatchable refusal is ledgered with its real reason instead of a
  bare domain error that reads as a renderer failure;
* that ledger is reached *only* for a proposal that genuinely died -- the
  redispatch guards protecting a live card must still reach the caller
  untouched (r2 blocker: recording one of those burned the card it protected);
  and
* every live lane still renders a non-empty card carrying the ROB-458 / §40
  contract fields, so the #1876 abbreviation is proven innocent for
  ``kis_live`` (KR and US), ``toss_live`` and ``upbit`` alike.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.order_proposals import OrderProposalApprovalDispatchAttempt
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals.approval_message import (
    ApprovalDispatchMessages,
    build_approval_dispatch_messages,
    build_batch_approval_message,
    build_loss_cut_confirmation_message,
)
from app.services.order_proposals.dispatch import (
    publish_approval_messages,
    send_proposal_for_approval,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    DispatchBinding,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.service import (
    RungInput,
    proposal_approval_block_reason,
)
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session

_ATTEMPT_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")


@pytest.fixture(autouse=True)
def _known_market_session(monkeypatch):
    monkeypatch.setattr(
        dispatch_module, "evaluate_approval_window", allow_known_session
    )


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, dict | None, str]] = []

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        self.sent_messages.append((text, inline_keyboard, chat_id))
        return TelegramMethodResult(
            ok=True,
            message_id=7001,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )


def _session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


def _binding(
    card_kind: ApprovalCardKind = ApprovalCardKind.MANUAL,
) -> DispatchBinding:
    return DispatchBinding(
        attempt_id=_ATTEMPT_ID,
        card_kind=card_kind,
        membership_revision=1,
        membership_digest="AbCdEf0123_-",
    )


def _group(**overrides):
    values = {
        "proposal_id": uuid.uuid4(),
        "symbol": "005930",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "side": "sell",
        "order_type": "limit",
        "action": "place",
        "thesis": None,
        "strategy": None,
        "valid_until": None,
        "validated_at": None,
        "commit_lease_until": None,
        "source_asof": None,
        "payload_hash": None,
        "approval_nonce": "abc123def45",
        "exit_intent": None,
        "exit_reason": None,
        "retrospective_id": None,
        "approval_issue_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(**overrides):
    values = {
        "rung_index": 0,
        "quantity": Decimal("10"),
        "limit_price": Decimal("70000"),
        "approval_hash_digest": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# AC1/AC2 -- the real production path: no card is rendered, and the ledger now
# says why instead of flattening to a generic internal error.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_rejected_proposal_ledgers_typed_not_dispatchable_refusal(
    monkeypatch, db_session
):
    """Reproduces the 2026-08-17 kis_live US failure and pins the typed code.

    Before the fix ``send_proposal_for_approval`` raised
    ``OrderProposalError("proposal_terminal:rejected")`` here, which the MCP
    post-commit boundary recorded as ``approval_dispatch_internal_error`` with
    ``payload_chars=0``.
    """
    from app.core.config import settings

    chat_id = f"chat-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )
    now = datetime(2026, 8, 17, 13, 51, tzinfo=UTC)

    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="IVV",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="claude-us-open-0817",
        thesis="KIS 2주 @503.2975, 현재가 779.38 — R1 787.83 근접 트림",
        strategy="sell.trim_preplace.rsi_confirmed_resistance",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("787.83"), None)],
    )
    await db_session.commit()

    # Production shape: the auto-approve lane submitted, KIS answered
    # ``EGW00201 초당 거래건수를 초과하였습니다.`` as an explicit rejection, and
    # revalidation terminalized the rung -- which recomputes the group to
    # ``rejected``, a state that can no longer carry an approval nonce.
    await service.record_rejected(
        group.proposal_id,
        0,
        reason="EGW00201 초당 거래건수를 초과하였습니다.",
        now=now,
    )
    await db_session.commit()
    terminalized, rungs = await service.get_proposal(group.proposal_id)
    assert [rung.state for rung in rungs] == ["rejected"]
    assert terminalized.lifecycle_state == "rejected"

    notifier = _FakeNotifier()
    result = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    # No Telegram I/O, and no card body was ever composed on this path.
    assert notifier.sent_messages == []
    assert result.ok is False
    assert result.payload_chars == 0
    assert result.failure_code == "approval_not_dispatchable:proposal_terminal:rejected"

    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    assert (
        refreshed.approval_dispatch_failure_code
        == "approval_not_dispatchable:proposal_terminal:rejected"
    )
    assert refreshed.approval_dispatch_payload_chars == 0
    assert refreshed.approval_dispatch_state == "failed"


@pytest.mark.asyncio
async def test_superseded_proposal_ledgers_typed_not_dispatchable_refusal(
    monkeypatch, db_session
):
    """The supersede reissues in the incident hit the same gate, not a renderer."""
    from app.core.config import settings

    chat_id = f"chat-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )
    now = datetime(2026, 8, 17, 13, 58, tzinfo=UTC)
    service = OrderProposalsService(db_session)
    original = await service.create_proposal(
        symbol="GOOGL",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="claude-us-open-0817",
        thesis="R1 345.73 초근접 트림",
        strategy="sell.trim_preplace.ultra_near_resistance",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("345.73"), None)],
    )
    replacement = await service.create_proposal(
        symbol="GOOGL",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="claude-us-open-0817",
        thesis="dispatch 복구 재발행 — 가격·수량·근거 무변경",
        strategy="sell.trim_preplace.ultra_near_resistance",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("345.73"), None)],
        supersedes_proposal_id=original.proposal_id,
    )
    await db_session.commit()

    notifier = _FakeNotifier()
    result = await send_proposal_for_approval(
        original.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )

    assert notifier.sent_messages == []
    assert result.payload_chars == 0
    assert result.failure_code == (
        f"approval_not_dispatchable:proposal_superseded_by:{replacement.proposal_id}"
    )


# --------------------------------------------------------------------------
# ROB-1281 r2 -- the refusal ledger must never touch a *live* card.
#
# ``set_approval_nonce`` raises for two unrelated reasons.  Only one of them
# means "this proposal is dead": ``proposal_terminal:*`` /
# ``proposal_superseded_by:*``.  The other two --
# ``approval_dispatch_already_pending`` and ``approval_dispatch_already_
# current`` -- are the redispatch guard *protecting* an existing card, and
# ledgering them would run ``_record_proposal_not_dispatchable``, which
# supersedes the current attempt (service.py) and clears the nonce when the
# attempt finishes failed.  That would burn a card an operator can still press.
# --------------------------------------------------------------------------


async def _seed_and_send_one_card(db_session, monkeypatch, *, now):
    """Produce the real post-dispatch state: sent_current + unused live nonce."""
    from app.core.config import settings

    chat_id = f"chat-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="rob1281-r2",
        thesis="지지선 방어 확인 후 분할 매수",
        strategy="buy.ladder.support_defense",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("70000"), None)],
    )
    await db_session.commit()

    notifier = _FakeNotifier()
    first = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=now,
        service_factory=_session_factory(db_session),
    )
    assert first.ok is True, "precondition: the first card must actually publish"
    live, _rungs = await service.get_proposal(group.proposal_id)
    assert live.approval_dispatch_state == "sent_current"
    assert live.approval_nonce is not None
    assert live.approval_nonce_used_at is None
    return service, live, notifier


async def _capture_redispatch(db_session, proposal_id, *, notifier, now):
    """Run the redispatch and report *how* it answered, without asserting yet.

    Keeping the outcome instead of wrapping the call in ``pytest.raises`` lets
    the state-preservation assertions run in both worlds, so a re-widened
    catch fails on the card it destroyed rather than on the exception type.
    """
    try:
        returned = await send_proposal_for_approval(
            proposal_id,
            notifier=notifier,
            now=now,
            service_factory=_session_factory(db_session),
            redispatch=True,
        )
    except OrderProposalError as exc:
        return exc, None
    return None, returned


async def _attempt_state(db_session, attempt_id):
    row = await db_session.execute(
        select(OrderProposalApprovalDispatchAttempt).where(
            OrderProposalApprovalDispatchAttempt.attempt_id == attempt_id
        )
    )
    return row.scalar_one().state


@pytest.mark.asyncio
async def test_redispatch_refusal_preserves_a_live_current_card(
    monkeypatch, db_session
):
    """A ``sent_current`` card survives a redispatch attempt, untouched.

    The r1 fix caught every ``OrderProposalError`` from the nonce mint, so
    ``approval_dispatch_already_current`` -- the guard whose entire job is to
    refuse to disturb this card -- was ledgered as "not dispatchable", which
    flipped the group to ``failed`` and destroyed the live nonce.  That is
    strictly worse than the bug it was fixing: the original defect only failed
    to build a card for a dead proposal.
    """
    now = datetime(2026, 8, 18, 0, 30, tzinfo=UTC)
    service, live, notifier = await _seed_and_send_one_card(
        db_session, monkeypatch, now=now
    )
    # Materialise everything now: the rollback below expires the ORM instance,
    # and a lazy refresh afterwards would read through the very state under test.
    proposal_id = live.proposal_id
    before = SimpleNamespace(
        state=live.approval_dispatch_state,
        nonce=live.approval_nonce,
        nonce_used_at=live.approval_nonce_used_at,
        attempt_id=live.approval_dispatch_attempt_id,
        published_at=live.approval_dispatch_published_at,
        failure_code=live.approval_dispatch_failure_code,
        payload_chars=live.approval_dispatch_payload_chars,
    )
    assert await _attempt_state(db_session, before.attempt_id) == "sent_current"
    sent_before = len(notifier.sent_messages)

    raised, returned = await _capture_redispatch(
        db_session, proposal_id, notifier=notifier, now=now
    )

    # Mirror production: the dispatch session is discarded, so everything the
    # assertions below read is what is actually durable in the database.
    await db_session.rollback()
    after, _rungs = await service.get_proposal(proposal_id)

    # The damage assertions come first *on purpose*: re-widening the catch
    # makes the call return a ledgered result instead of raising, and this
    # test must then die on the destroyed card -- not merely on "did not
    # raise", which would prove nothing about state preservation.
    # (1) the dispatch state is preserved -- not flipped to ``failed``
    assert after.approval_dispatch_state == "sent_current" == before.state
    # (2) the nonce is still live -- the operator's button still works
    assert after.approval_nonce == before.nonce
    assert after.approval_nonce is not None
    assert after.approval_nonce_used_at is None is before.nonce_used_at
    # (3) the current attempt is not replaced or superseded
    assert after.approval_dispatch_attempt_id == before.attempt_id
    assert await _attempt_state(db_session, before.attempt_id) == "sent_current"
    # ...and nothing else about the published card was rewritten
    assert after.approval_dispatch_published_at == before.published_at
    assert after.approval_dispatch_failure_code == before.failure_code
    assert after.approval_dispatch_payload_chars == before.payload_chars
    assert len(notifier.sent_messages) == sent_before

    # Only then: the guard reached the caller as its own typed refusal, which
    # is what ``order_proposal_redispatch`` turns into a plain error string.
    assert returned is None
    assert isinstance(raised, OrderProposalError)
    assert str(raised) == "approval_dispatch_already_current"


@pytest.mark.asyncio
async def test_redispatch_refusal_preserves_an_in_flight_pending_card(
    monkeypatch, db_session
):
    """The ``already_pending`` twin gets the same protection.

    The adversarial verification only reproduced the ``current`` half; this
    pins the ``pending`` half directly rather than by inference.  A pending
    attempt is a card whose Telegram outcome is not yet known, so overwriting
    its ownership row is exactly the state loss the guard exists to prevent.
    """
    from app.core.config import settings

    chat_id = f"chat-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat_id
    )
    now = datetime(2026, 8, 18, 0, 35, tzinfo=UTC)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="rob1281-r2",
        thesis="지지선 방어 확인 후 분할 매수",
        strategy="buy.ladder.support_defense",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("70000"), None)],
    )
    proposal_id = group.proposal_id
    await service.set_approval_nonce(proposal_id, "pendingnonce")
    group, _rungs = await service.get_proposal(proposal_id)
    in_flight_attempt = uuid.uuid4()
    await service.start_approval_dispatch(
        proposal_id,
        attempt_id=in_flight_attempt,
        binding=build_proposal_dispatch_binding(
            proposal_id=proposal_id,
            nonce="pendingnonce",
            attempt_id=in_flight_attempt,
            card_kind=ApprovalCardKind.MANUAL,
            current_membership_revision=group.approval_dispatch_membership_revision,
        ),
        now=now,
        payload_chars=242,
        context_message_count=0,
    )
    await db_session.commit()

    notifier = _FakeNotifier()
    raised, returned = await _capture_redispatch(
        db_session, proposal_id, notifier=notifier, now=now
    )

    await db_session.rollback()
    after, _rungs = await service.get_proposal(proposal_id)

    # Damage first, refusal shape second -- same ordering rationale as the
    # ``sent_current`` twin above.
    assert after.approval_dispatch_state == "pending"
    assert after.approval_dispatch_attempt_id == in_flight_attempt
    assert await _attempt_state(db_session, in_flight_attempt) == "pending"
    assert after.approval_nonce == "pendingnonce"
    assert after.approval_nonce_used_at is None
    assert after.approval_dispatch_failure_code is None
    assert after.approval_dispatch_payload_chars == 242
    assert notifier.sent_messages == []

    assert returned is None
    assert isinstance(raised, OrderProposalError)
    assert str(raised) == "approval_dispatch_already_pending"


@pytest.mark.unit
def test_not_dispatchable_allowlist_matches_the_service_block_reasons():
    """Pin the string coupling the narrowed catch depends on.

    ``dispatch`` decides what to ledger by matching the reason text that
    ``service.proposal_approval_block_reason`` produces.  If those reason
    strings are ever renamed, this goes red *before* the catch silently stops
    recognising a dead proposal (and, worse, before the redispatch guards
    start matching).
    """
    prefixes = dispatch_module._NOT_DISPATCHABLE_REASON_PREFIXES

    terminal = proposal_approval_block_reason(
        SimpleNamespace(superseded_by_proposal_id=None, lifecycle_state="rejected")
    )
    superseded_by_id = proposal_approval_block_reason(
        SimpleNamespace(
            superseded_by_proposal_id=uuid.uuid4(), lifecycle_state="superseded"
        )
    )
    superseded_unknown = proposal_approval_block_reason(
        SimpleNamespace(superseded_by_proposal_id=None, lifecycle_state="superseded")
    )
    approvable = proposal_approval_block_reason(
        SimpleNamespace(superseded_by_proposal_id=None, lifecycle_state="proposed")
    )

    assert approvable is None
    for reason in (terminal, superseded_by_id, superseded_unknown):
        assert reason is not None
        assert reason.startswith(prefixes), f"{reason!r} must be ledgerable"

    # The redispatch guards are *not* proposal-death reasons and must never
    # match, or narrowing the catch would have been pointless.
    for guard_reason in (
        "approval_dispatch_already_pending",
        "approval_dispatch_already_current",
    ):
        assert not guard_reason.startswith(prefixes)


# --------------------------------------------------------------------------
# AC2 -- fail-closed: a body-less card is refused, never published.
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   \n \t "])
async def test_publish_refuses_a_card_with_no_visible_body(blank):
    notifier = _FakeNotifier()
    messages = ApprovalDispatchMessages(
        (),
        blank,
        {"inline_keyboard": [[{"text": "✅ 승인", "callback_data": "x"}]]},
        telegram_text_length(blank),
    )

    publication = await publish_approval_messages(
        notifier=notifier, messages=messages, chat_id="chat-1"
    )

    assert publication.card_published is False
    assert publication.failure_code == "approval_payload_empty"
    assert notifier.sent_messages == []


# --------------------------------------------------------------------------
# AC3 -- every live lane still renders a non-empty, contract-complete card.
#
# 🔴 Naming honesty (ROB-1281 r2).  These four samples were originally called
# "account mode coverage", which they are not: the manual card builder reads
# ``market``/``symbol``/``side``/``order_type`` and never branches on
# ``account_mode``.  Measured -- with ``account_mode`` set to ``kis_live``,
# ``toss_live``, ``upbit``, ``None`` and a nonsense value, the rendered body is
# byte-for-byte identical (189 chars for a common fixture).  What they really
# discriminate is *market formatting*: ``equity_kr`` -> ₩, ``equity_us`` -> $,
# ``crypto`` -> fractional quantity.  The only card surface that renders
# ``account_mode`` at all is the batch summary, covered separately below.
#
# The four lanes are still worth keeping as distinct rows -- they are the four
# live proposal sources, and #1876's verification skipped ``kis_live``
# entirely -- but calling them account-mode coverage would repeat the exact
# mistake that let ROB-1281 through: a sample matrix that cannot fail for the
# reason its name claims.
# --------------------------------------------------------------------------


_LIVE_LANE_SAMPLES = {
    "kis_live_kr": (
        _group(
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            thesis="외국인 순매수 전환과 지지선 방어를 근거로 분할 매수",
            strategy="buy.ladder.support_defense",
            valid_until=datetime(2026, 8, 18, 6, 0, tzinfo=UTC),
        ),
        [_rung(quantity=Decimal("10"), limit_price=Decimal("70000"))],
        ["005930", "equity_kr / buy / limit", "10주", "₩70,000", "총수량 10주"],
    ),
    "kis_live_us": (
        _group(
            symbol="IVV",
            market="equity_us",
            account_mode="kis_live",
            side="sell",
            thesis="KIS 2주 @503.2975, 현재가 779.38 — R1 787.83 근접 트림",
            strategy="sell.trim_preplace.rsi_confirmed_resistance",
            valid_until=datetime(2026, 8, 17, 20, 0, tzinfo=UTC),
        ),
        [_rung(quantity=Decimal("1"), limit_price=Decimal("787.83"))],
        ["IVV", "equity_us / sell / limit", "1주", "$787.83", "총수량 1주"],
    ),
    "toss_live": (
        _group(
            symbol="042660",
            market="equity_kr",
            account_mode="toss_live",
            side="sell",
            thesis="목표가 도달 구간 분할 익절",
            strategy="sell.trim_preplace.target",
            valid_until=datetime(2026, 8, 18, 6, 0, tzinfo=UTC),
        ),
        [_rung(quantity=Decimal("4"), limit_price=Decimal("88000"))],
        ["042660", "equity_kr / sell / limit", "4주", "₩88,000", "총수량 4주"],
    ),
    "upbit": (
        _group(
            symbol="KRW-BTC",
            market="crypto",
            account_mode="upbit",
            side="buy",
            thesis="변동성 축소 구간 재진입",
            strategy="buy.ladder.volatility_contraction",
            valid_until=datetime(2026, 8, 18, 6, 0, tzinfo=UTC),
        ),
        [_rung(quantity=Decimal("0.002"), limit_price=Decimal("95000000"))],
        ["KRW-BTC", "crypto / buy / limit", "0.002", "₩95,000,000", "총수량 0.002"],
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("sample_key", sorted(_LIVE_LANE_SAMPLES))
def test_manual_card_renders_non_empty_contract_complete_body_per_market(sample_key):
    """AC3: ``payload_chars > 0`` plus the §40 fields, per live lane.

    Discriminates *market formatting* (KRW / USD / crypto quantity), not
    account-mode routing -- see the block comment above ``_LIVE_LANE_SAMPLES``.
    ``kis_live`` (KR *and* US) is the lane #1876's verification skipped, which
    is why it is pinned here even though the builder treats it like the rest.
    """
    group, rungs, expected_fragments = _LIVE_LANE_SAMPLES[sample_key]

    messages = build_approval_dispatch_messages(
        group=group, rungs=rungs, binding=_binding()
    )

    assert messages.payload_chars > 0
    assert messages.payload_chars == telegram_text_length(messages.approval_text)
    text = messages.approval_text
    for fragment in expected_fragments:
        assert fragment in text, f"{sample_key}: missing {fragment!r}"
    # §40 card contract: symbol / side / quantity / price / rationale / validity.
    assert "종목:" in text
    assert "시장/방향/유형:" in text
    assert "주문 단계" in text
    assert "핵심 수치:" in text
    assert "투자 논지:" in text
    assert "전략:" in text
    assert "유효기간:" in text
    assert messages.inline_keyboard["inline_keyboard"][0][0]["text"] == "✅ 승인"


@pytest.mark.unit
def test_manual_card_body_does_not_depend_on_account_mode():
    """Record the measurement the lane samples cannot make on their own.

    This is the fact that makes the honest renaming above necessary, kept as
    an executable statement rather than a comment: swapping ``account_mode``
    on an otherwise identical proposal changes nothing in the manual card.  If
    someone later gives the manual builder an account-mode branch, this fails
    and forces the lane samples to be re-described (and actually made
    discriminating) instead of silently inheriting a name they no longer earn.
    """
    common = {
        "symbol": "005930",
        "market": "equity_kr",
        "side": "buy",
        "thesis": "지지선 방어",
        "strategy": "buy.ladder.support_defense",
        "valid_until": datetime(2026, 8, 18, 6, 0, tzinfo=UTC),
    }
    rendered = {
        mode: build_approval_dispatch_messages(
            group=_group(account_mode=mode, **common),
            rungs=[_rung()],
            binding=_binding(),
        ).approval_text
        for mode in ("kis_live", "toss_live", "upbit", None)
    }

    assert len(set(rendered.values())) == 1, (
        "the manual card now branches on account_mode -- rename and re-scope "
        "_LIVE_LANE_SAMPLES so it actually discriminates the new behaviour"
    )


@pytest.mark.unit
@pytest.mark.parametrize("account_mode", ["kis_live", "toss_live", "upbit"])
def test_batch_card_labels_the_account_mode_it_was_built_from(account_mode):
    """The one card surface that *does* route on account_mode.

    ``_batch_account_label`` puts the account mode on every batch summary
    line, so an operator approving a mixed batch can see which account each
    order lands in.  This is the genuinely account-mode-discriminating
    assertion the r1 sample matrix claimed to be and was not: the card body
    changes with the account mode and nothing else here does.
    """
    batch = SimpleNamespace(batch_id=uuid.uuid4(), approval_nonce="btch123def45")
    members = [
        (_group(symbol="005930", account_mode=account_mode), [_rung()]),
        (
            _group(symbol="042660", account_mode=account_mode),
            [_rung(quantity=Decimal("4"), limit_price=Decimal("88000"))],
        ),
    ]

    text, _keyboard = build_batch_approval_message(
        batch=batch,
        proposals=members,
        binding=_binding(ApprovalCardKind.BATCH),
    )

    # Markdown-escaped in the rendered line (``kis\_live``), so compare on the
    # escaped form rather than asserting a substring that cannot appear.
    escaped_mode = account_mode.replace("_", r"\_")
    lines = [line for line in text.splitlines() if line.startswith("- `")]
    assert len(lines) == 2
    for line in lines:
        assert f"· {escaped_mode} ·" in line, f"missing account label in {line!r}"
    for other in {"kis_live", "toss_live", "upbit"} - {account_mode}:
        assert other.replace("_", r"\_") not in text


@pytest.mark.unit
def test_loss_cut_confirmation_card_keeps_loss_pct_and_second_window():
    """The two safety notices on the 2nd-step loss-cut card are non-negotiable."""
    group = _group(
        symbol="IVV",
        market="equity_us",
        account_mode="kis_live",
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_nonce="secondnonce",
        source_asof={
            "loss_cut_confirmation": {"expires_at": "2026-08-18T00:30:00+00:00"}
        },
    )

    text, _keyboard = build_loss_cut_confirmation_message(
        group=group,
        rungs=[_rung(quantity=Decimal("2"), limit_price=Decimal("700"))],
        evidence={
            "rungs": [
                {
                    "rung_index": 0,
                    "current_price": "690",
                    "loss_pct": "-12.34",
                    "loss_cut_slip_band": "685",
                }
            ],
            "retrospective_id": 42,
            "lesson_excerpt": "손절 기준을 늦추지 않는다",
        },
        binding=_binding(ApprovalCardKind.LOSS_CUT_CONFIRMATION),
    )

    assert telegram_text_length(text) > 0
    assert "손실률 -12.34%" in text
    assert "2차 창(유효시간): 09:30 KST (2026-08-18)" in text
