from __future__ import annotations

import ast
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.caller_identity import get_caller_agent_id
from app.models.order_proposals import (
    OrderProposalApprovalEvent,
)
from app.models.trading import UserRole
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalPublication,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.loss_cut_approval import (
    LossCutApprovalRejected,
    LossCutApprovalService,
)
from app.services.order_proposals.service import RungInput
from app.telegram_contract import TelegramMethodResult


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _successful_publication() -> ApprovalPublication:
    return ApprovalPublication.published(
        payload_chars=100,
        method_result=TelegramMethodResult(
            ok=True,
            message_id=8181,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=100,
        ),
    )


async def _seed_loss_cut_proposal(
    db_session,
    monkeypatch,
    *,
    now: datetime,
    include_account_scope: bool = True,
):
    symbol = f"LC{uuid.uuid4().hex[:8].upper()}"
    retro = SimpleNamespace(
        id=8842,
        symbol=symbol,
        trigger_type="stop_loss",
        created_at=now - timedelta(minutes=5),
        lesson="손절 기준을 늦추지 않는다",
    )

    async def fake_lookup(session, retrospective_id):
        assert retrospective_id == retro.id
        return retro

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_us",
        account_mode="toss_live",
        broker_account_id=(
            f"fixture-{uuid.uuid4().hex[:10]}" if include_account_scope else None
        ),
        side="sell",
        order_type="limit",
        proposer="b1-test",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("99"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=retro.id,
        approval_issue_id="ROB-1285",
        now=now,
        valid_until=now + timedelta(minutes=15),
    )
    first_nonce = "initial-b1"
    await service.set_approval_nonce(group.proposal_id, first_nonce)
    group, _ = await service.get_proposal(group.proposal_id)
    attempt_id = uuid.uuid4()
    binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce=first_nonce,
        attempt_id=attempt_id,
        card_kind=ApprovalCardKind.MANUAL,
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
        publication=_successful_publication(),
        chat_id="fixture-chat",
        now=now,
    )
    assert result.ok
    await db_session.commit()
    return group, retro


def _preview(state: dict[str, str], retro: SimpleNamespace):
    async def fake_preview(**kwargs):
        observed_at = kwargs["now"]
        return {
            "rungs": [
                {
                    "rung_index": 0,
                    "current_price": state["current_price"],
                    "avg_buy_price": "200",
                    "loss_pct": "-50.00",
                    "loss_cut_slip_band": "98",
                    "requested_quantity": "1",
                    "limit_price": "99",
                    "observed_sellable_qty": state["sellable_quantity"],
                    "observed_total_qty": state["total_quantity"],
                    "observed_locked_qty": state["locked_quantity"],
                    "fill_distance": {"pct": state["fill_distance"]},
                    "warnings": [],
                    "quote_observed_at": observed_at.isoformat(),
                }
            ],
            "retrospective_id": retro.id,
            "lesson_excerpt": retro.lesson,
            "retrospective_trigger_type": retro.trigger_type,
            "retrospective_created_at": retro.created_at.isoformat(),
            "exit_reason": "stop_loss",
            "observed_at": observed_at.isoformat(),
        }

    return fake_preview


def _state() -> dict[str, str]:
    return {
        "current_price": "100",
        "sellable_quantity": "3",
        "total_quantity": "4",
        "locked_quantity": "1",
        "fill_distance": "-1",
    }


@pytest.mark.asyncio
async def test_web_two_step_revalidates_records_actor_and_never_transitions_rung(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    clock = _Clock(now + timedelta(seconds=1))
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(_state(), retro),
        clock=clock,
        nonce_factory=lambda: "web-confirmation-nonce",
        ceremony_factory=lambda: "c" * 48,
    )

    begin = await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=701,
        actor_role=UserRole.trader,
    )
    await db_session.commit()
    assert begin.next_step == "confirm"
    assert begin.evidence.can_begin is True
    browser_payload = json.dumps(begin.model_dump(mode="json"), sort_keys=True)
    assert "initial-b1" not in browser_payload
    assert "web-confirmation-nonce" not in browser_payload

    clock.value += timedelta(seconds=1)
    confirmed = await service.confirm(
        proposal_id=group.proposal_id,
        ceremony_id=begin.ceremony_id,
        actor_user_id=701,
        actor_role=UserRole.trader,
    )
    await db_session.commit()

    assert confirmed.status == "validated_no_execution"
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.approved_by_channel == "web"
    assert refreshed.approved_by_subject == "701"
    assert refreshed.approval_nonce_used_at == clock.value
    assert [rung.state for rung in rungs] == ["pending_approval"]
    events = list(
        (
            await db_session.execute(
                select(OrderProposalApprovalEvent)
                .where(OrderProposalApprovalEvent.proposal_pk == group.id)
                .order_by(OrderProposalApprovalEvent.observed_at)
            )
        )
        .scalars()
        .all()
    )
    assert [(event.step, event.outcome) for event in events] == [
        ("begin", "accepted"),
        ("confirm", "accepted"),
    ]
    assert all(event.nonce_digest for event in events)
    audit_payload = json.dumps(
        [event.evidence_snapshot for event in events], sort_keys=True
    )
    assert "initial-b1" not in audit_payload
    assert "web-confirmation-nonce" not in audit_payload


@pytest.mark.asyncio
async def test_web_confirm_rejects_different_principal_without_consuming_nonce(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    clock = _Clock(now + timedelta(seconds=1))
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(_state(), retro),
        clock=clock,
        nonce_factory=lambda: "principal-confirmation-nonce",
        ceremony_factory=lambda: "p" * 48,
    )
    begin = await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=801,
        actor_role=UserRole.trader,
    )
    proposal_id = group.proposal_id
    await db_session.commit()

    with pytest.raises(
        OrderProposalError, match="^loss_cut_confirmation_principal_mismatch$"
    ):
        await service.confirm(
            proposal_id=group.proposal_id,
            ceremony_id=begin.ceremony_id,
            actor_user_id=802,
            actor_role=UserRole.trader,
        )
    await db_session.rollback()

    refreshed, _ = await OrderProposalsService(db_session).get_proposal(proposal_id)
    assert refreshed.approval_nonce_used_at is None
    assert refreshed.approved_by_channel is None


@pytest.mark.asyncio
async def test_confirm_price_change_consumes_nonce_and_requires_new_ceremony(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    state = _state()
    clock = _Clock(now + timedelta(seconds=1))
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(state, retro),
        clock=clock,
        nonce_factory=lambda: "changed-evidence-nonce",
        ceremony_factory=lambda: "e" * 48,
    )
    begin = await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=901,
        actor_role=UserRole.trader,
    )
    await db_session.commit()
    state["current_price"] = "101"
    clock.value += timedelta(seconds=1)

    with pytest.raises(
        LossCutApprovalRejected,
        match="^loss_cut_confirmation_scope_or_evidence_changed$",
    ):
        await service.confirm(
            proposal_id=group.proposal_id,
            ceremony_id=begin.ceremony_id,
            actor_user_id=901,
            actor_role=UserRole.trader,
        )
    await db_session.commit()

    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.approval_nonce_used_at == clock.value
    assert refreshed.approved_by_channel is None
    assert [rung.state for rung in rungs] == ["pending_approval"]
    confirm_event = (
        await db_session.execute(
            select(OrderProposalApprovalEvent).where(
                OrderProposalApprovalEvent.proposal_pk == group.id,
                OrderProposalApprovalEvent.step == "confirm",
            )
        )
    ).scalar_one()
    assert confirm_event.outcome == "needs_reconfirm"


@pytest.mark.asyncio
async def test_confirm_partial_fill_scope_change_consumes_nonce_and_fails_closed(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    state = _state()
    clock = _Clock(now + timedelta(seconds=1))
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(state, retro),
        clock=clock,
        nonce_factory=lambda: "partial-fill-confirmation",
        ceremony_factory=lambda: "f" * 48,
    )
    begin = await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=925,
        actor_role=UserRole.trader,
    )
    await db_session.commit()
    state.update(
        {
            "sellable_quantity": "2.5",
            "total_quantity": "3.5",
            "locked_quantity": "1",
        }
    )
    clock.value += timedelta(seconds=1)

    with pytest.raises(
        LossCutApprovalRejected,
        match="^loss_cut_confirmation_scope_or_evidence_changed$",
    ):
        await service.confirm(
            proposal_id=group.proposal_id,
            ceremony_id=begin.ceremony_id,
            actor_user_id=925,
            actor_role=UserRole.trader,
        )
    await db_session.commit()

    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.approval_nonce_used_at == clock.value
    assert refreshed.approved_by_channel is None
    assert [rung.state for rung in rungs] == ["pending_approval"]


@pytest.mark.asyncio
async def test_confirm_guard_failure_consumes_nonce_and_requires_reconfirm(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    calls = 0
    normal_preview = _preview(_state(), retro)

    async def preview_then_block(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OrderProposalError("loss_cut_confirmation_quantity_exceeds_sellable")
        return await normal_preview(**kwargs)

    clock = _Clock(now + timedelta(seconds=1))
    service = LossCutApprovalService(
        db_session,
        preview_fn=preview_then_block,
        clock=clock,
        nonce_factory=lambda: "guard-failure-confirmation",
        ceremony_factory=lambda: "g" * 48,
    )
    begin = await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=951,
        actor_role=UserRole.trader,
    )
    await db_session.commit()
    clock.value += timedelta(seconds=1)

    with pytest.raises(
        LossCutApprovalRejected,
        match="^loss_cut_confirmation_revalidation_failed$",
    ):
        await service.confirm(
            proposal_id=group.proposal_id,
            ceremony_id=begin.ceremony_id,
            actor_user_id=951,
            actor_role=UserRole.trader,
        )
    await db_session.commit()

    refreshed, _ = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.approval_nonce_used_at == clock.value
    assert refreshed.approved_by_channel is None
    event = (
        await db_session.execute(
            select(OrderProposalApprovalEvent).where(
                OrderProposalApprovalEvent.proposal_pk == group.id,
                OrderProposalApprovalEvent.step == "confirm",
            )
        )
    ).scalar_one()
    assert event.outcome == "needs_reconfirm"
    assert event.reason_code == (
        "loss_cut_confirmation_revalidation_failed:"
        "loss_cut_confirmation_quantity_exceeds_sellable"
    )


@pytest.mark.asyncio
async def test_expired_web_ceremony_is_audited_and_does_not_approve(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    clock = _Clock(now + timedelta(seconds=1))
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(_state(), retro),
        clock=clock,
        nonce_factory=lambda: "expiry-confirmation-nonce",
        ceremony_factory=lambda: "x" * 48,
    )
    begin = await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=1001,
        actor_role=UserRole.trader,
    )
    await db_session.commit()
    clock.value += timedelta(seconds=91)

    with pytest.raises(
        LossCutApprovalRejected, match="^loss_cut_confirmation_expired$"
    ):
        await service.confirm(
            proposal_id=group.proposal_id,
            ceremony_id=begin.ceremony_id,
            actor_user_id=1001,
            actor_role=UserRole.trader,
        )
    await db_session.commit()

    refreshed, _ = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    assert refreshed.approved_by_channel is None
    event = (
        await db_session.execute(
            select(OrderProposalApprovalEvent).where(
                OrderProposalApprovalEvent.proposal_pk == group.id,
                OrderProposalApprovalEvent.step == "confirm",
            )
        )
    ).scalar_one()
    assert event.outcome == "expired"


@pytest.mark.asyncio
async def test_cross_channel_first_nonce_has_one_database_winner(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    web_ready = asyncio.Event()
    telegram_ready = asyncio.Event()
    release = asyncio.Event()

    async def blocked_preview(**kwargs):
        web_ready.set()
        await release.wait()
        return await _preview(_state(), retro)(**kwargs)

    async def web_click() -> str:
        async with AsyncSessionLocal() as session:
            service = LossCutApprovalService(
                session,
                preview_fn=blocked_preview,
                clock=lambda: now + timedelta(seconds=1),
                nonce_factory=lambda: "web-race-confirmation",
                ceremony_factory=lambda: "w" * 48,
            )
            try:
                await service.begin(
                    proposal_id=group.proposal_id,
                    actor_user_id=1101,
                    actor_role=UserRole.trader,
                )
                await session.commit()
                return "web"
            except LossCutApprovalRejected:
                await session.commit()
                return "web-rejected"

    async def telegram_click() -> str:
        async with AsyncSessionLocal() as session:
            service = OrderProposalsService(session)
            callback = await service.current_callback_envelope(
                group.proposal_id, action="op"
            )
            telegram_ready.set()
            await release.wait()
            try:
                await service.consume_published_proposal_callback(
                    group.proposal_id,
                    callback=callback,
                    now=now + timedelta(seconds=1),
                )
                await session.commit()
                return "telegram"
            except OrderProposalError:
                await session.rollback()
                return "telegram-rejected"

    web_task = asyncio.create_task(web_click())
    telegram_task = asyncio.create_task(telegram_click())
    await asyncio.wait_for(web_ready.wait(), timeout=5)
    await asyncio.wait_for(telegram_ready.wait(), timeout=5)
    release.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(web_task, telegram_task), timeout=10
    )

    assert sorted(outcomes) in [
        ["telegram", "web-rejected"],
        ["telegram-rejected", "web"],
    ]


@pytest.mark.asyncio
async def test_confirm_calls_target_lock_before_shared_nonce_consumer(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    clock = _Clock(now + timedelta(seconds=1))
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(_state(), retro),
        clock=clock,
        nonce_factory=lambda: "lock-order-confirmation",
        ceremony_factory=lambda: "l" * 48,
    )
    begin = await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=1201,
        actor_role=UserRole.trader,
    )
    await db_session.commit()
    calls: list[str] = []
    original_lock = service._proposals.acquire_target_mutation_lock
    original_consume = service._proposals.consume_published_proposal_callback

    async def logged_lock(*args, **kwargs):
        calls.append("target_lock")
        return await original_lock(*args, **kwargs)

    async def logged_consume(*args, **kwargs):
        calls.append("nonce_consume")
        return await original_consume(*args, **kwargs)

    monkeypatch.setattr(service._proposals, "acquire_target_mutation_lock", logged_lock)
    monkeypatch.setattr(
        service._proposals, "consume_published_proposal_callback", logged_consume
    )
    clock.value += timedelta(seconds=1)
    await service.confirm(
        proposal_id=group.proposal_id,
        ceremony_id=begin.ceremony_id,
        actor_user_id=1201,
        actor_role=UserRole.trader,
    )
    await db_session.commit()

    assert calls == ["target_lock", "nonce_consume"]


@pytest.mark.asyncio
async def test_total_requested_quantity_cannot_exceed_fresh_sellable_quantity(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    state = _state()
    state["sellable_quantity"] = "0.5"
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(state, retro),
        clock=lambda: now + timedelta(seconds=1),
    )

    with pytest.raises(
        OrderProposalError, match="^loss_cut_confirmation_quantity_exceeds_sellable$"
    ):
        await service.get_proposal_evidence(proposal_id=group.proposal_id)


@pytest.mark.asyncio
async def test_proposal_without_exact_broker_account_scope_fails_closed(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(
        db_session,
        monkeypatch,
        now=now,
        include_account_scope=False,
    )
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(_state(), retro),
        clock=lambda: now + timedelta(seconds=1),
    )

    with pytest.raises(
        OrderProposalError, match="^loss_cut_confirmation_account_scope_missing$"
    ):
        await service.get_proposal_evidence(proposal_id=group.proposal_id)


@pytest.mark.asyncio
async def test_preview_uses_server_configured_proposal_agent_identity(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_SUBMIT_AGENT_ID", "server-b1-agent")
    normal_preview = _preview(_state(), retro)

    async def identity_checked_preview(**kwargs):
        assert get_caller_agent_id() == "server-b1-agent"
        return await normal_preview(**kwargs)

    service = LossCutApprovalService(
        db_session,
        preview_fn=identity_checked_preview,
        clock=lambda: now + timedelta(seconds=1),
    )

    evidence = await service.get_proposal_evidence(proposal_id=group.proposal_id)

    assert evidence.can_begin is True
    assert get_caller_agent_id() is None


@pytest.mark.asyncio
async def test_confirmation_replay_is_single_use(db_session, monkeypatch):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    clock = _Clock(now + timedelta(seconds=1))
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(_state(), retro),
        clock=clock,
        nonce_factory=lambda: "single-use-confirmation",
        ceremony_factory=lambda: "s" * 48,
    )
    begin = await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=1301,
        actor_role=UserRole.trader,
    )
    await db_session.commit()
    clock.value += timedelta(seconds=1)
    await service.confirm(
        proposal_id=group.proposal_id,
        ceremony_id=begin.ceremony_id,
        actor_user_id=1301,
        actor_role=UserRole.trader,
    )
    await db_session.commit()

    with pytest.raises(OrderProposalError, match="^nonce_replay$"):
        await service.confirm(
            proposal_id=group.proposal_id,
            ceremony_id=begin.ceremony_id,
            actor_user_id=1301,
            actor_role=UserRole.trader,
        )


@pytest.mark.asyncio
async def test_approval_events_reject_update_and_delete(db_session, monkeypatch):
    now = datetime.now(UTC)
    group, retro = await _seed_loss_cut_proposal(db_session, monkeypatch, now=now)
    service = LossCutApprovalService(
        db_session,
        preview_fn=_preview(_state(), retro),
        clock=lambda: now + timedelta(seconds=1),
        nonce_factory=lambda: "append-only-confirmation",
        ceremony_factory=lambda: "a" * 48,
    )
    await service.begin(
        proposal_id=group.proposal_id,
        actor_user_id=1351,
        actor_role=UserRole.trader,
    )
    group_pk = group.id
    await db_session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(OrderProposalApprovalEvent)
                .where(OrderProposalApprovalEvent.proposal_pk == group_pk)
                .values(outcome="rejected")
            )
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(
                delete(OrderProposalApprovalEvent).where(
                    OrderProposalApprovalEvent.proposal_pk == group_pk
                )
            )
        await session.rollback()


def _assert_b1_has_no_execution_call(tree: ast.AST) -> None:
    forbidden = {"revalidate_and_submit", "place_order", "submit_order"}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not (forbidden & (called | imported))


def test_b1_execution_absence_assertion_fails_on_inverted_fixture():
    source = Path("app/services/order_proposals/loss_cut_approval.py").read_text()
    _assert_b1_has_no_execution_call(ast.parse(source))

    with pytest.raises(AssertionError):
        _assert_b1_has_no_execution_call(ast.parse("revalidate_and_submit()"))


def test_single_use_assertion_fails_on_double_accept_fixture():
    def assert_one_accept(outcomes: list[str]) -> None:
        assert outcomes.count("accepted") == 1

    assert_one_accept(["accepted", "rejected"])
    with pytest.raises(AssertionError):
        assert_one_accept(["accepted", "accepted"])


@pytest.mark.asyncio
async def test_b0_evidence_is_read_only_and_keeps_accounts_separate(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    before = len(
        list((await db_session.execute(select(OrderProposalApprovalEvent))).scalars())
    )
    holdings = [
        SimpleNamespace(
            symbol="AAPL",
            quantity=2.0,
            averageCost=200.0,
            valueNative=200.0,
            sellableQuantity=1.0,
            pendingSellQuantity=1.0,
            source="kis",
            sourceOfTruth=True,
            accountId="kis-one",
            market="us",
        ),
        SimpleNamespace(
            symbol="AAPL",
            quantity=3.0,
            averageCost=150.0,
            valueNative=300.0,
            sellableQuantity=None,
            pendingSellQuantity=0.0,
            source="toss_api",
            sourceOfTruth=False,
            accountId="toss-two",
            market="us",
        ),
    ]

    class FakeHome:
        async def get_home(self, *, user_id, include_paper):
            assert user_id == 1401
            assert include_paper is False
            return SimpleNamespace(holdings=holdings)

    service = LossCutApprovalService(
        db_session,
        home_service=FakeHome(),
        clock=lambda: now,
    )
    evidence = await service.get_symbol_evidence(symbol="AAPL", user_id=1401)

    assert evidence.can_begin is False
    assert [position.account_ref for position in evidence.positions] == [
        "kis-one",
        "toss-two",
    ]
    assert [
        evidence.loss.label,
        evidence.reason.label,
        evidence.r931.label,
        evidence.consensus.label,
        evidence.watch.label,
    ] == ["손실률", "사유 판정", "R-931", "컨센서스", "워치 맥락"]
    assert evidence.reason.status == "missing"
    assert evidence.r931.status == "unavailable"
    after = len(
        list((await db_session.execute(select(OrderProposalApprovalEvent))).scalars())
    )
    assert after == before
