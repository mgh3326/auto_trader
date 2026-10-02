"""Task 890 PR A -- what the /trader approval inbox includes and excludes.

Core contract (builder-owned): only proposals that still need a human decision
on a live approval card are actionable. An auto-approved proposal (auto-veto
notice), an expired proposal (even before the expiry sweeper ran), a consumed
nonce, an unpublished/failed card, an in-flight loss-cut confirmation, and any
terminal proposal must never be actionable -- the page renders buttons only
for ``actionable`` items.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.service import RungInput
from app.services.order_proposals.telegram_callback import handle_web_approval
from app.services.trader_page.approval_inbox import (
    TraderApprovalInboxService,
    inbox_block_reason,
    project_item,
)
from tests.services.order_proposals.test_telegram_callback import (
    _fake_loss_cut_preview,
    _seed_auto_resting,
    _seed_loss_cut_proposal,
    _seed_proposal,
    _session_factory,
)
from tests.services.order_proposals.window_fakes import allow_known_session

NOW = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)


def _group(**overrides):
    base = {
        "proposal_id": "00000000-0000-0000-0000-000000000001",
        "superseded_by_proposal_id": None,
        "lifecycle_state": "proposed",
        "source_asof": {},
        "approval_dispatch_state": "sent_current",
        "approval_dispatch_card_kind": "manual",
        "approval_nonce": "nonce123456",
        "approval_nonce_used_at": None,
        "valid_until": NOW + timedelta(hours=1),
        "exit_intent": None,
        "action": "place",
        "rationale": None,
        "strategy": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _rung(state: str = "pending_approval", **overrides):
    base = {
        "rung_index": 0,
        "side": "buy",
        "quantity": Decimal("10"),
        "limit_price": Decimal("100"),
        "notional": None,
        "state": state,
        "broker_order_id": None,
        "filled_qty": None,
        "void_reason": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- pure predicate: every exclusion branch, one at a time -----------------


@pytest.mark.unit
def test_published_manual_pending_card_is_actionable() -> None:
    assert inbox_block_reason(_group(), [_rung()], now=NOW) is None


@pytest.mark.unit
def test_reconfirm_card_with_needs_reconfirm_rung_is_actionable() -> None:
    group = _group(approval_dispatch_card_kind="reconfirm")
    assert inbox_block_reason(group, [_rung("needs_reconfirm")], now=NOW) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "rungs", "reason"),
    [
        ({"lifecycle_state": "rejected"}, None, "terminal"),
        ({"lifecycle_state": "expired"}, None, "terminal"),
        ({"lifecycle_state": "voided"}, None, "terminal"),
        ({"lifecycle_state": "terminal"}, None, "terminal"),
        ({"lifecycle_state": "superseded"}, None, "terminal"),
        ({"superseded_by_proposal_id": "x"}, None, "terminal"),
        (
            {"source_asof": {"auto_approved": {"approved_at": "t"}}},
            None,
            "auto_approved",
        ),
        (
            {
                "source_asof": {"auto_approved": {"approved_at": "t"}},
                "approval_dispatch_card_kind": "auto_veto",
            },
            None,
            "auto_approved",
        ),
        ({"approval_dispatch_state": "failed"}, None, "not_published"),
        ({"approval_dispatch_state": "pending"}, None, "not_published"),
        ({"approval_dispatch_state": "sent_superseded"}, None, "not_published"),
        ({"approval_dispatch_state": None}, None, "not_published"),
        ({"approval_dispatch_card_kind": "auto_veto"}, None, "not_human_card"),
        ({"approval_dispatch_card_kind": "batch"}, None, "not_human_card"),
        (
            {"approval_dispatch_card_kind": "loss_cut_confirmation"},
            None,
            "not_human_card",
        ),
        ({"approval_nonce": None}, None, "nonce_used"),
        ({"approval_nonce_used_at": NOW - timedelta(seconds=1)}, None, "nonce_used"),
        ({"valid_until": None}, None, "expired"),
        ({"valid_until": NOW}, None, "expired"),
        ({"valid_until": NOW - timedelta(seconds=1)}, None, "expired"),
        ({}, [_rung("acked")], "no_pending_rungs"),
        ({}, [_rung("revalidating")], "no_pending_rungs"),
        ({}, [], "no_pending_rungs"),
    ],
)
def test_every_exclusion_is_not_actionable(overrides, rungs, reason) -> None:
    group = _group(**overrides)
    assert (
        inbox_block_reason(group, [_rung()] if rungs is None else rungs, now=NOW)
        == reason
    )
    item = project_item(
        SimpleNamespace(
            **{
                **vars(group),
                "symbol": "005930",
                "market": "equity_kr",
                "account_mode": "kis_live",
                "broker_account_id": None,
                "side": "buy",
                "order_type": "limit",
                "approved_at": None,
                "approved_by_channel": None,
                "commit_lease_until": None,
            }
        ),
        [_rung()] if rungs is None else rungs,
        now=NOW,
    )
    assert item.actionable is False
    assert item.block_reason == reason


@pytest.mark.unit
def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        inbox_block_reason(_group(), [_rung()], now=NOW.replace(tzinfo=None))


# --- DB-backed: the real published/auto/expired/consumed shapes ------------


async def _inbox_ids(db_session, now: datetime) -> set[str]:
    response = await TraderApprovalInboxService(db_session, now=now).list_inbox()
    assert response.count == len(response.items)
    assert all(item.actionable for item in response.items)
    return {item.proposal_id for item in response.items}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inbox_includes_human_cards_and_excludes_auto_approved_notice(
    db_session,
) -> None:
    manual = await _seed_proposal(db_session, nonce="inbox-manual", symbol="M1")
    auto = await _seed_auto_resting(db_session, nonce="inbox-autov")

    ids = await _inbox_ids(db_session, datetime.now(UTC))

    assert str(manual.proposal_id) in ids
    assert str(auto.proposal_id) not in ids
    detail = await TraderApprovalInboxService(
        db_session, now=datetime.now(UTC)
    ).get_item(auto.proposal_id)
    assert detail is not None
    assert detail.item.actionable is False
    assert detail.item.block_reason == "auto_approved"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_expired_proposal_is_excluded_even_before_the_sweeper(
    db_session,
) -> None:
    group = await _seed_proposal(db_session, nonce="inbox-expir", symbol="E1")
    fresh, _ = await OrderProposalsService(db_session).get_proposal(group.proposal_id)
    assert fresh.lifecycle_state == "proposed"

    after_expiry = fresh.valid_until + timedelta(seconds=1)
    ids = await _inbox_ids(db_session, after_expiry)

    assert str(group.proposal_id) not in ids
    detail = await TraderApprovalInboxService(db_session, now=after_expiry).get_item(
        group.proposal_id
    )
    assert detail is not None
    assert detail.item.actionable is False
    assert detail.item.block_reason == "expired"
    assert detail.item.expires_in_seconds == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_consumed_nonce_and_unpublished_cards_are_excluded(
    db_session,
) -> None:
    service = OrderProposalsService(db_session)
    consumed = await _seed_proposal(db_session, nonce="inbox-used1", symbol="C1")
    group, _ = await service.get_proposal(consumed.proposal_id)
    await service._repo.update_group(group, approval_nonce_used_at=datetime.now(UTC))

    unpublished = await service.create_proposal(
        symbol="U1",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
    )
    await service.set_approval_nonce(unpublished.proposal_id, "inbox-unpub")
    await db_session.commit()

    ids = await _inbox_ids(db_session, datetime.now(UTC))

    assert str(consumed.proposal_id) not in ids
    assert str(unpublished.proposal_id) not in ids


@pytest.mark.asyncio
@pytest.mark.integration
async def test_denied_proposal_leaves_the_inbox(db_session) -> None:
    group = await _seed_proposal(db_session, nonce="inbox-deny1", symbol="D1")
    assert str(group.proposal_id) in await _inbox_ids(db_session, datetime.now(UTC))

    result = await handle_web_approval(
        group.proposal_id,
        action="deny",
        actor_subject="user:9",
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
    )
    assert result["handled"] is True

    assert str(group.proposal_id) not in await _inbox_ids(db_session, datetime.now(UTC))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_loss_cut_is_listed_as_two_step_and_leaves_after_first_click(
    db_session, monkeypatch
) -> None:
    group = await _seed_loss_cut_proposal(db_session, monkeypatch, nonce="inbox-lcut1")
    now = datetime.now(UTC)
    listed = await TraderApprovalInboxService(db_session, now=now).list_inbox()
    item = next(i for i in listed.items if i.proposal_id == str(group.proposal_id))
    assert item.requires_two_step is True
    assert "loss_cut_two_step" in item.caveats

    monkeypatch.setattr(
        "app.services.order_proposals.telegram_callback.evaluate_approval_window",
        allow_known_session,
    )
    first = await handle_web_approval(
        group.proposal_id,
        action="approve",
        actor_subject="user:9",
        now=datetime.now(UTC),
        service_factory=_session_factory(db_session),
        loss_cut_preview_fn=_fake_loss_cut_preview,
    )
    assert first["reason"] == "loss_cut_confirmation_required"

    # The confirmation step is bound to the clicking browser's token; the
    # inbox must not offer a fresh first-step button for it.
    assert str(group.proposal_id) not in await _inbox_ids(db_session, datetime.now(UTC))
