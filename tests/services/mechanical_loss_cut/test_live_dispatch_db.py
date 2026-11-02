"""The live path through real proposals and dispatch (ROB-1460). Runs in CI.

Offline: fake notifier and a scripted preview/submit seam. No broker socket opens.
"""

from __future__ import annotations

import contextlib
import random
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.mechanical_loss_cut import live as live_module
from app.services.mechanical_loss_cut.gate import MECHANICAL_STRATEGY
from app.services.mechanical_loss_cut.rules import CutDecision, LotKey
from app.services.mechanical_loss_cut.store import MechanicalLossCutStore
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals.auto_approve import AutoApproveLimits
from app.services.order_proposals.auto_approve_audit import (
    project_auto_approve_rejections,
)
from app.services.order_proposals.dispatch import dispatch_proposal
from app.services.order_proposals.revalidation import (
    RungOutcome,
    revalidate_and_submit,
)
from app.services.order_proposals.service import OrderProposalsService, RungInput
from app.services.trade_journal.trade_retrospective_service import save_retrospective
from app.services.trading_policy_service import load_trading_policy
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

NOW = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)  # Friday 10:00 KST
_LIMITS = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("2000000"),
    daily_cap=Decimal("5000000"),
    policy_version="2026-11-02.1",
    mode="expanded",
)


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict | None]] = []

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown", **_kwargs
    ):
        self.sent.append((text, inline_keyboard))
        return TelegramMethodResult(
            ok=True,
            message_id=9000 + len(self.sent),
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def send_auto_veto_card_mirror(self, **_kwargs):
        return True

    def approval_cards(self) -> list[str]:
        return [body for body, _keyboard in self.sent if "주문 제안 승인" in body]

    def auto_cards(self) -> list[str]:
        return [body for body, _keyboard in self.sent if "자동 접수됨" in body]


class _Revalidate:
    def __init__(self, preview: dict) -> None:
        self.preview = preview
        self.submits = 0

    async def __call__(self, *, service, proposal_id, now, eligibility_gate):
        group, rungs = await service.get_proposal(proposal_id)
        decision = await eligibility_gate(
            group=group, rung=rungs[0], preview=self.preview, now=now
        )
        if not decision.eligible:
            return [
                RungOutcome(
                    0,
                    "approval_required",
                    {"reason": decision.reason, **decision.details},
                )
            ]
        self.submits += 1
        await service.transition_rung(proposal_id, 0, new_state="revalidating")
        await service.transition_rung(proposal_id, 0, new_state="approved")
        await service.transition_rung(proposal_id, 0, new_state="submitting")
        await service.record_resting(
            proposal_id,
            0,
            broker_order_id="kis-1",
            correlation_id="corr-1",
            idempotency_key="idem-1",
            approval_hash_digest="digest-1",
            now=now,
        )
        return [RungOutcome(0, "submitted_resting", {})]


def _preview(*, current="84000", quantity="10") -> dict:
    return {
        "success": True,
        "current_price": current,
        "price": "83200",
        "quantity": quantity,
        "avg_buy_price": "100000",
    }


def _factory(db_session):
    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    return factory


@pytest.fixture(autouse=True)
def _gates_open(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", True)
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_MARKETS", "kr")
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ACCOUNT_MODES", "kis_live")
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"chat-1460-{uuid.uuid4().hex}",
    )
    monkeypatch.setattr(dispatch_module, "limits_for_market", lambda _market: _LIMITS)
    monkeypatch.setattr(
        dispatch_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(
        revalidation_module, "evaluate_approval_window", allow_known_session
    )
    policy = load_trading_policy().model_copy(deep=True)
    policy.mechanical_loss_cut.markets["kr"].thresholds_confirmed = True
    monkeypatch.setattr(dispatch_module, "load_trading_policy", lambda: policy)


def _symbol() -> str:
    return f"{random.randrange(100000, 999999)}"


def _decision(symbol: str, *, quantity: str = "10") -> CutDecision:
    limit = Decimal("83200")
    return CutDecision(
        key=LotKey("kis_live", "equity_kr", symbol),
        plan_id=1,
        plan_version=1,
        backfilled=False,
        trigger_family="plan_stop_close",
        trigger_key="v1:85000:r1",
        session=date(2026, 10, 29),
        close=Decimal("84000"),
        stop_price=Decimal("85000"),
        average_cost=Decimal("100000"),
        unrealized_pct=Decimal("-16"),
        held_quantity=Decimal(quantity),
        open_sell_quantity=Decimal("0"),
        protected_quantity=Decimal("0"),
        intended_quantity=Decimal(quantity),
        intended_limit_price=limit,
        intended_notional=limit * Decimal(quantity),
        currency="KRW",
        episode_start=date(2026, 10, 29),
        details={"close": "84000"},
    )


async def _create(db_session, decision: CutDecision):
    return await live_module._create_proposal(
        decision,
        per_order_cap=Decimal("2000000"),
        now=NOW,
        session_factory=_factory(db_session),
    )


async def _dispatch(db_session, proposal_id, notifier, revalidate):
    return await dispatch_proposal(
        proposal_id,
        notifier=notifier,
        now=NOW,
        now_fn=lambda: NOW,
        service_factory=_factory(db_session),
        revalidate_fn=revalidate,
    )


async def test_inside_the_caps_a_mechanical_cut_submits_with_no_card(db_session):
    proposal_id = await _create(db_session, _decision(_symbol()))
    notifier = _Notifier()
    revalidate = _Revalidate(_preview())

    await _dispatch(db_session, proposal_id, notifier, revalidate)

    assert revalidate.submits == 1
    assert notifier.approval_cards() == []
    assert len(notifier.auto_cards()) == 1
    group, _rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    assert group.exit_intent == "loss_cut"
    assert group.strategy == MECHANICAL_STRATEGY
    assert isinstance(group.source_asof["auto_approved"], dict)


async def test_a_mechanical_cut_clears_the_real_revalidation_path(db_session):
    """The existing state machine accepts a loss_cut with no human approval."""
    proposal_id = await _create(db_session, _decision(_symbol()))
    notifier = _Notifier()
    orders: list[dict] = []

    async def place_order(**kwargs):
        orders.append(kwargs)
        if kwargs["dry_run"]:
            return _preview()
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "kis-real-path",
        }

    async def revalidate(*, service, proposal_id, now, eligibility_gate):
        return await revalidate_and_submit(
            service=service,
            proposal_id=proposal_id,
            now=now,
            eligibility_gate=eligibility_gate,
            place_order_fn=place_order,
            window_evaluator=allow_known_session,
            now_fn=lambda: NOW,
        )

    await _dispatch(db_session, proposal_id, notifier, revalidate)

    assert [order["dry_run"] for order in orders] == [True, False]
    assert orders[1]["exit_intent"] == "loss_cut"
    assert orders[1]["retrospective_id"] is not None
    assert notifier.approval_cards() == []
    _group, rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    assert rungs[0].state == "resting"


async def test_over_the_per_order_cap_goes_to_the_human_card(db_session):
    proposal_id = await _create(db_session, _decision(_symbol(), quantity="30"))
    notifier = _Notifier()
    revalidate = _Revalidate(_preview(quantity="30"))

    await _dispatch(db_session, proposal_id, notifier, revalidate)

    assert revalidate.submits == 0
    assert len(notifier.approval_cards()) == 1
    group, _rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    assert "per_order_cap_exceeded" in str(
        project_auto_approve_rejections(group.source_asof)
    )


async def test_switch_off_sends_nothing_automatically(db_session, monkeypatch):
    proposal_id = await _create(db_session, _decision(_symbol()))
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", False)
    notifier = _Notifier()
    revalidate = _Revalidate(_preview())

    await _dispatch(db_session, proposal_id, notifier, revalidate)

    assert revalidate.submits == 0
    assert len(notifier.approval_cards()) == 1
    group, _rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    assert "loss_cut_intent" in str(project_auto_approve_rejections(group.source_asof))


async def test_a_loss_cut_with_the_label_but_no_decision_row_stays_carded(db_session):
    """A session can write the strategy label. It cannot write the decision row."""
    symbol = _symbol()
    _status, retrospective = await save_retrospective(
        db_session,
        symbol=symbol,
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="unfilled",
        side="sell",
        trigger_type="stop_loss",
        next_actions=[{"action": "review", "owner": "operator"}],
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="session",
        thesis="non-mechanical loss sell",
        strategy=MECHANICAL_STRATEGY,
        broker_account_id="acct-1",
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=retrospective.id,
        approval_issue_id="operator-card",
        source_asof={MECHANICAL_STRATEGY: {"forged": True}},
        rungs=[RungInput(0, "sell", Decimal("10"), Decimal("83200"), None)],
        now=NOW,
    )
    await db_session.commit()
    notifier = _Notifier()
    revalidate = _Revalidate(_preview())

    await _dispatch(db_session, group.proposal_id, notifier, revalidate)

    assert revalidate.submits == 0
    assert len(notifier.approval_cards()) == 1


async def test_a_second_emission_for_one_trigger_leaves_nothing_behind(db_session):
    decision = _decision(_symbol())

    first = await _create(db_session, decision)
    second = await _create(db_session, decision)

    assert first is not None
    assert second is None
    count = await db_session.execute(
        text("SELECT count(*) FROM review.order_proposals WHERE symbol = :symbol"),
        {"symbol": decision.key.symbol},
    )
    assert count.scalar_one() == 1
    row = await MechanicalLossCutStore(db_session).live_decision_for_proposal(first)
    assert row is not None
    assert row.intended_quantity == decision.intended_quantity
