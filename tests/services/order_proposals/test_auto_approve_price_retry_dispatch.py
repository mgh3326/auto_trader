"""#1067 -- dispatch-level lifecycle of the price fallback and the 30s retry.

Offline only: fake notifier, fake quote reader, fake Toss place/preview seam,
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
from app.services.order_proposals.auto_approve_price_fallback import (
    AUTO_APPROVE_PRICE_RETRY_KEY,
    PriceFallback,
)
from app.services.order_proposals.dispatch import (
    AUTO_APPROVE_PRICE_RETRY_SCHEDULED,
    AUTO_APPROVE_PRICE_RETRY_SUPERSEDED,
    dispatch_proposal,
    is_price_retry_non_failure,
    send_proposal_for_approval,
)
from app.services.order_proposals.dispatch_contract import ApprovalDispatchState
from app.services.order_proposals.revalidation import (
    RungOutcome,
    revalidate_and_submit,
)
from app.services.order_proposals.service import RungInput
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session

CHAT_ID = "chat-1067"
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
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", CHAT_ID
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


def _factory(db_session):
    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _create_case(db_session, *, account_mode="toss_live", symbol="035720"):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
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


class _FakeRevalidate:
    """Mimics revalidate_and_submit's gate contract with a scripted preview."""

    def __init__(self, previews: list[dict]) -> None:
        self.previews = list(previews)
        self.calls = 0
        self.submits = 0

    async def __call__(self, *, service, proposal_id, now, eligibility_gate):
        self.calls += 1
        preview = self.previews.pop(0)
        group, rungs = await service.get_proposal(proposal_id)
        decision = await eligibility_gate(
            group=group, rung=rungs[0], preview=preview, now=now
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
            broker_order_id=f"toss-{self.submits}",
            correlation_id=f"corr-{self.submits}",
            idempotency_key=f"idem-{self.submits}",
            approval_hash_digest=f"digest-{self.submits}",
            now=now,
        )
        return [RungOutcome(0, "submitted_resting", {})]


def _preview_without_price():
    return {
        "success": True,
        "price": "32750",
        "quantity": "3",
        "price_context_message": _PRICE_CONTEXT_MESSAGE,
    }


def _preview_with_price(price="33800"):
    return {
        "success": True,
        "current_price": price,
        "price": "32750",
        "quantity": "3",
    }


class _Fallback:
    def __init__(self, *results: PriceFallback) -> None:
        self.results = list(results)
        self.calls: list[tuple] = []

    async def __call__(self, *, symbol, market):
        self.calls.append((symbol, market))
        return self.results.pop(0)


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, str]] = []

    def __call__(self, proposal_id, token):
        self.calls.append((proposal_id, token))


def _latest_rejection(group):
    attempts = project_auto_approve_rejections(group.source_asof)
    return attempts[-1]["rungs"][0] if attempts else None


async def _dispatch(db_session, group, notifier, revalidate, **kwargs):
    return await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=kwargs.pop("now", NOW),
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
    revalidate = _FakeRevalidate([_preview_without_price()])
    fallback = _Fallback(PriceFallback.observed(Decimal("33800")))
    scheduler = _Scheduler()
    notifier = _Notifier()

    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        price_fallback_fn=fallback,
        price_retry_scheduler=scheduler,
    )

    assert result.ok is True
    assert fallback.calls == [("035720", "equity_kr")]
    assert revalidate.submits == 1
    assert scheduler.calls == []
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"
    [eligibility] = refreshed.source_asof["auto_approved"]["eligibility"]
    assert eligibility["eligible"] is True
    assert eligibility["price_source"] == "kis_quote_fallback"
    assert eligibility["current_price"] == "33800"
    assert "자동 접수됨" in notifier.sent[-1][0]
    assert AUTO_APPROVE_PRICE_RETRY_KEY not in refreshed.source_asof


@pytest.mark.asyncio
async def test_golden_present_preview_price_never_reads_fallback(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_with_price()])

    async def forbidden_fallback(**_kwargs):
        raise AssertionError("a present preview price must not read a quote")

    def forbidden_scheduler(*_args):
        raise AssertionError("a present preview price must not schedule a retry")

    result = await _dispatch(
        db_session,
        group,
        _Notifier(),
        revalidate,
        price_fallback_fn=forbidden_fallback,
        price_retry_scheduler=forbidden_scheduler,
    )

    assert result.ok is True
    refreshed, _ = await service.get_proposal(group.proposal_id)
    [eligibility] = refreshed.source_asof["auto_approved"]["eligibility"]
    assert eligibility["price_source"] == "toss_preview"


@pytest.mark.asyncio
async def test_fallback_price_failing_a_gate_goes_to_the_card_now(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_without_price()])
    scheduler = _Scheduler()
    notifier = _Notifier()

    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        price_fallback_fn=_Fallback(PriceFallback.observed(Decimal("32700"))),
        price_retry_scheduler=scheduler,
    )

    assert result.ok is True  # the manual approval card
    assert revalidate.submits == 0
    assert scheduler.calls == []  # a real gate verdict is not retried
    refreshed, _ = await service.get_proposal(group.proposal_id)
    rung = _latest_rejection(refreshed)
    assert rung["reason_code"] == "marketable_not_resting"
    assert rung["inputs"]["price_source"] == "kis_quote_fallback"
    assert "주문 제안 승인" in notifier.sent[-1][0]


@pytest.mark.asyncio
async def test_non_toss_missing_price_is_unchanged(db_session):
    service, group = await _create_case(db_session, account_mode="kis_live")
    revalidate = _FakeRevalidate([_preview_without_price()])

    async def forbidden_fallback(**_kwargs):
        raise AssertionError("kis_live never reads the Toss fallback")

    scheduler = _Scheduler()
    notifier = _Notifier()
    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        price_fallback_fn=forbidden_fallback,
        price_retry_scheduler=scheduler,
    )

    assert result.ok is True
    assert scheduler.calls == []
    refreshed, _ = await service.get_proposal(group.proposal_id)
    rung = _latest_rejection(refreshed)
    assert rung["reason_code"] == "price_or_quantity_missing"
    assert "price_fallback_reason" not in rung["inputs"]


# ---------------------------------------------------------------------------
# AC2 / AC5: failed fallback -> one deferred re-evaluation, then the card
# ---------------------------------------------------------------------------


async def _first_pass_deferred(db_session, service, group, notifier, revalidate):
    scheduler = _Scheduler()
    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        price_fallback_fn=_Fallback(PriceFallback.failed("quote_stale")),
        price_retry_scheduler=scheduler,
    )
    assert result.state is ApprovalDispatchState.PENDING
    assert result.failure_code == AUTO_APPROVE_PRICE_RETRY_SCHEDULED
    assert is_price_retry_non_failure(result) is True
    assert notifier.sent == []  # no card yet
    [(proposal_id, token)] = scheduler.calls
    assert proposal_id == group.proposal_id
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "pending_approval"
    marker = refreshed.source_asof[AUTO_APPROVE_PRICE_RETRY_KEY]
    assert marker["state"] == "scheduled"
    assert marker["token"] == token
    assert datetime.fromisoformat(marker["due_at"]) - datetime.fromisoformat(
        marker["scheduled_at"]
    ) == timedelta(seconds=30)
    rung = _latest_rejection(refreshed)
    assert rung["reason_code"] == "price_or_quantity_missing"
    assert rung["inputs"]["price_fallback_reason"] == "quote_stale"
    assert rung["inputs"]["price_context_message"] == _PRICE_CONTEXT_MESSAGE
    return token


@pytest.mark.asyncio
async def test_still_missing_after_retry_sends_card_with_both_diagnostics(
    db_session,
):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_without_price(), _preview_without_price()])
    notifier = _Notifier()
    token = await _first_pass_deferred(db_session, service, group, notifier, revalidate)

    rescheduler = _Scheduler()
    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        now=NOW + timedelta(seconds=30),
        price_fallback_fn=_Fallback(PriceFallback.failed("session_not_live")),
        price_retry_scheduler=rescheduler,
        price_retry_token=token,
    )

    assert result.ok is True
    assert rescheduler.calls == []  # exactly one re-evaluation, never two
    assert revalidate.calls == 2
    assert revalidate.submits == 0
    [(text, _keyboard)] = notifier.sent
    assert "주문 제안 승인" in text
    assert "`price_or_quantity_missing` / `session_not_live`" in text
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.source_asof[AUTO_APPROVE_PRICE_RETRY_KEY]["state"] == "consumed"
    rung = _latest_rejection(refreshed)
    assert rung["inputs"]["price_retry_reevaluation"] is True
    assert rung["inputs"]["price_fallback_reason"] == "session_not_live"
    assert rung["inputs"]["price_context_message"] == _PRICE_CONTEXT_MESSAGE


@pytest.mark.asyncio
async def test_retry_with_price_back_auto_approves_once(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_without_price(), _preview_with_price()])
    notifier = _Notifier()
    token = await _first_pass_deferred(db_session, service, group, notifier, revalidate)

    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        now=NOW + timedelta(seconds=30),
        price_retry_token=token,
    )

    assert result.ok is True
    assert revalidate.submits == 1
    assert "자동 접수됨" in notifier.sent[-1][0]
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"
    [eligibility] = refreshed.source_asof["auto_approved"]["eligibility"]
    assert eligibility["price_source"] == "toss_preview"
    assert eligibility["price_retry_reevaluation"] is True


# ---------------------------------------------------------------------------
# AC4: the retry can never double-dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_run_with_same_token_is_a_no_op(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_without_price(), _preview_with_price()])
    notifier = _Notifier()
    token = await _first_pass_deferred(db_session, service, group, notifier, revalidate)

    first = await _dispatch(
        db_session, group, notifier, revalidate, price_retry_token=token
    )
    assert first.ok is True

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("a consumed retry must never revalidate again")

    second = await _dispatch(
        db_session, group, notifier, must_not_revalidate, price_retry_token=token
    )
    card_only = await _dispatch(
        db_session,
        group,
        notifier,
        must_not_revalidate,
        price_retry_token=token,
        price_retry_card_only=True,
    )
    for replay in (second, card_only):
        assert replay.state is ApprovalDispatchState.FAILED_SUPERSEDED
        assert replay.failure_code == AUTO_APPROVE_PRICE_RETRY_SUPERSEDED
        assert is_price_retry_non_failure(replay) is True
    assert revalidate.submits == 1
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_wrong_or_seeded_token_never_runs(db_session):
    service = OrderProposalsService(db_session)
    # A proposer-supplied marker is overwritten by the first pass and can
    # never be consumed with a guessed token.
    group = await service.create_proposal(
        symbol="035720",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="t1067-fixture",
        thesis="underwater_support_net add at support",
        broker_account_id=f"t1067-{uuid.uuid4()}",
        rungs=[RungInput(0, "buy", Decimal("3"), Decimal("32750"), None)],
        source_asof={
            AUTO_APPROVE_PRICE_RETRY_KEY: {"state": "scheduled", "token": "seeded"}
        },
        now=NOW,
        valid_until=NOW + timedelta(hours=6),
    )
    await db_session.commit()
    revalidate = _FakeRevalidate([_preview_without_price()])
    notifier = _Notifier()
    token = await _first_pass_deferred(db_session, service, group, notifier, revalidate)
    assert token != "seeded"

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("a foreign token must not revalidate")

    for bad in ("seeded", "0" * 32, ""):
        replay = await _dispatch(
            db_session, group, notifier, must_not_revalidate, price_retry_token=bad
        )
        assert replay.failure_code == AUTO_APPROVE_PRICE_RETRY_SUPERSEDED
    assert notifier.sent == []
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.source_asof[AUTO_APPROVE_PRICE_RETRY_KEY]["state"] == "scheduled"


@pytest.mark.asyncio
async def test_manual_redispatch_during_wait_supersedes_the_retry(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_without_price()])
    notifier = _Notifier()
    token = await _first_pass_deferred(db_session, service, group, notifier, revalidate)

    manual = await send_proposal_for_approval(
        group.proposal_id,
        notifier=notifier,
        now=NOW + timedelta(seconds=5),
        service_factory=_factory(db_session),
        window_evaluator=allow_known_session,
        now_fn=lambda: NOW,
    )
    assert manual.ok is True
    published = len(notifier.sent)

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("a published card must stop the auto retry")

    replay = await _dispatch(
        db_session, group, notifier, must_not_revalidate, price_retry_token=token
    )
    assert replay.failure_code == AUTO_APPROVE_PRICE_RETRY_SUPERSEDED
    assert len(notifier.sent) == published  # the retry published nothing


@pytest.mark.asyncio
async def test_cancelled_wait_sends_the_card_without_re_evaluating(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_without_price()])
    notifier = _Notifier()
    token = await _first_pass_deferred(db_session, service, group, notifier, revalidate)

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("card-only must not re-evaluate")

    result = await _dispatch(
        db_session,
        group,
        notifier,
        must_not_revalidate,
        price_retry_token=token,
        price_retry_card_only=True,
    )
    assert result.ok is True
    [(text, _keyboard)] = notifier.sent
    assert "주문 제안 승인" in text
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.source_asof[AUTO_APPROVE_PRICE_RETRY_KEY]["state"] == "consumed"


@pytest.mark.asyncio
async def test_master_gate_off_during_wait_only_sends_card_after_consuming(
    monkeypatch, db_session
):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_without_price()])
    notifier = _Notifier()
    token = await _first_pass_deferred(db_session, service, group, notifier, revalidate)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", False)

    first = await _dispatch(
        db_session, group, notifier, revalidate, price_retry_token=token
    )
    published = len(notifier.sent)
    second = await _dispatch(
        db_session, group, notifier, revalidate, price_retry_token=token
    )
    assert first.ok is True
    assert published >= 1
    assert "주문 제안 승인" in notifier.sent[0][0]
    assert second.failure_code == AUTO_APPROVE_PRICE_RETRY_SUPERSEDED
    assert len(notifier.sent) == published
    assert revalidate.calls == 1  # only the first pass evaluated


@pytest.mark.asyncio
async def test_scheduler_failure_falls_back_to_the_card_now(db_session):
    service, group = await _create_case(db_session)
    revalidate = _FakeRevalidate([_preview_without_price()])
    notifier = _Notifier()

    def broken_scheduler(*_args):
        raise RuntimeError("no running loop")

    result = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        price_fallback_fn=_Fallback(PriceFallback.failed("quote_timeout")),
        price_retry_scheduler=broken_scheduler,
    )
    assert result.ok is True
    assert "주문 제안 승인" in notifier.sent[-1][0]


@pytest.mark.asyncio
async def test_real_revalidation_retry_submits_to_broker_exactly_once(db_session):
    """End to end through revalidate_and_submit with a fake Toss seam."""
    service, group = await _create_case(db_session)
    previews = [False, True]  # first preview lacks the price, retry has it
    submits: list[dict] = []

    async def place_order(**kwargs):
        if kwargs["dry_run"]:
            has_price = previews.pop(0)
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
        fresh_group, _ = await kwargs["service"].get_proposal(kwargs["proposal_id"])
        stamp = (await allow_known_session(fresh_group, now=NOW)).policy_stamp
        return await revalidate_and_submit(
            **kwargs,
            place_order_fn=place_order,
            buying_power_claimer=enough_buying_power,
            window_evaluator=allow_known_session,
            expected_policy_stamp=stamp,
            now_fn=lambda: NOW,
        )

    notifier = _Notifier()
    scheduler = _Scheduler()
    first = await _dispatch(
        db_session,
        group,
        notifier,
        revalidate,
        price_fallback_fn=_Fallback(PriceFallback.failed("quote_unavailable")),
        price_retry_scheduler=scheduler,
    )
    assert first.failure_code == AUTO_APPROVE_PRICE_RETRY_SCHEDULED
    assert submits == []
    [(_proposal_id, token)] = scheduler.calls

    second = await _dispatch(
        db_session, group, notifier, revalidate, price_retry_token=token
    )
    assert second.ok is True
    assert len(submits) == 1

    async def must_not_revalidate(**_kwargs):
        raise AssertionError("no second evaluation")

    third = await _dispatch(
        db_session, group, notifier, must_not_revalidate, price_retry_token=token
    )
    assert third.failure_code == AUTO_APPROVE_PRICE_RETRY_SUPERSEDED
    assert len(submits) == 1
    _refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"
