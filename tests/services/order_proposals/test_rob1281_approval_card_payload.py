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

These tests pin both halves:

* the not-dispatchable refusal is ledgered with its real reason instead of a
  bare domain error that reads as a renderer failure, and
* every account mode still renders a non-empty card carrying the ROB-458 /
  §40 contract fields, so the #1876 abbreviation is proven innocent for
  ``kis_live`` (KR and US), ``toss_live`` and ``upbit`` alike.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals.approval_message import (
    ApprovalDispatchMessages,
    build_approval_dispatch_messages,
    build_loss_cut_confirmation_message,
)
from app.services.order_proposals.dispatch import (
    publish_approval_messages,
    send_proposal_for_approval,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    DispatchBinding,
)
from app.services.order_proposals.service import RungInput
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
# AC3 -- every account mode still renders a non-empty, contract-complete card.
# --------------------------------------------------------------------------


_ACCOUNT_MODE_SAMPLES = {
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
@pytest.mark.parametrize("sample_key", sorted(_ACCOUNT_MODE_SAMPLES))
def test_approval_card_payload_is_non_empty_and_contract_complete(sample_key):
    """AC3: ``payload_chars > 0`` plus the §40 fields, per account mode.

    ``kis_live`` (KR *and* US) is the variant #1876's verification skipped;
    ``toss_live``/``upbit`` keep the other two live lanes covered.
    """
    group, rungs, expected_fragments = _ACCOUNT_MODE_SAMPLES[sample_key]

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
