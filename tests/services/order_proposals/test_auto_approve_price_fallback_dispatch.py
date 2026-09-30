"""#1067 -- dispatch-level behaviour of the KIS quote fallback.

Offline only: fake notifier, fake quote reader, fake Toss preview/submit seam,
and the run-owned test database. No broker socket is opened.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals.auto_approve import AutoApproveLimits
from app.services.order_proposals.auto_approve_audit import (
    project_auto_approve_rejections,
)
from app.services.order_proposals.auto_approve_price_fallback import PriceFallback
from app.services.order_proposals.dispatch import dispatch_proposal
from app.services.order_proposals.revalidation import (
    RungOutcome,
    revalidate_and_submit,
)
from app.services.order_proposals.service import RungInput
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session

NOW = datetime(2026, 9, 30, 0, 13, tzinfo=UTC)  # 09:13 KST
_PRICE_CONTEXT_MESSAGE = "Failed to retrieve current price for 035720: boom"
_LIMITS = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("2000000"),
    daily_cap=Decimal("5000000"),
    policy_version="2026-09-30.1",
    mode="expanded",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("90"),
)


@pytest.fixture(autouse=True)
def _auto_toss(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"chat-1067-{uuid.uuid4().hex}",
    )
    monkeypatch.setattr(dispatch_module, "limits_for_market", lambda _m: _LIMITS)
    monkeypatch.setattr(
        dispatch_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(
        revalidation_module, "evaluate_approval_window", allow_known_session
    )


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self._message_id = 9100

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        self._message_id += 1
        self.sent.append((text, inline_keyboard))
        return TelegramMethodResult(
            ok=True,
            message_id=self._message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        return TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def send_auto_veto_card_mirror(self, **kwargs):
        return True

    def approval_cards(self) -> list[str]:
        return [text for text, _kb in self.sent if "주문 제안 승인" in text]


def _factory(db_session):
    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _create_case(db_session, *, account_mode="toss_live"):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="035720",
        market="equity_kr",
        account_mode=account_mode,
        side="buy",
        order_type="limit",
        proposer="t1067-fixture",
        thesis="underwater_support_net add at support",
        broker_account_id=f"t1067-{uuid.uuid4()}",
        rungs=[RungInput(0, "buy", Decimal("3"), Decimal("32750"), None)],
        now=NOW,
        valid_until=NOW + timedelta(hours=6),
    )
    await db_session.commit()
    return service, group


def _preview_without_price():
    return {
        "success": True,
        "price": "32750",
        "quantity": "3",
        "price_context_message": _PRICE_CONTEXT_MESSAGE,
    }


def _preview_with_price(price="33800"):
    return {"success": True, "current_price": price, "price": "32750", "quantity": "3"}


class _FakeRevalidate:
    """Mimics revalidate_and_submit's gate contract with one scripted preview."""

    def __init__(self, preview: dict) -> None:
        self.preview = preview
        self.calls = 0
        self.submits = 0

    async def __call__(self, *, service, proposal_id, now, eligibility_gate):
        self.calls += 1
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
            broker_order_id="toss-1",
            correlation_id="corr-1",
            idempotency_key="idem-1",
            approval_hash_digest="digest-1",
            now=now,
        )
        return [RungOutcome(0, "submitted_resting", {})]


class _Fallback:
    def __init__(self, result: PriceFallback) -> None:
        self.result = result
        self.calls: list[tuple] = []

    async def __call__(self, *, symbol, market):
        self.calls.append((symbol, market))
        return self.result


def _latest_rejection(group):
    attempts = project_auto_approve_rejections(group.source_asof)
    return attempts[-1]["rungs"][0] if attempts else None


async def _dispatch(db_session, group, notifier, revalidate, **kwargs):
    return await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=NOW,
        service_factory=_factory(db_session),
        revalidate_fn=revalidate,
        window_evaluator=allow_known_session,
        now_fn=lambda: NOW,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AC1: the fallback supplies the price and the proposal auto-approves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_preview_price_auto_approves_on_fresh_kis_quote(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate(_preview_without_price())
    fallback = _Fallback(PriceFallback.observed(Decimal("33800")))
    notifier = _Notifier()

    result = await _dispatch(
        db_session, group, notifier, revalidate, price_fallback_fn=fallback
    )

    assert result.ok is True
    assert fallback.calls == [("035720", "equity_kr")]
    assert revalidate.submits == 1
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"
    [eligibility] = refreshed.source_asof["auto_approved"]["eligibility"]
    assert eligibility["eligible"] is True
    assert eligibility["price_source"] == "kis_quote_fallback"
    assert eligibility["current_price"] == "33800"
    assert "자동 접수됨" in notifier.sent[-1][0]
    assert "auto_approve_price_retry" not in refreshed.source_asof


# ---------------------------------------------------------------------------
# AC4 golden: a present preview price never reads the fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_golden_present_preview_price_never_reads_fallback(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate(_preview_with_price())

    async def forbidden_fallback(**_kwargs):
        raise AssertionError("a present preview price must not read a quote")

    result = await _dispatch(
        db_session,
        group,
        _Notifier(),
        revalidate,
        price_fallback_fn=forbidden_fallback,
    )

    assert result.ok is True
    refreshed, _ = await service.get_proposal(group.proposal_id)
    [eligibility] = refreshed.source_asof["auto_approved"]["eligibility"]
    assert eligibility["price_source"] == "toss_preview"


# ---------------------------------------------------------------------------
# AC2 (option B): a failed/stale fallback goes to the ordinary card at once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["quote_stale", "session_not_live", "quote_timeout", "quote_unavailable"]
)
async def test_failed_fallback_sends_the_card_immediately_with_both_diagnostics(
    db_session, reason
):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate(_preview_without_price())
    notifier = _Notifier()

    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        price_fallback_fn=_Fallback(PriceFallback.failed(reason)),
    )

    assert result.ok is True  # the ordinary manual card, in this same call
    assert revalidate.calls == 1 and revalidate.submits == 0
    [card] = notifier.approval_cards()
    assert f"`price_or_quantity_missing` / `{reason}`" in card
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "pending_approval"
    assert refreshed.approval_dispatch_state == "sent_current"
    rung = _latest_rejection(refreshed)
    assert rung["reason_code"] == "price_or_quantity_missing"
    assert rung["inputs"]["missing_inputs"] == ["current_price"]
    assert rung["inputs"]["price_fallback_reason"] == reason
    assert rung["inputs"]["price_context_message"] == _PRICE_CONTEXT_MESSAGE
    assert "auto_approve_price_retry" not in refreshed.source_asof


@pytest.mark.asyncio
async def test_fallback_read_raising_is_quote_unavailable_and_carded(db_session):
    service, group = await _create_case(db_session)
    notifier = _Notifier()

    async def broken_fallback(**_kwargs):
        raise RuntimeError("socket closed")

    result = await _dispatch(
        db_session,
        group,
        notifier,
        _FakeRevalidate(_preview_without_price()),
        price_fallback_fn=broken_fallback,
    )

    assert result.ok is True
    refreshed, _ = await service.get_proposal(group.proposal_id)
    rung = _latest_rejection(refreshed)
    assert rung["inputs"]["price_fallback_reason"] == "quote_unavailable"
    assert rung["inputs"]["price_context_message"] == _PRICE_CONTEXT_MESSAGE


@pytest.mark.asyncio
async def test_fallback_price_failing_a_gate_goes_to_the_card(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate(_preview_without_price())
    notifier = _Notifier()

    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        price_fallback_fn=_Fallback(PriceFallback.observed(Decimal("32700"))),
    )

    assert result.ok is True
    assert revalidate.submits == 0
    refreshed, _ = await service.get_proposal(group.proposal_id)
    rung = _latest_rejection(refreshed)
    assert rung["reason_code"] == "marketable_not_resting"
    assert rung["inputs"]["price_source"] == "kis_quote_fallback"
    assert rung["inputs"]["price_context_message"] == _PRICE_CONTEXT_MESSAGE
    assert len(notifier.approval_cards()) == 1


@pytest.mark.asyncio
async def test_non_toss_missing_price_is_unchanged(db_session):
    service, group = await _create_case(db_session, account_mode="kis_live")

    async def forbidden_fallback(**_kwargs):
        raise AssertionError("kis_live never reads the Toss fallback")

    result = await _dispatch(
        db_session,
        group,
        _Notifier(),
        _FakeRevalidate(_preview_without_price()),
        price_fallback_fn=forbidden_fallback,
    )

    assert result.ok is True
    refreshed, _ = await service.get_proposal(group.proposal_id)
    rung = _latest_rejection(refreshed)
    assert rung["reason_code"] == "price_or_quantity_missing"
    assert "price_fallback_reason" not in rung["inputs"]
    assert "price_source" not in rung["inputs"]


# ---------------------------------------------------------------------------
# End to end through the real revalidate_and_submit (fake Toss seam)
# ---------------------------------------------------------------------------


def _real_revalidate(submits: list[dict], *, has_price: bool):
    async def place_order(**kwargs):
        if kwargs["dry_run"]:
            preview = {
                "success": True,
                "approval_hash": "preview-token",
                "price": "32750",
                "quantity": "3",
                "estimated_value": "98250",
                "fee": "0",
                "payload_preview": {
                    "clientOrderId": kwargs["proposal_client_order_id"],
                    "price": "32750",
                    "quantity": "3",
                },
            }
            if has_price:
                preview["current_price"] = "33800"
            else:
                preview["price_context_message"] = _PRICE_CONTEXT_MESSAGE
            return preview
        submits.append(kwargs)
        return {"success": True, "status": "resting", "broker_order_id": "toss-e2e"}

    async def enough_buying_power(**_kwargs):
        return Decimal("10000000")

    async def revalidate(**kwargs):
        # Thread the approval-window stamp exactly as dispatch does for the
        # real revalidate_and_submit (see test_dispatch's same pattern).
        group, _ = await kwargs["service"].get_proposal(kwargs["proposal_id"])
        stamp = (await allow_known_session(group, now=NOW)).policy_stamp
        return await revalidate_and_submit(
            **kwargs,
            place_order_fn=place_order,
            buying_power_claimer=enough_buying_power,
            window_evaluator=allow_known_session,
            expected_policy_stamp=stamp,
            now_fn=lambda: NOW,
        )

    return revalidate


@pytest.mark.asyncio
async def test_real_revalidation_fallback_success_submits_exactly_once(db_session):
    service, group = await _create_case(db_session)
    submits: list[dict] = []
    notifier = _Notifier()

    result = await _dispatch(
        db_session,
        group,
        notifier,
        _real_revalidate(submits, has_price=False),
        price_fallback_fn=_Fallback(PriceFallback.observed(Decimal("33800"))),
    )

    assert result.ok is True
    assert len(submits) == 1
    _refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("a submitted proposal must not dispatch twice")

    again = await _dispatch(db_session, group, notifier, must_not_revalidate)
    assert again.failure_code == "proposal_not_pending_approval"
    assert len(submits) == 1


@pytest.mark.asyncio
async def test_real_revalidation_fallback_failure_cards_without_submit(db_session):
    service, group = await _create_case(db_session)
    submits: list[dict] = []
    notifier = _Notifier()

    result = await _dispatch(
        db_session,
        group,
        notifier,
        _real_revalidate(submits, has_price=False),
        price_fallback_fn=_Fallback(PriceFallback.failed("quote_stale")),
    )

    assert result.ok is True
    assert submits == []
    assert len(notifier.approval_cards()) == 1
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "pending_approval"
    rung = _latest_rejection(refreshed)
    assert rung["inputs"]["price_fallback_reason"] == "quote_stale"
    assert rung["inputs"]["price_context_message"] == _PRICE_CONTEXT_MESSAGE
