import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.core.db import AsyncSessionLocal
from app.core.timezone import KST
from app.models.review import TossLiveOrderLedger
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import service as service_module
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalPublication,
    CallbackEnvelope,
    DispatchBinding,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.errors import (
    OrderProposalError,
    OrderProposalInvalidStateTransition,
    OrderProposalNotFound,
    OrderProposalUnsupportedTargetAction,
)
from app.services.order_proposals.service import ExpirySweepResult, RungInput
from app.telegram_contract import TelegramMethodResult


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


async def _publish_fixture_card(
    service: OrderProposalsService,
    group,
    *,
    nonce: str,
    card_kind: ApprovalCardKind = ApprovalCardKind.MANUAL,
    now: datetime | None = None,
    message_id: int = 4242,
) -> DispatchBinding:
    published_at = now or datetime.now(UTC)
    attempt_id = uuid.uuid4()
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
        now=published_at,
        payload_chars=100,
        context_message_count=0,
    )
    result = await service.finish_approval_dispatch(
        group.proposal_id,
        attempt_id=attempt_id,
        publication=_successful_publication(message_id),
        chat_id="fixture-chat",
        now=published_at,
    )
    assert result.ok
    return binding


def _batch_callback(
    batch,
    *,
    nonce: str | None = None,
    attempt_id: uuid.UUID | None = None,
) -> CallbackEnvelope:
    return CallbackEnvelope(
        action="ba",
        subject_short=str(batch.batch_id)[:8],
        attempt_id=attempt_id or batch.approval_dispatch_attempt_id,
        membership_revision=batch.membership_revision,
        membership_digest=batch.membership_digest,
        nonce=nonce or batch.approval_nonce,
    )


def _target_snapshot_payload(**overrides):
    payload = {
        "broker_order_id": "manual-upbit-1",
        "symbol": "KRW-AVAX",
        "side": "sell",
        "order_type": "limit",
        "limit_price": "42000",
        "remaining_quantity": "3.5",
        "status": "open",
        "observed_at": "2026-07-11T08:23:00+00:00",
    }
    payload.update(overrides)
    return payload


def _target_action_create_kwargs(action: str, **overrides):
    kwargs = {
        "symbol": "KRW-AVAX",
        "market": "crypto",
        "account_mode": "upbit",
        "side": "sell",
        "order_type": "limit",
        "proposer": "p",
        "action": action,
        "target_broker_order_id": "manual-upbit-1",
        "target_order_snapshot": _target_snapshot_payload(),
        "rungs": [RungInput(0, "sell", Decimal("3.5"), Decimal("42000"), None)],
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_target_mutation_lock_serializes_same_broker_order():
    target = SimpleNamespace(
        action="replace",
        account_mode="upbit",
        market="crypto",
        broker_account_id=None,
        target_broker_order_id=f"manual-{uuid.uuid4()}",
    )

    async with (
        AsyncSessionLocal() as first_session,
        AsyncSessionLocal() as second_session,
    ):
        first = OrderProposalsService(first_session)
        second = OrderProposalsService(second_session)

        assert await first.acquire_target_mutation_lock(target) is True
        waiter = asyncio.create_task(second.acquire_target_mutation_lock(target))
        await asyncio.sleep(0.05)
        assert waiter.done() is False

        await first_session.commit()
        assert await asyncio.wait_for(waiter, timeout=1) is True
        await second_session.rollback()


@pytest.mark.asyncio
async def test_place_still_allows_multiple_rungs_and_persists_normalized_action(
    db_session,
):
    group = await OrderProposalsService(db_session).create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        action="place",
        source_asof={"quote_asof": "2026-07-11T08:23:00+00:00"},
        rungs=[
            RungInput(0, "buy", Decimal("1"), Decimal("70000"), None),
            RungInput(1, "buy", Decimal("1"), Decimal("69000"), None),
        ],
    )

    assert group.action == "place"
    assert group.target_broker_order_id is None
    assert group.source_asof == {"quote_asof": "2026-07-11T08:23:00+00:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_target_actions_require_exactly_one_rung(db_session, action):
    with pytest.raises(OrderProposalError, match="exactly one rung"):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(
                action,
                rungs=[
                    RungInput(0, "sell", Decimal("3.5"), Decimal("42000"), None),
                    RungInput(1, "sell", Decimal("1"), Decimal("41000"), None),
                ],
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"target_broker_order_id": None},
        {"target_order_snapshot": None},
    ],
)
async def test_target_actions_require_target_broker_evidence(
    db_session, action, overrides
):
    with pytest.raises(OrderProposalError, match="requires target broker evidence"):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(action, **overrides)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
@pytest.mark.parametrize("status", ["filled", "cancelled", "expired", "rejected"])
async def test_target_actions_reject_non_open_target_at_create(
    db_session, action, status
):
    with pytest.raises(OrderProposalError, match="target broker order must be open"):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(
                action,
                target_order_snapshot=_target_snapshot_payload(
                    status=status, remaining_quantity="0"
                ),
            )
        )


@pytest.mark.asyncio
async def test_place_rejects_target_broker_evidence(db_session):
    with pytest.raises(OrderProposalError, match="cannot target a broker order"):
        await OrderProposalsService(db_session).create_proposal(
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="p",
            target_broker_order_id="old-1",
            target_order_snapshot=_target_snapshot_payload(),
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("70000"), None)],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_target_actions_reject_unsupported_account_market_tuple(
    db_session, action
):
    with pytest.raises(
        OrderProposalError, match="unsupported account_mode/market/action"
    ):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(action, account_mode="kis_mock")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
@pytest.mark.parametrize("market", ["equity_kr", "equity_us"])
async def test_target_actions_allow_toss_live_equities(db_session, action, market):
    """ROB-972: toss_live x equity_kr/us must be a supported cancel/replace
    target -- previously SUPPORTED_TARGET_ACTIONS had no toss_live entry at
    all, so this raised 'cancel unsupported for toss_live/equity_kr' even
    though the create-time contract message claimed it was allowed.
    """
    symbol = "005930" if market == "equity_kr" else "AAPL"
    group = await OrderProposalsService(db_session).create_proposal(
        **_target_action_create_kwargs(
            action,
            account_mode="toss_live",
            market=market,
            symbol=symbol,
            target_order_snapshot=_target_snapshot_payload(symbol=symbol),
        )
    )
    await db_session.commit()
    assert group.account_mode == "toss_live"
    assert group.market == market
    assert group.action == action
    assert group.target_broker_order_id == "manual-upbit-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_target_action_rejection_message_matches_capability_set(
    db_session, action
):
    """The rejection message must be derived from the same set the action
    actually checks membership against -- not a different action's set (the
    ROB-972 bug: cancel/replace rejections reused place's hardcoded message
    and falsely advertised toss_live support they didn't have).
    """
    with pytest.raises(OrderProposalError) as exc_info:
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(action, account_mode="kis_mock")
        )
    assert str(exc_info.value) == (
        f"unsupported account_mode/market/action: kis_mock/crypto/{action} "
        "(allowed: kis_live×equity_kr|equity_us, "
        "toss_live×equity_kr|equity_us, upbit×crypto; "
        "market aliases kr→equity_kr, us→equity_us)"
    )


@pytest.mark.asyncio
async def test_target_action_rejection_supported_matrix_is_action_accurate(db_session):
    with pytest.raises(OrderProposalUnsupportedTargetAction) as exc_info:
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(
                "cancel", account_mode="toss_live", market="crypto"
            )
        )
    err = exc_info.value
    assert err.requested == {
        "account_mode": "toss_live",
        "market": "crypto",
        "action": "cancel",
    }
    assert {"account_mode": "toss_live", "market": "equity_kr"} in (
        err.supported_matrix["cancel"]
    )
    assert {"account_mode": "toss_live", "market": "equity_us"} in (
        err.supported_matrix["cancel"]
    )
    assert {"account_mode": "toss_live", "market": "crypto"} not in (
        err.supported_matrix["cancel"]
    )
    # replace shares cancel's capability set today but is derived
    # independently -- assert both explicitly so a future divergence between
    # the two actions' capability sets is caught here.
    assert err.supported_matrix["replace"] == err.supported_matrix["cancel"]
    assert err.supported_matrix["place"] == err.supported_matrix["cancel"]


@pytest.mark.asyncio
async def test_cancel_rejects_rung_that_differs_from_target_snapshot(db_session):
    with pytest.raises(
        OrderProposalError, match="cancel rung must equal target broker snapshot"
    ):
        await OrderProposalsService(db_session).create_proposal(
            **_target_action_create_kwargs(
                "cancel",
                rungs=[RungInput(0, "sell", Decimal("3.4"), Decimal("42000"), None)],
            )
        )


@pytest.mark.asyncio
async def test_replace_persists_target_snapshot_and_allows_independent_proposals(
    db_session,
):
    service = OrderProposalsService(db_session)
    first = await service.create_proposal(
        **_target_action_create_kwargs("replace", source_asof={"origin": "manual"})
    )
    second = await service.create_proposal(
        **_target_action_create_kwargs(
            "replace",
            target_broker_order_id="manual-upbit-2",
            target_order_snapshot=_target_snapshot_payload(
                broker_order_id="manual-upbit-2"
            ),
        )
    )

    assert first.proposal_id != second.proposal_id
    assert first.action == second.action == "replace"
    assert first.target_broker_order_id == "manual-upbit-1"
    assert first.source_asof == {
        "origin": "manual",
        "target_order_snapshot": _target_snapshot_payload(),
    }
    assert first.payload_hash != second.payload_hash


async def _create_single_rung(
    db_session,
    *,
    symbol: str = "A",
    account_mode: str = "kis_live",
    market: str = "equity_kr",
):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market=market,
        account_mode=account_mode,
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    return service, group


async def _create_cancel_proposal(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(**_target_action_create_kwargs("cancel"))
    await db_session.commit()
    return service, group


async def _drive_to_submitting(service, proposal_id):
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(proposal_id, 0, new_state=state)


async def _record_ack(service, proposal_id, *, now: datetime):
    await _drive_to_submitting(service, proposal_id)
    return await service.record_ack(
        proposal_id,
        0,
        broker_order_id=f"B-ACK-{proposal_id}",
        correlation_id=f"corr-ack-{proposal_id}",
        idempotency_key=f"idem-ack-{proposal_id}",
        approval_hash_digest=f"digest-ack-{proposal_id}",
        now=now,
    )


async def _record_resting(service, proposal_id, *, now: datetime):
    await _drive_to_submitting(service, proposal_id)
    return await service.record_resting(
        proposal_id,
        0,
        broker_order_id=f"B-REST-{proposal_id}",
        correlation_id=f"corr-rest-{proposal_id}",
        idempotency_key=f"idem-rest-{proposal_id}",
        approval_hash_digest=f"digest-rest-{proposal_id}",
        now=now,
    )


def _retro(*, symbol="005930", trigger_type="stop_loss", created_at=None):
    return SimpleNamespace(
        symbol=symbol,
        trigger_type=trigger_type,
        created_at=created_at or datetime.now(UTC),
    )


def _loss_cut_create_kwargs(*, now: datetime):
    return {
        "symbol": "005930",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "side": "sell",
        "order_type": "limit",
        "proposer": "p",
        "rungs": [RungInput(0, "sell", Decimal("1"), Decimal("65000"), None)],
        "exit_intent": "loss_cut",
        "exit_reason": "stop_loss",
        "retrospective_id": 42,
        "approval_issue_id": None,
        "now": now,
    }


@pytest.mark.asyncio
async def test_create_defaults_valid_until_to_next_kst_midnight(db_session):
    now = datetime(2026, 7, 11, 14, 30, tzinfo=KST)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("70000"), None)],
        now=now,
    )
    assert group.valid_until == datetime(2026, 7, 12, 0, 0, tzinfo=KST)


@pytest.mark.asyncio
async def test_loss_cut_requires_all_group_fields_without_paperclip_lookup(
    db_session,
):
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError, match="exit_reason"):
        await service.create_proposal(
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            side="sell",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("65000"), None)],
            exit_intent="loss_cut",
            retrospective_id=42,
            approval_issue_id="ROB-800",
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"retrospective_id": None}, "retrospective_id"),
        ({"exit_reason": None}, "exit_reason"),
        ({"exit_intent": "emergency"}, "unknown exit_intent"),
        # ROB-929 code review: every submit path still only recognizes
        # exit_intent="loss_cut" -- accepting "defensive_trim" at create would
        # produce a proposal that TTL-floors/lists fine but dies in
        # revalidation once approved. Fail closed until execution-side
        # support for defensive_trim exists (separate issue).
        ({"exit_intent": "defensive_trim"}, "unknown exit_intent"),
    ],
)
async def test_loss_cut_required_fields_fail_closed(db_session, overrides, message):
    service = OrderProposalsService(db_session)
    kwargs = _loss_cut_create_kwargs(now=datetime.now(UTC))
    kwargs.update(overrides)
    with pytest.raises(OrderProposalError, match=message):
        await service.create_proposal(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retro", "message"),
    [
        (None, "not found"),
        (_retro(symbol="000660"), "symbol mismatch"),
        (_retro(trigger_type="fill"), "trigger_type"),
        (_retro(created_at=datetime.now(UTC) - timedelta(hours=73)), "stale"),
    ],
)
async def test_loss_cut_retrospective_validation(
    db_session, monkeypatch, retro, message
):
    async def fake_lookup(session, retrospective_id):
        return retro

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    with pytest.raises(OrderProposalError, match=message):
        await OrderProposalsService(db_session).create_proposal(
            **_loss_cut_create_kwargs(now=datetime.now(UTC))
        )


@pytest.mark.asyncio
async def test_valid_loss_cut_persists_exact_group_binding(db_session, monkeypatch):
    async def fake_lookup(session, retrospective_id):
        return _retro()

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    group = await OrderProposalsService(db_session).create_proposal(
        **_loss_cut_create_kwargs(now=datetime.now(UTC))
    )
    assert (
        group.exit_intent,
        group.exit_reason,
        group.retrospective_id,
        group.approval_issue_id,
    ) == ("loss_cut", "stop_loss", 42, None)


@pytest.mark.asyncio
async def test_loss_cut_preserves_optional_approval_issue_as_audit_note(
    db_session, monkeypatch
):
    async def fake_lookup(session, retrospective_id):
        return _retro()

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    kwargs = _loss_cut_create_kwargs(now=datetime.now(UTC))
    kwargs["approval_issue_id"] = "legacy Paperclip note / operator context"

    group = await OrderProposalsService(db_session).create_proposal(**kwargs)

    assert group.approval_issue_id == "legacy Paperclip note / operator context"


@pytest.mark.asyncio
async def test_upbit_crypto_loss_cut_is_valid(db_session, monkeypatch):
    async def fake_lookup(session, retrospective_id):
        return _retro(symbol="KRW-DOT")

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    group = await OrderProposalsService(db_session).create_proposal(
        symbol="KRW-DOT",
        market="crypto",
        account_mode="upbit",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("0.1"), Decimal("3200"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-800",
        now=datetime.now(UTC),
    )
    assert group.exit_intent == "loss_cut"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "symbol"),
    [("equity_kr", "005930"), ("equity_us", "AAPL")],
)
async def test_toss_live_loss_cut_supports_kr_and_us(
    db_session, monkeypatch, market, symbol
):
    async def fake_lookup(session, retrospective_id):
        return _retro(symbol=symbol)

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    group = await OrderProposalsService(db_session).create_proposal(
        symbol=symbol,
        market=market,
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("100"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-800",
        now=datetime.now(UTC),
    )

    assert (group.account_mode, group.market, group.exit_intent) == (
        "toss_live",
        market,
        "loss_cut",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "side", "order_type", "message"),
    [
        ("crypto", "sell", "limit", "unsupported account_mode/market/action"),
        ("equity_kr", "buy", "limit", "loss_cut requires side='sell'"),
        ("equity_us", "sell", "market", "loss_cut requires order_type='limit'"),
    ],
)
async def test_toss_live_loss_cut_rejects_invalid_contract(
    db_session, monkeypatch, market, side, order_type, message
):
    symbol = "KRW-BTC" if market == "crypto" else "005930"

    async def fake_lookup(session, retrospective_id):
        return _retro(symbol=symbol)

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    with pytest.raises(OrderProposalError, match=message):
        await OrderProposalsService(db_session).create_proposal(
            symbol=symbol,
            market=market,
            account_mode="toss_live",
            side=side,
            order_type=order_type,
            proposer="p",
            rungs=[
                RungInput(
                    0,
                    side,
                    Decimal("1"),
                    Decimal("100") if order_type == "limit" else None,
                    None,
                )
            ],
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            approval_issue_id="ROB-800",
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retro", "message"),
    [
        (_retro(symbol="AAPL"), "symbol mismatch"),
        (
            _retro(created_at=datetime.now(UTC) - timedelta(hours=73)),
            "stale \\(> 72h old\\)",
        ),
    ],
    ids=["symbol_mismatch", "stale"],
)
async def test_toss_live_loss_cut_rejects_invalid_retrospective(
    db_session, monkeypatch, retro, message
):
    async def fake_lookup(session, retrospective_id):
        return retro

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    with pytest.raises(OrderProposalError, match=message):
        await OrderProposalsService(db_session).create_proposal(
            symbol="005930",
            market="equity_kr",
            account_mode="toss_live",
            side="sell",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("100"), None)],
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            approval_issue_id="ROB-858",
            now=datetime.now(UTC),
        )


#  -- ROB-929: defensive proposal (loss_cut/defensive_trim) TTL floor --------


@pytest.mark.asyncio
async def test_loss_cut_valid_until_floors_up_to_next_approval_window_when_shorter(
    db_session, monkeypatch
):
    async def fake_lookup(session, retrospective_id):
        return _retro(symbol="AAPL")

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    now = datetime(2026, 7, 15, 20, 0, tzinfo=KST)
    kwargs = _loss_cut_create_kwargs(now=now)
    kwargs["symbol"] = "AAPL"
    kwargs["market"] = "equity_us"
    kwargs["valid_until"] = now + timedelta(minutes=1)

    group = await OrderProposalsService(db_session).create_proposal(**kwargs)

    assert group.valid_until == datetime(2026, 7, 15, 23, 30, tzinfo=KST)


@pytest.mark.asyncio
async def test_loss_cut_valid_until_keeps_caller_supplied_longer_window(
    db_session, monkeypatch
):
    async def fake_lookup(session, retrospective_id):
        return _retro()

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    now = datetime(2026, 7, 15, 20, 0, tzinfo=KST)
    longer_window = datetime(2026, 7, 16, 10, 0, tzinfo=KST)
    kwargs = _loss_cut_create_kwargs(now=now)
    kwargs["valid_until"] = longer_window

    group = await OrderProposalsService(db_session).create_proposal(**kwargs)

    assert group.valid_until == longer_window


@pytest.mark.asyncio
async def test_loss_cut_valid_until_default_also_respects_floor(
    db_session, monkeypatch
):
    """No valid_until supplied at all -- the default must not fall short either."""

    async def fake_lookup(session, retrospective_id):
        return _retro(symbol="AAPL")

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    kwargs = _loss_cut_create_kwargs(now=datetime(2026, 7, 15, 20, 0, tzinfo=KST))
    kwargs["symbol"] = "AAPL"
    kwargs["market"] = "equity_us"

    group = await OrderProposalsService(db_session).create_proposal(**kwargs)

    # Default (next KST midnight) already exceeds the US window end here, so
    # the default itself, not the floor, wins -- this locks that in.
    assert group.valid_until == datetime(2026, 7, 16, 0, 0, tzinfo=KST)


@pytest.mark.asyncio
async def test_defensive_trim_exit_intent_is_rejected_pending_execution_support(
    db_session,
):
    """exit_intent="defensive_trim" is deliberately NOT accepted at create time.

    ROB-929 code review: every submit path (order_execution.py,
    orders_kis_variants.py, orders_toss_variants.py) still only recognizes
    exit_intent="loss_cut" -- accepting "defensive_trim" here would create a
    proposal that TTL-floors and lists correctly but dies in revalidation the
    moment it's approved (a zombie lane). The TTL-floor helper and the
    expiry-handoff read surface stay forward-compat aware of "defensive_trim"
    (see defensive_ttl.DEFENSIVE_EXIT_INTENTS) for whenever execution-side
    support lands as a separate issue, but create must keep failing closed
    until then.
    """
    with pytest.raises(OrderProposalError, match="unknown exit_intent"):
        await OrderProposalsService(db_session).create_proposal(
            symbol="AAPL",
            market="equity_us",
            account_mode="kis_live",
            side="sell",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("200"), None)],
            exit_intent="defensive_trim",
            now=datetime(2026, 7, 15, 20, 0, tzinfo=KST),
        )


@pytest.mark.asyncio
async def test_general_proposal_valid_until_is_not_floored_by_approval_window(
    db_session,
):
    now = datetime(2026, 7, 15, 20, 0, tzinfo=KST)
    short_valid_until = now + timedelta(minutes=1)

    group = await OrderProposalsService(db_session).create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("200"), None)],
        valid_until=short_valid_until,
        now=now,
    )

    assert group.exit_intent is None
    assert group.valid_until == short_valid_until


@pytest.mark.asyncio
async def test_create_and_get_multi_rung(db_session):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="operator:sess-1",
        thesis="support bounce",
        strategy="ladder",
        rungs=[
            RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None),
            RungInput(1, "buy", Decimal("5"), Decimal("2200000"), None),
        ],
    )
    await db_session.commit()
    assert group.lifecycle_state == "proposed"
    assert group.root_proposal_id == group.proposal_id
    assert group.payload_hash and len(group.payload_hash) == 64

    fetched, rungs = await svc.get_proposal(group.proposal_id)
    assert fetched.id == group.id
    assert [r.rung_index for r in rungs] == [0, 1]
    assert all(r.state == "pending_approval" for r in rungs)


@pytest.mark.asyncio
async def test_get_missing_raises(db_session):
    svc = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalNotFound):
        await svc.get_proposal(uuid.uuid4())


@pytest.mark.asyncio
async def test_rung_transition_enforces_state_machine(db_session):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    # illegal: pending_approval -> filled
    with pytest.raises(OrderProposalInvalidStateTransition):
        await svc.transition_rung(group.proposal_id, 0, new_state="filled")


@pytest.mark.asyncio
async def test_resolve_proposal_prefix_checks_all_lifecycle_states(
    db_session, monkeypatch
):
    colliding_prefix = uuid.uuid4().hex[:8]
    unique_prefix = uuid.uuid4().hex[:8]
    proposal_ids = iter(
        [
            uuid.UUID(f"{colliding_prefix}-0000-0000-0000-000000000001"),
            uuid.UUID(f"{colliding_prefix}-0000-0000-0000-000000000002"),
            uuid.UUID(f"{unique_prefix}-0000-0000-0000-000000000003"),
        ]
    )
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: next(proposal_ids))
    svc = OrderProposalsService(db_session)
    created = []
    for symbol in ("A", "B", "C"):
        created.append(
            await svc.create_proposal(
                symbol=symbol,
                market="equity_kr",
                account_mode="kis_live",
                side="buy",
                order_type="limit",
                proposer="p",
                rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
            )
        )
    created[0].lifecycle_state = "superseded"
    created[1].lifecycle_state = "voided"
    await db_session.commit()

    assert await svc.resolve_proposal_id_prefix(colliding_prefix) is None
    assert await svc.resolve_proposal_id_prefix(unique_prefix) == created[2].proposal_id


@pytest.mark.asyncio
async def test_replacement_lineage_supersedes_original(db_session):
    svc = OrderProposalsService(db_session)
    superseded_at = datetime(2026, 7, 14, 1, 2, 3, tzinfo=UTC)
    original = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await svc.set_approval_nonce(original.proposal_id, "old-approval-nonce")
    await db_session.commit()
    replacement = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2340000"), None)],
        supersedes_proposal_id=original.proposal_id,
        now=superseded_at,
    )
    await db_session.commit()
    orig_after, original_rungs = await svc.get_proposal(original.proposal_id)
    assert orig_after.lifecycle_state == "superseded"
    assert orig_after.superseded_by_proposal_id == replacement.proposal_id
    assert orig_after.approval_nonce_used_at == superseded_at
    assert [rung.state for rung in original_rungs] == ["superseded"]
    assert replacement.root_proposal_id == original.root_proposal_id
    assert replacement.payload_hash != original.payload_hash


@pytest.mark.asyncio
async def test_supersede_leaves_submitted_rung_unchanged(db_session):
    svc = OrderProposalsService(db_session)
    original = await svc.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(0, "sell", Decimal("1"), Decimal("250"), None),
            RungInput(1, "sell", Decimal("1"), Decimal("260"), None),
        ],
    )
    for state in ("revalidating", "approved", "submitting", "resting"):
        await svc.transition_rung(original.proposal_id, 0, new_state=state)
    await svc.transition_rung(original.proposal_id, 1, new_state="revalidating")
    await svc.mark_needs_reconfirm(
        original.proposal_id, 1, now=datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    )

    replacement = await svc.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("2"), Decimal("270"), None)],
        supersedes_proposal_id=original.proposal_id,
        now=datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )

    superseded, rungs = await svc.get_proposal(original.proposal_id)
    assert superseded.superseded_by_proposal_id == replacement.proposal_id
    assert [rung.state for rung in rungs] == ["resting", "superseded"]


@pytest.mark.asyncio
async def test_superseded_group_blocks_both_approval_nonce_consumers(db_session):
    svc, original = await _create_single_rung(db_session)
    await svc.set_approval_nonce(original.proposal_id, "old-nonce")
    replacement = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("101"), None)],
        supersedes_proposal_id=original.proposal_id,
        now=datetime(2026, 7, 14, 1, 10, tzinfo=UTC),
    )
    expected = f"proposal_superseded_by:{replacement.proposal_id}"

    with pytest.raises(OrderProposalError, match=f"^{expected}$"):
        await svc.consume_approval_nonce(
            original.proposal_id,
            "old-nonce",
            now=datetime(2026, 7, 14, 1, 11, tzinfo=UTC),
        )
    with pytest.raises(OrderProposalError, match=f"^{expected}$"):
        await svc.consume_loss_cut_confirmation(
            original.proposal_id,
            "old-nonce",
            telegram_user_id="777",
            now=datetime(2026, 7, 14, 1, 11, tzinfo=UTC),
        )
    with pytest.raises(OrderProposalError, match=f"^{expected}$"):
        await svc.set_approval_nonce(original.proposal_id, "late-dispatch-nonce")

    refreshed, _ = await svc.get_proposal(original.proposal_id)
    assert refreshed.approval_nonce == "old-nonce"
    assert refreshed.approval_nonce_used_at == datetime(2026, 7, 14, 1, 10, tzinfo=UTC)


@pytest.mark.asyncio
async def test_terminal_group_blocks_both_approval_nonce_consumers(db_session):
    svc, group = await _create_single_rung(db_session)
    terminalized_at = datetime(2026, 7, 14, 1, 12, tzinfo=UTC)
    await svc.set_approval_nonce(group.proposal_id, "terminal-nonce")
    await svc.void_proposal(
        group.proposal_id,
        reason="operator void",
        now=terminalized_at,
    )

    with pytest.raises(OrderProposalError, match="^proposal_terminal:voided$"):
        await svc.consume_approval_nonce(
            group.proposal_id,
            "terminal-nonce",
            now=terminalized_at + timedelta(seconds=1),
        )
    with pytest.raises(OrderProposalError, match="^proposal_terminal:voided$"):
        await svc.consume_loss_cut_confirmation(
            group.proposal_id,
            "terminal-nonce",
            telegram_user_id="777",
            now=terminalized_at + timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_approval_nonce_mismatch_and_reset(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)

    result = await service.set_approval_nonce(group.proposal_id, "nonce-1")
    assert result is None
    await _publish_fixture_card(
        service,
        group,
        nonce="nonce-1",
        now=now - timedelta(seconds=1),
    )
    await service.consume_approval_nonce(group.proposal_id, "nonce-1", now=now)

    with pytest.raises(OrderProposalError, match="^nonce_mismatch$"):
        await service.consume_approval_nonce(group.proposal_id, "wrong-nonce", now=now)

    await service.set_approval_nonce(group.proposal_id, "nonce-2")
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce == "nonce-2"
    assert refreshed.approval_nonce_used_at is None
    assert refreshed.approval_dispatch_state == "failed"


@pytest.mark.asyncio
async def test_approval_nonce_replay_blocked(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 1, tzinfo=UTC)
    await service.set_approval_nonce(group.proposal_id, "nonce-1")
    await _publish_fixture_card(
        service,
        group,
        nonce="nonce-1",
        now=now - timedelta(seconds=1),
    )
    await db_session.commit()

    consumed = await service.consume_approval_nonce(
        group.proposal_id, "nonce-1", now=now
    )
    assert consumed.approval_nonce_used_at == now
    await db_session.commit()

    with pytest.raises(OrderProposalError, match="^nonce_replay$"):
        await service.consume_approval_nonce(
            group.proposal_id, "nonce-1", now=now + timedelta(seconds=1)
        )


@pytest.mark.asyncio
async def test_expire_if_needed_terminalizes_pending_rungs_and_nonce(db_session):
    service, group = await _create_single_rung(db_session)
    await service.set_approval_nonce(group.proposal_id, "nonce")
    group.valid_until = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    assert await service.expire_if_needed(
        group.proposal_id, now=datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    )
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.lifecycle_state == "expired"
    assert refreshed.approval_nonce is None
    assert [r.state for r in rungs] == ["expired"]


@pytest.mark.asyncio
async def test_void_refuses_unverified_rung_without_partial_mutation(db_session):
    service, group = await _create_single_rung(db_session)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id, 0, reason="unknown", now=datetime.now(UTC)
    )
    with pytest.raises(OrderProposalError, match="cannot void"):
        await service.void_proposal(
            group.proposal_id, reason="operator cleanup", now=datetime.now(UTC)
        )


@pytest.mark.asyncio
async def test_void_unverified_with_absent_broker_evidence_records_audit(db_session):
    from app.services.order_proposals.broker_gateway import OperatorVoidEvidence

    service, group = await _create_single_rung(db_session, account_mode="toss_live")
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id,
        0,
        reason="legacy_timeout",
        idempotency_key="tosprop-legacy-1",
        now=now - timedelta(minutes=6),
    )
    ledger_count = (
        await db_session.execute(
            select(func.count())
            .select_from(TossLiveOrderLedger)
            .where(TossLiveOrderLedger.client_order_id == "tosprop-legacy-1")
        )
    ).scalar_one()
    assert ledger_count == 0

    async def broker_evidence(**kwargs):
        assert [rung.rung_index for rung in kwargs["rungs"]] == [0]
        return {
            0: OperatorVoidEvidence(
                "absent",
                "toss GET /orders OPEN + CLOSED "
                "scan_kst=2026-07-11..2026-07-15 combination_matches=0",
            )
        }

    rows = await service.void_proposal(
        group.proposal_id,
        reason="operator cleanup",
        now=now,
        broker_evidence=broker_evidence,
    )
    refreshed, rungs = await service.get_proposal(group.proposal_id)

    assert [row.state for row in rows] == ["voided_local_stale"]
    assert rungs[0].state == "voided_local_stale"
    for audit_reason in (refreshed.void_reason, rungs[0].void_reason):
        assert "operator cleanup" in audit_reason
        assert "outcome=absent" in audit_reason
        assert "GET /orders OPEN + CLOSED" in audit_reason
        assert "scan_kst=2026-07-11..2026-07-15" in audit_reason
        assert "combination_matches=0" in audit_reason
        assert "toss_live_order_ledger rows=0" in audit_reason
    assert rungs[0].void_reason == refreshed.void_reason


@pytest.mark.asyncio
async def test_void_unverified_refuses_when_accepted_toss_ledger_row_exists(db_session):
    from app.services.order_proposals.broker_gateway import OperatorVoidEvidence

    service, group = await _create_single_rung(db_session, account_mode="toss_live")
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id,
        0,
        reason="legacy_timeout",
        idempotency_key="tosprop-ledger-1",
        now=now - timedelta(minutes=6),
    )
    db_session.add(
        TossLiveOrderLedger(
            trade_date=now,
            operation_kind="place",
            market="kr",
            symbol="A",
            side="buy",
            order_type="limit",
            quantity=Decimal("1"),
            price=Decimal("100"),
            client_order_id="tosprop-ledger-1",
            broker_order_id="broker-ledger-1",
            status="accepted",
        )
    )
    await db_session.flush()

    async def broker_evidence(**_kwargs):
        return {
            0: OperatorVoidEvidence(
                "absent", "toss GET /orders OPEN + CLOSED 2026-07-13..2026-07-13"
            )
        }

    with pytest.raises(OrderProposalError, match="toss_live_order_ledger"):
        await service.void_proposal(
            group.proposal_id,
            reason="operator cleanup",
            now=now,
            broker_evidence=broker_evidence,
        )
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"


@pytest.mark.asyncio
async def test_void_unverified_ignores_rejected_toss_ledger_without_order_id(
    db_session,
):
    from app.services.order_proposals.broker_gateway import OperatorVoidEvidence

    service, group = await _create_single_rung(db_session, account_mode="toss_live")
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id,
        0,
        reason="legacy_timeout",
        idempotency_key="tosprop-rejected-ledger-1",
        now=now - timedelta(minutes=6),
    )
    db_session.add(
        TossLiveOrderLedger(
            trade_date=now,
            operation_kind="place",
            market="kr",
            symbol="A",
            side="buy",
            order_type="limit",
            quantity=Decimal("1"),
            price=Decimal("100"),
            client_order_id="tosprop-rejected-ledger-1",
            broker_order_id=None,
            status="rejected",
        )
    )
    await db_session.flush()

    async def broker_evidence(**_kwargs):
        return {
            0: OperatorVoidEvidence(
                "absent", "toss GET /orders OPEN + CLOSED 2026-07-13..2026-07-13"
            )
        }

    rows = await service.void_proposal(
        group.proposal_id,
        reason="operator cleanup",
        now=now,
        broker_evidence=broker_evidence,
    )

    assert [row.state for row in rows] == ["voided_local_stale"]


@pytest.mark.asyncio
@pytest.mark.parametrize("broker_state", ["OPEN", "FILLED"])
async def test_void_unverified_refuses_existing_broker_order(db_session, broker_state):
    from app.services.order_proposals.broker_gateway import OperatorVoidEvidence

    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id,
        0,
        reason="legacy_timeout",
        now=now - timedelta(minutes=6),
    )

    async def broker_evidence(**_kwargs):
        return {
            0: OperatorVoidEvidence(
                "found",
                "toss GET /orders OPEN + CLOSED",
                broker_order_id="broker-1",
                broker_state=broker_state,
            )
        }

    with pytest.raises(OrderProposalError, match=broker_state):
        await service.void_proposal(
            group.proposal_id,
            reason="operator cleanup",
            now=now,
            broker_evidence=broker_evidence,
        )
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("broker down")])
async def test_void_unverified_refuses_broker_lookup_failure(db_session, error):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id,
        0,
        reason="legacy_timeout",
        now=now - timedelta(minutes=6),
    )

    async def broker_evidence(**_kwargs):
        raise error

    with pytest.raises(OrderProposalError, match="broker evidence lookup failed"):
        await service.void_proposal(
            group.proposal_id,
            reason="operator cleanup",
            now=now,
            broker_evidence=broker_evidence,
        )
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"


@pytest.mark.asyncio
async def test_void_unverified_refuses_during_broker_settlement_grace(db_session):
    service, group = await _create_single_rung(db_session, account_mode="toss_live")
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id,
        0,
        reason="recent_timeout",
        idempotency_key="tosprop-recent-1",
        now=now - timedelta(minutes=1),
    )

    async def broker_evidence(**_kwargs):
        pytest.fail("broker lookup must wait until the settlement grace elapses")

    with pytest.raises(OrderProposalError, match="settlement grace"):
        await service.void_proposal(
            group.proposal_id,
            reason="operator cleanup",
            now=now,
            broker_evidence=broker_evidence,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ["rejected", "filled"])
async def test_void_terminal_rung_still_refused(db_session, terminal_state):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    if terminal_state == "rejected":
        await service.record_rejected(
            group.proposal_id, 0, reason="broker rejected", now=now
        )
    else:
        await _record_ack(service, group.proposal_id, now=now)
        await service.transition_rung(group.proposal_id, 0, new_state="filled")

    with pytest.raises(OrderProposalError, match="cannot void proposal"):
        await service.void_proposal(
            group.proposal_id, reason="operator cleanup", now=now
        )


@pytest.mark.asyncio
async def test_void_multi_rung_sets_audit_and_invalidates_nonce(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(0, "buy", Decimal("1"), Decimal("70000"), None),
            RungInput(1, "buy", Decimal("1"), Decimal("69000"), None),
        ],
    )
    await service.set_approval_nonce(group.proposal_id, "nonce")
    rows = await service.void_proposal(
        group.proposal_id, reason="thesis invalidated", now=datetime.now(UTC)
    )
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert [row.state for row in rows] == ["voided", "voided"]
    assert refreshed.lifecycle_state == "voided"
    assert refreshed.no_resubmit is True
    assert refreshed.void_reason == "thesis invalidated"
    assert refreshed.approval_nonce is None


@pytest.mark.asyncio
async def test_expire_if_needed_before_deadline_is_noop(db_session):
    service, group = await _create_single_rung(db_session)
    group.valid_until = datetime.now(UTC) + timedelta(minutes=1)
    assert not await service.expire_if_needed(group.proposal_id, now=datetime.now(UTC))


@pytest.mark.asyncio
async def test_record_approval_sets_telegram_user_and_timestamp(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 1, 30, tzinfo=UTC)

    updated = await service.record_approval(
        group.proposal_id, telegram_user_id="tg-12345", now=now
    )

    assert updated.approved_by_telegram_user_id == "tg-12345"
    assert updated.approved_at == now

    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.approved_by_telegram_user_id == "tg-12345"
    assert refreshed.approved_at == now


@pytest.mark.asyncio
async def test_record_approval_missing_proposal_raises(db_session):
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalNotFound):
        await service.record_approval(
            uuid.uuid4(),
            telegram_user_id="tg-1",
            now=datetime(2026, 7, 10, 9, 1, 31, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_commit_lease_blocks_active_and_reacquires_expired(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 2, tzinfo=UTC)

    assert await service.acquire_commit_lease(
        group.proposal_id, now=now, lease_seconds=10
    )
    assert not await service.acquire_commit_lease(
        group.proposal_id, now=now + timedelta(seconds=9), lease_seconds=10
    )
    assert await service.acquire_commit_lease(
        group.proposal_id, now=now + timedelta(seconds=10), lease_seconds=5
    )

    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.commit_lease_until == now + timedelta(seconds=15)


@pytest.mark.asyncio
async def test_commit_lease_requires_timezone_aware_now(db_session):
    service, group = await _create_single_rung(db_session)

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.acquire_commit_lease(
            group.proposal_id, now=datetime(2026, 7, 10, 9, 2)
        )


@pytest.mark.asyncio
async def test_ack_is_accepted_not_filled_and_records_audit_fields(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 3, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)

    rung = await service.record_ack(
        group.proposal_id,
        0,
        broker_order_id="B1",
        correlation_id="corr1",
        idempotency_key="idem1",
        approval_hash_digest="digest1",
        now=now,
    )

    assert rung.state == "acked"
    assert rung.broker_order_id == "B1"
    assert rung.correlation_id == "corr1"
    assert rung.idempotency_key == "idem1"
    assert rung.approval_hash_digest == "digest1"
    assert rung.validated_at == now
    assert rung.updated_at == now
    assert rung.filled_qty is None


@pytest.mark.asyncio
async def test_resting_is_not_filled_and_records_audit_fields(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 4, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)

    rung = await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id="B2",
        correlation_id="corr2",
        idempotency_key="idem2",
        approval_hash_digest="digest2",
        now=now,
    )

    assert rung.state == "resting"
    assert rung.broker_order_id == "B2"
    assert rung.correlation_id == "corr2"
    assert rung.idempotency_key == "idem2"
    assert rung.approval_hash_digest == "digest2"
    assert rung.validated_at == now
    assert rung.updated_at == now
    assert rung.filled_qty is None


@pytest.mark.asyncio
async def test_record_cancelled_retains_target_id(db_session):
    service, group = await _create_cancel_proposal(db_session)
    now = datetime(2026, 7, 10, 9, 5, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)

    rung = await service.record_cancelled(
        group.proposal_id, 0, broker_order_id="old-1", now=now
    )

    assert rung.state == "cancelled"
    assert rung.broker_order_id == "old-1"
    assert rung.validated_at == now
    assert rung.updated_at == now


@pytest.mark.asyncio
async def test_record_cancelled_rejects_naive_now(db_session):
    service, group = await _create_cancel_proposal(db_session)
    await _drive_to_submitting(service, group.proposal_id)

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.record_cancelled(
            group.proposal_id,
            0,
            broker_order_id="old-1",
            now=datetime.now(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["submitting", "acked", "resting"])
async def test_record_unverified_holds_for_later_evidence(db_session, source_state):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 5, tzinfo=UTC)
    await _drive_to_submitting(service, group.proposal_id)
    if source_state == "acked":
        await service.record_ack(
            group.proposal_id,
            0,
            broker_order_id="B3",
            correlation_id="corr3",
            idempotency_key="idem3",
            approval_hash_digest="digest3",
            now=now,
        )
    elif source_state == "resting":
        await service.record_resting(
            group.proposal_id,
            0,
            broker_order_id="B3",
            correlation_id="corr3",
            idempotency_key="idem3",
            approval_hash_digest="digest3",
            now=now,
        )

    rung = await service.record_unverified(
        group.proposal_id,
        0,
        reason="broker_timeout",
        now=now + timedelta(seconds=1),
    )

    assert rung.state == "unverified"
    assert rung.void_reason == "broker_timeout"
    assert rung.validated_at == now + timedelta(seconds=1)
    assert rung.updated_at == now + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_fill_evidence_books_by_correlation_id(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 6, tzinfo=UTC)
    correlation_id = f"corr9-{group.proposal_id}"
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=f"B9-{group.proposal_id}",
        correlation_id=correlation_id,
        idempotency_key=f"idem9-{group.proposal_id}",
        approval_hash_digest=f"digest9-{group.proposal_id}",
        now=now,
    )
    await db_session.commit()

    booked = await service.record_fill_evidence(
        correlation_id=correlation_id, filled_qty=Decimal("1"), now=now
    )

    assert booked is not None
    assert booked.state == "filled"
    assert booked.filled_qty == Decimal("1")
    assert booked.updated_at == now


@pytest.mark.asyncio
async def test_fill_evidence_prefers_exact_broker_order_over_reused_correlation(
    db_session,
):
    service, older_group = await _create_single_rung(db_session)
    _, target_group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 14, 9, 6, tzinfo=UTC)
    correlation_id = f"reused-correlation-{target_group.proposal_id}"
    older_broker_order_id = f"older-{older_group.proposal_id}"
    target_broker_order_id = f"target-{target_group.proposal_id}"

    for group, broker_order_id in (
        (older_group, older_broker_order_id),
        (target_group, target_broker_order_id),
    ):
        await _drive_to_submitting(service, group.proposal_id)
        await service.record_resting(
            group.proposal_id,
            0,
            broker_order_id=broker_order_id,
            correlation_id=correlation_id,
            idempotency_key=f"idem-{group.proposal_id}",
            approval_hash_digest=f"digest-{group.proposal_id}",
            now=now,
        )
    await db_session.commit()

    booked = await service.record_fill_evidence(
        correlation_id=correlation_id,
        broker_order_id=target_broker_order_id,
        filled_qty=Decimal("0.5"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=1),
    )
    older_rung = (await service.get_proposal(older_group.proposal_id))[1][0]
    target_rung = (await service.get_proposal(target_group.proposal_id))[1][0]

    assert booked is not None
    assert booked.id == target_rung.id
    assert older_rung.state == "resting"
    assert target_rung.state == "partially_filled"


@pytest.mark.asyncio
async def test_fill_evidence_books_upbit_rung_by_identifier(db_session):
    service, group = await _create_single_rung(
        db_session,
        symbol="BTC/KRW",
        account_mode="upbit",
        market="crypto",
    )
    now = datetime(2026, 7, 14, 9, 6, tzinfo=UTC)
    identifier = f"rob868-{group.proposal_id}"
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=f"upbit-{group.proposal_id}",
        correlation_id=f"corr-{group.proposal_id}",
        idempotency_key=identifier,
        approval_hash_digest=f"digest-{group.proposal_id}",
        now=now,
    )
    await db_session.commit()

    booked = await service.record_fill_evidence(
        idempotency_key=identifier,
        filled_qty=Decimal("0.0003"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=1),
        account_mode="upbit",
    )

    assert booked is not None
    assert booked.state == "partially_filled"
    assert booked.filled_qty == Decimal("0.0003")

    duplicate_partial = await service.record_fill_evidence(
        idempotency_key=identifier,
        filled_qty=Decimal("0.0003"),
        terminal_state="partially_filled",
        now=now + timedelta(milliseconds=1500),
        account_mode="upbit",
    )
    stale_partial = await service.record_fill_evidence(
        idempotency_key=identifier,
        filled_qty=Decimal("0.0002"),
        terminal_state="partially_filled",
        now=now + timedelta(milliseconds=1750),
        account_mode="upbit",
    )

    assert duplicate_partial is None
    assert stale_partial is None
    assert booked.filled_qty == Decimal("0.0003")

    filled = await service.record_fill_evidence(
        idempotency_key=identifier,
        filled_qty=Decimal("0.0003"),
        terminal_state="filled",
        now=now + timedelta(seconds=2),
        account_mode="upbit",
    )
    duplicate_reconcile = await service.record_fill_evidence(
        broker_order_id=f"upbit-{group.proposal_id}",
        filled_qty=Decimal("0.0003"),
        terminal_state="filled",
        now=now + timedelta(seconds=3),
        account_mode="upbit",
    )

    assert filled is not None
    assert filled.state == "filled"
    assert duplicate_reconcile is None


@pytest.mark.asyncio
async def test_fill_evidence_books_partial_then_filled_by_broker_order_id(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 7, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await db_session.commit()

    partial = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.25"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=1),
    )
    assert partial is not None
    assert partial.state == "partially_filled"
    assert partial.filled_qty == Decimal("0.25")

    filled = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("1"),
        now=now + timedelta(seconds=2),
    )
    assert filled is not None
    assert filled.state == "filled"
    assert filled.filled_qty == Decimal("1")
    assert filled.updated_at == now + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_fill_evidence_resolves_unverified_rung(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 8, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await service.record_unverified(
        group.proposal_id, 0, reason="unknown", now=now + timedelta(seconds=1)
    )
    await db_session.commit()

    booked = await service.record_fill_evidence(
        correlation_id=acked.correlation_id,
        filled_qty=Decimal("1"),
        now=now + timedelta(seconds=2),
    )

    assert booked is not None
    assert booked.state == "filled"
    assert booked.filled_qty == Decimal("1")


@pytest.mark.asyncio
async def test_fill_evidence_missing_match_is_noop(db_session):
    service, _ = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 9, tzinfo=UTC)
    missing = f"missing-{uuid.uuid4()}"

    assert (
        await service.record_fill_evidence(
            correlation_id=missing, filled_qty=Decimal("1"), now=now
        )
        is None
    )
    assert (
        await service.record_fill_evidence(
            broker_order_id=missing, filled_qty=Decimal("1"), now=now
        )
        is None
    )
    assert await service.record_fill_evidence(filled_qty=Decimal("1"), now=now) is None


@pytest.mark.asyncio
async def test_fill_evidence_records_cancelled_terminal(db_session):
    """ROB-816 PR-3c: broker cancel evidence converges a resting rung to
    `cancelled` (the fa0dab30 canary scenario), with no filled_qty required."""
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
    rested = await _record_resting(service, group.proposal_id, now=now)
    await db_session.commit()

    cancelled = await service.record_fill_evidence(
        correlation_id=rested.correlation_id,
        terminal_state="cancelled",
        now=now + timedelta(seconds=1),
    )

    assert cancelled is not None
    assert cancelled.state == "cancelled"
    assert cancelled.updated_at == now + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_fill_evidence_cancel_after_partial_keeps_filled_qty(db_session):
    """A partially-filled rung that is later cancelled keeps the quantity that
    actually filled — cancel evidence must not zero out the partial fill."""
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 1, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await db_session.commit()
    await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.25"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=1),
    )

    cancelled = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        terminal_state="cancelled",
        now=now + timedelta(seconds=2),
    )

    assert cancelled is not None
    assert cancelled.state == "cancelled"
    assert cancelled.filled_qty == Decimal("0.25")


@pytest.mark.asyncio
async def test_fill_evidence_on_terminal_rung_short_circuits(db_session):
    """Re-evidence flowing into an already-terminal rung must short-circuit to a
    no-op — never raise InvalidStateTransition (which the reconcile kernel would
    otherwise mislabel as an anomaly)."""
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 2, tzinfo=UTC)
    rested = await _record_resting(service, group.proposal_id, now=now)
    await db_session.commit()

    first = await service.record_fill_evidence(
        broker_order_id=rested.broker_order_id, terminal_state="cancelled", now=now
    )
    assert first is not None and first.state == "cancelled"

    # A second reconcile pass re-delivers the same cancel evidence.
    again = await service.record_fill_evidence(
        broker_order_id=rested.broker_order_id, terminal_state="cancelled", now=now
    )
    assert again is None

    # A late-arriving fill against the same terminal rung must also no-op.
    late = await service.record_fill_evidence(
        broker_order_id=rested.broker_order_id,
        filled_qty=Decimal("1"),
        terminal_state="filled",
        now=now,
    )
    assert late is None


@pytest.mark.asyncio
async def test_fill_evidence_rechecks_committed_state_under_lock(db_session):
    """Concurrency invariant: once another session has committed a terminal fill,
    late/partial evidence arriving on a session that observed the rung earlier
    must short-circuit — it must never regress the already-`filled` rung back to
    `partially_filled`. Guards the record_fill_evidence lock + refresh re-check."""
    from app.mcp_server.tooling.live_order_ledger import _order_session_factory

    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 4, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await db_session.commit()

    # Prime THIS session's identity map with the rung while it is still 'acked'.
    await service.get_proposal(group.proposal_id)

    # A concurrent session commits the terminal fill.
    async with _order_session_factory()() as db2:
        other = OrderProposalsService(db2)
        await other.record_fill_evidence(
            broker_order_id=acked.broker_order_id,
            filled_qty=Decimal("1"),
            terminal_state="filled",
            now=now + timedelta(seconds=1),
        )
        await db2.commit()

    # Late partial evidence arriving on the stale session must short-circuit,
    # never regress the committed `filled` rung back to `partially_filled`.
    result = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.25"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=2),
    )
    assert result is None

    async with _order_session_factory()() as db3:
        _, rungs = await OrderProposalsService(db3).get_proposal(group.proposal_id)
        assert rungs[0].state == "filled"
        assert rungs[0].filled_qty == Decimal("1")


@pytest.mark.asyncio
async def test_fill_evidence_repeated_partial_refreshes_qty(db_session):
    """A second partial-fill evidence on an already-partially-filled rung
    refreshes the cumulative quantity without an (illegal) self-transition."""
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 11, 9, 3, tzinfo=UTC)
    acked = await _record_ack(service, group.proposal_id, now=now)
    await db_session.commit()

    await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.25"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=1),
    )
    refreshed = await service.record_fill_evidence(
        broker_order_id=acked.broker_order_id,
        filled_qty=Decimal("0.5"),
        terminal_state="partially_filled",
        now=now + timedelta(seconds=2),
    )

    assert refreshed is not None
    assert refreshed.state == "partially_filled"
    assert refreshed.filled_qty == Decimal("0.5")
    assert refreshed.updated_at == now + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_mark_needs_reconfirm_bumps_revision(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 10, tzinfo=UTC)
    await service.transition_rung(
        group.proposal_id,
        0,
        new_state="revalidating",
        approval_revision=2,
    )

    rung = await service.mark_needs_reconfirm(group.proposal_id, 0, now=now)

    assert rung.state == "needs_reconfirm"
    assert rung.approval_revision == 3
    assert rung.validated_at == now
    assert rung.updated_at == now


@pytest.mark.asyncio
async def test_record_rejected_records_reason_from_legal_state(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 11, tzinfo=UTC)

    rung = await service.record_rejected(
        group.proposal_id, 0, reason="operator_denied", now=now
    )

    assert rung.state == "rejected"
    assert rung.void_reason == "operator_denied"
    assert rung.updated_at == now


@pytest.mark.asyncio
async def test_record_rejected_does_not_bypass_state_machine(db_session):
    service, group = await _create_single_rung(db_session)
    now = datetime(2026, 7, 10, 9, 12, tzinfo=UTC)
    await _record_ack(service, group.proposal_id, now=now)

    with pytest.raises(OrderProposalInvalidStateTransition):
        await service.record_rejected(
            group.proposal_id, 0, reason="late_denial", now=now
        )


@pytest.mark.asyncio
async def test_sweep_local_stale_only_voids_evidence_absent(db_session):
    service = OrderProposalsService(db_session)
    groups = []
    rung_ids = []
    for symbol in ("NO_ORDER", "TIMEOUT", "UNKNOWN"):
        group = await service.create_proposal(
            symbol=symbol,
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )
        _, rungs = await service.get_proposal(group.proposal_id)
        groups.append(group)
        rung_ids.append(rungs[0].id)

    with_broker = await service.create_proposal(
        symbol="HAS_BROKER",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await service.transition_rung(
        with_broker.proposal_id,
        0,
        new_state="revalidating",
        broker_order_id="B-present",
    )
    await service.transition_rung(
        with_broker.proposal_id, 0, new_state="pending_approval"
    )
    _, with_broker_rungs = await service.get_proposal(with_broker.proposal_id)
    not_pending = await service.create_proposal(
        symbol="REVALIDATING",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await service.transition_rung(not_pending.proposal_id, 0, new_state="revalidating")
    _, not_pending_rungs = await service.get_proposal(not_pending.proposal_id)
    await db_session.commit()

    evidence_by_rung = dict(
        zip(rung_ids, ("no_broker_order", "timeout", "unknown"), strict=True)
    )
    called = []

    async def broker_evidence(rung):
        called.append(rung.id)
        return evidence_by_rung.get(rung.id, "unknown")

    now = datetime(2026, 7, 10, 9, 13, tzinfo=UTC)
    swept = await service.sweep_local_stale(now=now, broker_evidence=broker_evidence)

    assert swept == [groups[0].proposal_id]
    assert set(rung_ids).issubset(called)
    assert with_broker_rungs[0].id not in called
    assert not_pending_rungs[0].id not in called
    states = []
    for group in groups:
        _, rungs = await service.get_proposal(group.proposal_id)
        states.append(rungs[0].state)
    assert states == ["voided_local_stale", "pending_approval", "pending_approval"]
    _, swept_rungs = await service.get_proposal(groups[0].proposal_id)
    assert swept_rungs[0].void_reason == "no_broker_order"
    assert swept_rungs[0].updated_at == now


@pytest.mark.asyncio
async def test_sweep_local_stale_accepts_sync_evidence_callback(db_session):
    service, group = await _create_single_rung(db_session, symbol="SYNC")
    now = datetime(2026, 7, 10, 9, 14, tzinfo=UTC)
    _, initial_rungs = await service.get_proposal(group.proposal_id)
    target_rung_id = initial_rungs[0].id

    swept = await service.sweep_local_stale(
        now=now,
        broker_evidence=lambda rung: (
            "no_broker_order" if rung.id == target_rung_id else "unknown"
        ),
    )

    assert group.proposal_id in swept
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "voided_local_stale"


@pytest.mark.asyncio
async def test_record_approval_dispatch_merges_source_asof(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        source_asof={"resting_deadline": "2026-07-10T15:30:00+09:00"},
    )
    await db_session.commit()
    now = datetime(2026, 7, 10, 9, 15, tzinfo=UTC)

    updated = await service.record_approval_dispatch(
        group.proposal_id, message_id=4242, chat_id="chat-1", now=now
    )

    assert updated.source_asof["resting_deadline"] == "2026-07-10T15:30:00+09:00"
    assert updated.source_asof["approval_message_id"] == 4242
    assert updated.source_asof["approval_chat_id"] == "chat-1"
    assert updated.source_asof["approval_sent_at"] == now.isoformat()

    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert refreshed.source_asof["resting_deadline"] == "2026-07-10T15:30:00+09:00"
    assert refreshed.source_asof["approval_message_id"] == 4242


@pytest.mark.asyncio
async def test_record_approval_dispatch_missing_proposal_raises(db_session):
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalNotFound):
        await service.record_approval_dispatch(
            uuid.uuid4(),
            message_id=1,
            chat_id="chat-1",
            now=datetime(2026, 7, 10, 9, 15, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# Final-review Finding 1 — account_mode/market submit-routing allowlist.
# `_place_order_impl` (the submit path's default binding) has no
# `account_mode` param at all: it routes purely by `market` and always
# submits `is_mock=False`. A proposal created with an account_mode the submit
# path doesn't actually honor (kis_mock, toss_live, db_simulated) must be
# rejected at create time -- never persisted -- rather than silently routed
# to LIVE KIS on approval.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_allows_kis_live_equity_kr(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    assert group.account_mode == "kis_live"
    assert group.market == "equity_kr"


@pytest.mark.asyncio
async def test_create_proposal_allows_kis_live_equity_us(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    assert group.account_mode == "kis_live"
    assert group.market == "equity_us"


@pytest.mark.asyncio
async def test_create_proposal_allows_upbit_crypto(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="BTC/KRW",
        market="crypto",
        account_mode="upbit",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("0.01"), Decimal("100000000"), None)],
    )
    await db_session.commit()
    assert group.account_mode == "upbit"
    assert group.market == "crypto"


@pytest.mark.asyncio
async def test_create_proposal_rejects_kis_mock_equity_kr(db_session):
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError) as exc_info:
        await service.create_proposal(
            symbol="A",
            market="equity_kr",
            account_mode="kis_mock",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )
    assert str(exc_info.value) == (
        "unsupported account_mode/market/action: kis_mock/equity_kr/place "
        "(allowed: kis_live×equity_kr|equity_us, "
        "toss_live×equity_kr|equity_us, upbit×crypto; "
        "market aliases kr→equity_kr, us→equity_us)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["equity_kr", "equity_us"])
async def test_create_proposal_allows_toss_live_equities(db_session, market):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="A" if market == "equity_kr" else "AAPL",
        market=market,
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await db_session.commit()
    assert group.account_mode == "toss_live"
    assert group.market == market


@pytest.mark.asyncio
async def test_create_proposal_rejects_db_simulated_and_upbit_wrong_market(db_session):
    service = OrderProposalsService(db_session)
    with pytest.raises(
        OrderProposalError, match="unsupported account_mode/market/action"
    ):
        await service.create_proposal(
            symbol="A",
            market="equity_kr",
            account_mode="db_simulated",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )
    with pytest.raises(
        OrderProposalError, match="unsupported account_mode/market/action"
    ):
        await service.create_proposal(
            symbol="A",
            market="equity_kr",
            account_mode="upbit",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )


@pytest.mark.asyncio
async def test_create_proposal_rejection_leaves_no_partial_rows(db_session):
    """Airtight: the reject must fire before any group/rung row is written --
    even flushed-but-uncommitted -- so a query against this same session sees
    zero matching rows for a rejected create_proposal call.
    """
    from sqlalchemy import func, select

    from app.models.order_proposals import OrderProposal

    service = OrderProposalsService(db_session)
    symbol = f"REJECT-{uuid.uuid4().hex[:8]}"
    with pytest.raises(OrderProposalError):
        await service.create_proposal(
            symbol=symbol,
            market="equity_kr",
            account_mode="kis_mock",
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        )

    count = await db_session.scalar(
        select(func.count())
        .select_from(OrderProposal)
        .where(OrderProposal.symbol == symbol)
    )
    assert count == 0


async def _create_batch_candidate(
    db_session,
    *,
    symbol: str,
    nonce: str,
    source_asof: dict | None = None,
    valid_until: datetime | None = None,
):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_us",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="batch-test",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        source_asof=source_asof,
        valid_until=valid_until,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(service, group, nonce=nonce)
    await db_session.commit()
    return group


@pytest.mark.asyncio
async def test_approval_batch_registration_groups_same_chat_and_window(db_session):
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    first = await _create_batch_candidate(
        db_session, symbol="AAPL", nonce="batch-member-1"
    )
    second = await _create_batch_candidate(
        db_session, symbol="MSFT", nonce="batch-member-2"
    )
    third = await _create_batch_candidate(
        db_session, symbol="NVDA", nonce="batch-member-3"
    )
    service = OrderProposalsService(db_session)

    one = await service.register_approval_batch_member(
        first.proposal_id,
        chat_id=chat_id,
        approval_message_id=1001,
        now=now,
    )
    two = await service.register_approval_batch_member(
        second.proposal_id,
        chat_id=chat_id,
        approval_message_id=1002,
        now=now + timedelta(minutes=1),
    )
    assert one is not None and two is not None
    assert one.batch.batch_id == two.batch.batch_id
    assert one.member_count == 1 and one.summary_action == "none"
    assert two.member_count == 2 and two.summary_action == "send"

    assert two.binding is not None
    finalized = await service.finish_approval_batch_dispatch(
        two.batch.batch_id,
        attempt_id=two.binding.attempt_id,
        publication=_successful_publication(2001),
        now=now + timedelta(minutes=1),
    )
    assert finalized.ok
    three = await service.register_approval_batch_member(
        third.proposal_id,
        chat_id=chat_id,
        approval_message_id=1003,
        now=now + timedelta(minutes=2),
    )
    assert three is not None
    assert three.batch.batch_id != one.batch.batch_id
    assert three.member_count == 1 and three.summary_action == "none"


@pytest.mark.asyncio
async def test_approval_batch_registration_respects_chat_and_fixed_window(db_session):
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    other_chat_id = f"batch-{uuid.uuid4().hex}"
    first = await _create_batch_candidate(
        db_session, symbol="AAPL", nonce="window-member-1"
    )
    other_chat = await _create_batch_candidate(
        db_session, symbol="MSFT", nonce="window-member-2"
    )
    later = await _create_batch_candidate(
        db_session, symbol="NVDA", nonce="window-member-3"
    )
    service = OrderProposalsService(db_session)

    first_registration = await service.register_approval_batch_member(
        first.proposal_id,
        chat_id=chat_id,
        approval_message_id=1001,
        now=now,
    )
    other_registration = await service.register_approval_batch_member(
        other_chat.proposal_id,
        chat_id=other_chat_id,
        approval_message_id=1002,
        now=now + timedelta(minutes=1),
    )
    later_registration = await service.register_approval_batch_member(
        later.proposal_id,
        chat_id=chat_id,
        approval_message_id=1003,
        now=now + timedelta(minutes=10),
    )

    assert first_registration is not None
    assert other_registration is not None
    assert later_registration is not None
    assert (
        len(
            {
                first_registration.batch.batch_id,
                other_registration.batch.batch_id,
                later_registration.batch.batch_id,
            }
        )
        == 3
    )


@pytest.mark.asyncio
async def test_approval_batch_registration_does_not_join_expired_open_window(
    db_session,
):
    now = datetime.now(UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    expiring = await _create_batch_candidate(
        db_session,
        symbol="AAPL",
        nonce="expired-window-member-1",
        valid_until=now + timedelta(minutes=2),
    )
    later = await _create_batch_candidate(
        db_session,
        symbol="MSFT",
        nonce="expired-window-member-2",
        valid_until=now + timedelta(hours=1),
    )
    service = OrderProposalsService(db_session)

    first_registration = await service.register_approval_batch_member(
        expiring.proposal_id,
        chat_id=chat_id,
        approval_message_id=1001,
        now=now,
    )
    later_registration = await service.register_approval_batch_member(
        later.proposal_id,
        chat_id=chat_id,
        approval_message_id=1002,
        now=now + timedelta(minutes=3),
    )

    assert first_registration is not None
    assert later_registration is not None
    assert later_registration.batch.batch_id != first_registration.batch.batch_id
    assert later_registration.member_count == 1
    assert later_registration.summary_action == "none"


@pytest.mark.asyncio
async def test_approval_batch_registration_excludes_manual_safety_classes(db_session):
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    loss_cut = await _create_batch_candidate(
        db_session, symbol="LOSS", nonce="excluded-loss"
    )
    auto = await _create_batch_candidate(
        db_session,
        symbol="AUTO",
        nonce="excluded-auto",
        source_asof={"auto_approved": {"approved_at": now.isoformat()}},
    )
    terminal = await _create_batch_candidate(
        db_session, symbol="DONE", nonce="excluded-terminal"
    )
    superseded = await _create_batch_candidate(
        db_session, symbol="OLD", nonce="excluded-superseded"
    )
    loss_cut.exit_intent = "loss_cut"
    terminal.lifecycle_state = "terminal"
    superseded.lifecycle_state = "superseded"
    await db_session.commit()
    service = OrderProposalsService(db_session)

    for index, group in enumerate((loss_cut, auto, terminal, superseded), start=1):
        registration = await service.register_approval_batch_member(
            group.proposal_id,
            chat_id=chat_id,
            approval_message_id=1000 + index,
            now=now,
        )
        assert registration is None


@pytest.mark.asyncio
async def test_approval_batch_registration_excludes_transient_ineligible_members(
    db_session,
):
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    missing_nonce = await _create_batch_candidate(
        db_session, symbol="MISS", nonce="excluded-missing"
    )
    used_nonce = await _create_batch_candidate(
        db_session, symbol="USED", nonce="excluded-used"
    )
    no_pending = await _create_batch_candidate(
        db_session, symbol="NOPEND", nonce="excluded-no-pending"
    )
    expired = await _create_batch_candidate(
        db_session, symbol="EXPIRE", nonce="excluded-expired"
    )
    missing_nonce.approval_nonce = None
    used_nonce.approval_nonce_used_at = now
    expired.valid_until = now
    await db_session.commit()
    service = OrderProposalsService(db_session)
    await service.transition_rung(no_pending.proposal_id, 0, new_state="revalidating")

    for index, group in enumerate(
        (missing_nonce, used_nonce, no_pending, expired), start=1
    ):
        registration = await service.register_approval_batch_member(
            group.proposal_id,
            chat_id=chat_id,
            approval_message_id=1100 + index,
            now=now,
        )
        assert registration is None


@pytest.mark.asyncio
async def test_approval_batch_registration_keeps_fresh_nonce_out_of_same_batch(
    db_session,
):
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    group = await _create_batch_candidate(
        db_session, symbol="AAPL", nonce="membership-cycle-1"
    )
    service = OrderProposalsService(db_session)
    first = await service.register_approval_batch_member(
        group.proposal_id,
        chat_id=chat_id,
        approval_message_id=1201,
        now=now,
    )
    await db_session.commit()
    await service.set_approval_nonce(group.proposal_id, "membership-cycle-2")
    await db_session.commit()

    second = await service.register_approval_batch_member(
        group.proposal_id,
        chat_id=chat_id,
        approval_message_id=1202,
        now=now + timedelta(minutes=1),
    )

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_approval_batch_nonce_rejects_wrong_chat_nonce_and_too_small(
    db_session,
):
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    first = await _create_batch_candidate(
        db_session, symbol="AAPL", nonce="validation-member-1"
    )
    second = await _create_batch_candidate(
        db_session, symbol="MSFT", nonce="validation-member-2"
    )
    service = OrderProposalsService(db_session)
    await service.register_approval_batch_member(
        first.proposal_id,
        chat_id=chat_id,
        approval_message_id=1301,
        now=now,
    )
    registration = await service.register_approval_batch_member(
        second.proposal_id,
        chat_id=chat_id,
        approval_message_id=1302,
        now=now + timedelta(seconds=1),
    )
    assert registration is not None and registration.binding is not None
    await service.finish_approval_batch_dispatch(
        registration.batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        publication=_successful_publication(2301),
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    batch_id = registration.batch.batch_id
    for expected, supplied_chat, callback in (
        (
            "approval_batch_chat_mismatch",
            "wrong-chat",
            _batch_callback(registration.batch),
        ),
        (
            "nonce_mismatch",
            chat_id,
            _batch_callback(registration.batch, nonce="wrong-nonce"),
        ),
    ):
        with pytest.raises(OrderProposalError, match=expected):
            await service.consume_approval_batch_nonce(
                batch_id,
                callback=callback,
                chat_id=supplied_chat,
                telegram_user_id="777",
                now=now + timedelta(minutes=1),
            )
        await db_session.rollback()

    singleton_chat = f"batch-{uuid.uuid4().hex}"
    singleton = await _create_batch_candidate(
        db_session, symbol="NVDA", nonce="unpublished-member"
    )
    staged = await service.register_approval_batch_member(
        singleton.proposal_id,
        chat_id=singleton_chat,
        approval_message_id=1303,
        now=now,
    )
    assert staged is not None and staged.binding is None
    await db_session.commit()
    with pytest.raises(OrderProposalError, match="approval_dispatch_pending"):
        await service.consume_approval_batch_nonce(
            staged.batch.batch_id,
            callback=CallbackEnvelope(
                action="ba",
                subject_short=str(staged.batch.batch_id)[:8],
                attempt_id=uuid.uuid4(),
                membership_revision=staged.batch.membership_revision,
                membership_digest="AbCdEf0123_-",
                nonce=staged.batch.approval_nonce,
            ),
            chat_id=singleton_chat,
            telegram_user_id="777",
            now=now + timedelta(minutes=1),
        )


@pytest.mark.asyncio
async def test_approval_batch_nonce_is_single_use_and_bound_to_chat(db_session):
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    first = await _create_batch_candidate(
        db_session, symbol="AAPL", nonce="consume-member-1"
    )
    second = await _create_batch_candidate(
        db_session, symbol="MSFT", nonce="consume-member-2"
    )
    service = OrderProposalsService(db_session)
    await service.register_approval_batch_member(
        first.proposal_id,
        chat_id=chat_id,
        approval_message_id=1001,
        now=now,
    )
    registration = await service.register_approval_batch_member(
        second.proposal_id,
        chat_id=chat_id,
        approval_message_id=1002,
        now=now + timedelta(minutes=1),
    )
    assert registration is not None and registration.binding is not None
    await service.finish_approval_batch_dispatch(
        registration.batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        publication=_successful_publication(2001),
        now=now + timedelta(minutes=1),
    )
    await db_session.commit()
    callback = _batch_callback(registration.batch)
    batch, members = await service.consume_approval_batch_nonce(
        registration.batch.batch_id,
        callback=callback,
        chat_id=chat_id,
        telegram_user_id="777",
        now=now + timedelta(minutes=2),
    )
    await db_session.commit()
    assert batch.approval_nonce_used_at == now + timedelta(minutes=2)
    assert [member.proposal_id for member in members] == [
        first.proposal_id,
        second.proposal_id,
    ]

    with pytest.raises(OrderProposalError, match="nonce_replay"):
        await service.consume_approval_batch_nonce(
            batch.batch_id,
            callback=callback,
            chat_id=chat_id,
            telegram_user_id="777",
            now=now + timedelta(minutes=3),
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_approval_batch_nonce_expires_without_consuming_members(db_session):
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    chat_id = f"batch-{uuid.uuid4().hex}"
    first = await _create_batch_candidate(
        db_session, symbol="AAPL", nonce="expiry-member-1"
    )
    second = await _create_batch_candidate(
        db_session, symbol="MSFT", nonce="expiry-member-2"
    )
    service = OrderProposalsService(db_session)
    await service.register_approval_batch_member(
        first.proposal_id,
        chat_id=chat_id,
        approval_message_id=1001,
        now=now,
    )
    registration = await service.register_approval_batch_member(
        second.proposal_id,
        chat_id=chat_id,
        approval_message_id=1002,
        now=now + timedelta(minutes=1),
    )
    assert registration is not None and registration.binding is not None
    await service.finish_approval_batch_dispatch(
        registration.batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        publication=_successful_publication(2001),
        now=now + timedelta(minutes=1),
    )
    await db_session.commit()
    first_id = first.proposal_id
    second_id = second.proposal_id

    with pytest.raises(OrderProposalError, match="approval_batch_expired"):
        await service.consume_approval_batch_nonce(
            registration.batch.batch_id,
            callback=_batch_callback(registration.batch),
            chat_id=chat_id,
            telegram_user_id="777",
            now=registration.batch.expires_at,
        )
    await db_session.rollback()
    first_after, _ = await service.get_proposal(first_id)
    second_after, _ = await service.get_proposal(second_id)
    assert first_after.approval_nonce_used_at is None
    assert second_after.approval_nonce_used_at is None


# -- ROB-897 cause (1): batch expiry sweep ----------------------------------


@pytest.mark.asyncio
async def test_sweep_expired_transitions_past_deadline_group_to_expired(db_session):
    service, group = await _create_single_rung(db_session, symbol="EXP_PAST")
    await service.set_approval_nonce(group.proposal_id, "nonce-exp")
    await service.transition_rung(group.proposal_id, 0, new_state="revalidating")
    await service.mark_needs_reconfirm(
        group.proposal_id, 0, now=datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    )
    group.valid_until = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    await db_session.commit()

    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    swept = await service.sweep_expired(now=now)

    # Shared test DB accumulates committed past-valid_until groups from sibling
    # tests, and sweep_expired is a global maintenance sweep, so assert on THIS
    # test's seeded group by membership rather than exact list equality.
    assert group.proposal_id in {r.proposal_id for r in swept}
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.lifecycle_state == "expired"
    assert refreshed.approval_nonce is None
    assert [r.state for r in rungs] == ["expired"]


@pytest.mark.asyncio
async def test_sweep_expired_ignores_future_and_null_valid_until(db_session):
    service, future_group = await _create_single_rung(db_session, symbol="EXP_FUTURE")
    future_group.valid_until = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    _, null_group = await _create_single_rung(db_session, symbol="EXP_NULL")
    null_group.valid_until = None
    await db_session.commit()

    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    swept = await service.sweep_expired(now=now)

    swept_ids = {r.proposal_id for r in swept}
    assert future_group.proposal_id not in swept_ids
    assert null_group.proposal_id not in swept_ids
    future_after, future_rungs = await service.get_proposal(future_group.proposal_id)
    assert future_after.lifecycle_state == "proposed"
    assert [r.state for r in future_rungs] == ["pending_approval"]


@pytest.mark.asyncio
async def test_sweep_expired_does_not_reprocess_terminal_group(db_session):
    service, group = await _create_single_rung(db_session, symbol="EXP_TERMINAL")
    group.valid_until = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    await db_session.commit()
    earlier = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
    assert await service.expire_if_needed(group.proposal_id, now=earlier)
    await db_session.commit()

    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    swept = await service.sweep_expired(now=now)

    assert group.proposal_id not in {r.proposal_id for r in swept}
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.lifecycle_state == "expired"
    assert [r.state for r in rungs] == ["expired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("non_voidable_state", ["submitting", "resting", "filled"])
async def test_sweep_expired_skips_group_with_non_voidable_rung_and_continues(
    db_session, non_voidable_state
):
    service, blocked = await _create_single_rung(db_session, symbol="EXP_BLOCKED")
    if non_voidable_state == "submitting":
        await _drive_to_submitting(service, blocked.proposal_id)
    elif non_voidable_state == "resting":
        await _drive_to_submitting(service, blocked.proposal_id)
        await service.record_resting(
            blocked.proposal_id,
            0,
            broker_order_id="B-resting",
            correlation_id="corr-resting",
            idempotency_key="idem-resting",
            approval_hash_digest="hash-resting",
            now=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        )
    else:  # filled
        await _drive_to_submitting(service, blocked.proposal_id)
        await service.record_ack(
            blocked.proposal_id,
            0,
            broker_order_id="B-filled",
            correlation_id="corr-filled",
            idempotency_key="idem-filled",
            approval_hash_digest="hash-filled",
            now=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        )
        await service.record_fill_evidence(
            broker_order_id="B-filled",
            filled_qty=Decimal("1"),
            terminal_state="filled",
            now=datetime(2026, 7, 13, 9, 5, tzinfo=UTC),
        )
    blocked.valid_until = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)

    _, healthy = await _create_single_rung(db_session, symbol="EXP_HEALTHY")
    healthy.valid_until = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    await db_session.commit()

    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    swept = await service.sweep_expired(now=now)

    swept_ids = {r.proposal_id for r in swept}
    assert blocked.proposal_id not in swept_ids
    assert healthy.proposal_id in swept_ids
    blocked_after, blocked_rungs = await service.get_proposal(blocked.proposal_id)
    assert blocked_after.lifecycle_state != "expired"
    assert blocked_rungs[0].state == non_voidable_state
    healthy_after, healthy_rungs = await service.get_proposal(healthy.proposal_id)
    assert healthy_after.lifecycle_state == "expired"
    assert [r.state for r in healthy_rungs] == ["expired"]


@pytest.mark.asyncio
async def test_sweep_expired_returns_chat_and_message_id_for_telegram_cleanup(
    db_session,
):
    service, group = await _create_single_rung(db_session, symbol="EXP_TG")
    now_dispatch = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    await service.record_approval_dispatch(
        group.proposal_id, message_id=4242, chat_id="chat-897", now=now_dispatch
    )
    group.valid_until = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    await db_session.commit()

    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    swept = await service.sweep_expired(now=now)

    # Global sweep in a shared DB: assert THIS group's result is present, not
    # that it is the only one swept.
    assert (
        ExpirySweepResult(
            proposal_id=group.proposal_id,
            symbol="EXP_TG",
            chat_id="chat-897",
            message_id=4242,
        )
        in swept
    )


@pytest.mark.asyncio
async def test_list_expiry_candidates_is_read_only_preview(db_session):
    service, group = await _create_single_rung(db_session, symbol="EXP_PREVIEW")
    group.valid_until = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    await db_session.commit()

    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    candidates = await service.list_expiry_candidates(now=now)

    # Membership, not equality: the shared DB holds many other committed
    # past-valid_until groups. The preview must be read-only for our group.
    assert group.proposal_id in {g.proposal_id for g, _ in candidates}
    unchanged, rungs = await service.get_proposal(group.proposal_id)
    assert unchanged.lifecycle_state == "proposed"
    assert [r.state for r in rungs] == ["pending_approval"]


# -- ROB-929: expired/voided defensive proposal handoff surface -------------
#
# ``lifecycle_state``/``valid_until`` transitions accept a fictional business
# ``now``, but ``updated_at`` is DB ``onupdate=func.now()`` -- the real wall
# clock -- unless explicitly overridden. Every test below stamps
# ``updated_at`` directly after the transition so the `hours` filter is
# deterministic regardless of when the suite actually runs.

_HANDOFF_NOW = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)
_HANDOFF_RECENT = datetime(2026, 7, 21, 0, 30, tzinfo=UTC)  # 30min before _HANDOFF_NOW
_HANDOFF_CREATED = datetime(2026, 7, 19, 1, 0, tzinfo=UTC)


async def _create_defensive_rung(
    db_session,
    *,
    symbol: str,
    market: str = "equity_us",
    account_mode: str = "kis_live",
):
    """Create the only defensive exit_intent actually creatable today: loss_cut.

    exit_intent="defensive_trim" is rejected at create time (see
    test_defensive_trim_exit_intent_is_rejected_pending_execution_support) --
    every handoff test below that needs a *real* expired/voided defensive
    proposal uses loss_cut. Forward-compat recognition of the "defensive_trim"
    tag itself is covered separately by
    test_expired_defensive_handoff_recognizes_defensive_trim_tag_forward_compat.
    """
    service = OrderProposalsService(db_session)

    async def fake_lookup(session, retrospective_id):
        return _retro(symbol=symbol)

    with patch(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    ):
        group = await service.create_proposal(
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            side="sell",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("100"), None)],
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            valid_until=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
            now=_HANDOFF_CREATED,
        )
    await db_session.commit()
    return service, group


@pytest.mark.asyncio
async def test_expired_defensive_handoff_includes_expired_loss_cut(db_session):
    service, group = await _create_defensive_rung(db_session, symbol="HANDOFF_EXP")
    assert await service.expire_if_needed(group.proposal_id, now=_HANDOFF_RECENT)
    group.updated_at = _HANDOFF_RECENT
    await db_session.commit()

    items = await service.list_expired_defensive_handoff(now=_HANDOFF_NOW, hours=24)

    matching = [item for item in items if item.proposal_id == group.proposal_id]
    assert len(matching) == 1
    assert matching[0].symbol == "HANDOFF_EXP"
    assert matching[0].side == "sell"
    assert matching[0].exit_intent == "loss_cut"
    assert matching[0].lifecycle_state == "expired"
    assert matching[0].needs_reassessment is True


@pytest.mark.asyncio
async def test_expired_defensive_handoff_recognizes_defensive_trim_tag_forward_compat(
    db_session,
):
    """The read surface stays forward-compat aware of exit_intent="defensive_trim"
    even though create rejects it today (ROB-929 code review) -- so no further
    PR is needed here once execution-side support for defensive_trim lands.
    This bypasses create validation by writing the tag directly, mirroring how
    a future execution-aware create path would persist it.
    """
    service, group = await _create_single_rung(db_session, symbol="HANDOFF_FWDCOMPAT")
    group.side = "sell"
    group.exit_intent = "defensive_trim"
    group.valid_until = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    await db_session.commit()
    assert await service.expire_if_needed(group.proposal_id, now=_HANDOFF_RECENT)
    group.updated_at = _HANDOFF_RECENT
    await db_session.commit()

    items = await service.list_expired_defensive_handoff(now=_HANDOFF_NOW, hours=24)

    matching = [item for item in items if item.proposal_id == group.proposal_id]
    assert len(matching) == 1
    assert matching[0].exit_intent == "defensive_trim"


@pytest.mark.asyncio
async def test_expired_defensive_handoff_includes_voided_loss_cut(
    db_session, monkeypatch
):
    async def fake_lookup(session, retrospective_id):
        return _retro(symbol="HANDOFF_VOID", created_at=_HANDOFF_RECENT)

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="HANDOFF_VOID",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("65000"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        now=_HANDOFF_RECENT,
    )
    await db_session.commit()
    await service.void_proposal(
        group.proposal_id, reason="operator abort", now=_HANDOFF_RECENT
    )
    group.updated_at = _HANDOFF_RECENT
    await db_session.commit()

    items = await service.list_expired_defensive_handoff(now=_HANDOFF_NOW, hours=24)

    matching = [item for item in items if item.proposal_id == group.proposal_id]
    assert len(matching) == 1
    assert matching[0].lifecycle_state == "voided"
    assert matching[0].exit_intent == "loss_cut"


@pytest.mark.asyncio
async def test_expired_defensive_handoff_excludes_general_expired_proposal(db_session):
    service, group = await _create_single_rung(db_session, symbol="HANDOFF_GENERAL")
    group.valid_until = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    await db_session.commit()
    assert await service.expire_if_needed(group.proposal_id, now=_HANDOFF_RECENT)
    group.updated_at = _HANDOFF_RECENT
    await db_session.commit()

    items = await service.list_expired_defensive_handoff(now=_HANDOFF_NOW, hours=24)

    assert group.proposal_id not in {item.proposal_id for item in items}


@pytest.mark.asyncio
async def test_expired_defensive_handoff_excludes_superseded_proposal(db_session):
    service, group = await _create_defensive_rung(db_session, symbol="HANDOFF_SUPER")
    assert await service.expire_if_needed(group.proposal_id, now=_HANDOFF_RECENT)
    group.updated_at = _HANDOFF_RECENT
    group.superseded_by_proposal_id = uuid.uuid4()
    await db_session.commit()

    items = await service.list_expired_defensive_handoff(now=_HANDOFF_NOW, hours=24)

    assert group.proposal_id not in {item.proposal_id for item in items}


@pytest.mark.asyncio
async def test_expired_defensive_handoff_excludes_symbol_side_with_active_reproposal(
    db_session,
):
    service, expired_group = await _create_defensive_rung(
        db_session, symbol="HANDOFF_ACTIVE"
    )
    assert await service.expire_if_needed(
        expired_group.proposal_id, now=_HANDOFF_RECENT
    )
    expired_group.updated_at = _HANDOFF_RECENT
    await db_session.commit()
    # A fresh re-proposal for the same symbol+side is still active (proposed).
    # No exit_intent needed here -- the active-pair exclusion matches on
    # symbol+side regardless of exit_intent.
    await service.create_proposal(
        symbol="HANDOFF_ACTIVE",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("110"), None)],
        now=_HANDOFF_RECENT,
    )
    await db_session.commit()

    items = await service.list_expired_defensive_handoff(now=_HANDOFF_NOW, hours=24)

    assert expired_group.proposal_id not in {item.proposal_id for item in items}


@pytest.mark.asyncio
async def test_expired_defensive_handoff_respects_hours_boundary(db_session):
    service, in_window = await _create_defensive_rung(
        db_session, symbol="HANDOFF_HRS_IN"
    )
    assert await service.expire_if_needed(in_window.proposal_id, now=_HANDOFF_RECENT)
    in_window.updated_at = _HANDOFF_NOW - timedelta(hours=24)  # exactly 24h ago

    _, out_of_window = await _create_defensive_rung(
        db_session, symbol="HANDOFF_HRS_OUT"
    )
    assert await service.expire_if_needed(
        out_of_window.proposal_id, now=_HANDOFF_RECENT
    )
    out_of_window.updated_at = (
        _HANDOFF_NOW - timedelta(hours=24) - timedelta(seconds=1)
    )  # 1s past 24h
    await db_session.commit()

    items = await service.list_expired_defensive_handoff(now=_HANDOFF_NOW, hours=24)

    ids = {item.proposal_id for item in items}
    assert in_window.proposal_id in ids
    assert out_of_window.proposal_id not in ids


@pytest.mark.asyncio
async def test_expired_defensive_handoff_filters_by_market(db_session):
    service, us_group = await _create_defensive_rung(
        db_session, symbol="HANDOFF_US", market="equity_us"
    )
    assert await service.expire_if_needed(us_group.proposal_id, now=_HANDOFF_RECENT)
    us_group.updated_at = _HANDOFF_RECENT
    _, kr_group = await _create_defensive_rung(
        db_session,
        symbol="HANDOFF_KR",
        market="equity_kr",
        account_mode="toss_live",
    )
    assert await service.expire_if_needed(kr_group.proposal_id, now=_HANDOFF_RECENT)
    kr_group.updated_at = _HANDOFF_RECENT
    await db_session.commit()

    items = await service.list_expired_defensive_handoff(
        now=_HANDOFF_NOW, hours=24, market="equity_kr"
    )

    ids = {item.proposal_id for item in items}
    assert kr_group.proposal_id in ids
    assert us_group.proposal_id not in ids
