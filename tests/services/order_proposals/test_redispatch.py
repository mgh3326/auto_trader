from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.order_proposals.approval_window import (
    ApprovalWindowDecision,
    SubmissionSessionEvidence,
)
from app.services.order_proposals.approval_window_contract import ApprovalWindowCode
from app.services.order_proposals.redispatch import validate_proposal_redispatch

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


def _group(**overrides):
    values = {
        "proposal_id": uuid.UUID("d4444444-4444-4444-8444-444444444444"),
        "lifecycle_state": "proposed",
        "action": "place",
        "order_type": "limit",
        "approval_nonce": None,
        "approval_nonce_used_at": None,
        "approval_dispatch_state": "failed",
        "approval_dispatch_published_at": None,
        "valid_until": NOW + timedelta(hours=1),
        "market": "equity_kr",
        "account_mode": "kis_live",
        "symbol": "052690",
        "side": "sell",
        "thesis": "premarket exit",
        "strategy": "manual",
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
        "side": "sell",
        "quantity": Decimal("1"),
        "limit_price": Decimal("70000"),
        "state": "pending_approval",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _allow(group, *, now):
    return ApprovalWindowDecision(
        code=ApprovalWindowCode.ALLOW,
        observed_at=now,
        valid_until=group.valid_until,
        policy_stamp="order-proposal-approval-window-v1:test",
        market=group.market,
        account_mode=group.account_mode,
        action=group.action,
        order_type=group.order_type,
        evidence=SubmissionSessionEvidence(
            known=True,
            source="test",
            current_session="regular",
            allowed_sessions=("regular",),
            allowed_now=True,
            allowed_until=group.valid_until,
        ),
    )


def _preview(**overrides):
    values = {
        "success": True,
        "price": "70000",
        "quantity": "1",
        "current_price": "69000",
    }
    values.update(overrides)
    return values


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redispatch_validation_accepts_exact_fresh_limit_terms() -> None:
    place = AsyncMock(return_value=_preview())

    result = await validate_proposal_redispatch(
        group=_group(),
        rungs=[_rung()],
        now=NOW,
        now_fn=lambda: NOW,
        window_evaluator=_allow,
        place_order_fn=place,
    )

    assert result.eligible is True
    assert result.failure_code is None
    place.assert_awaited_once()
    assert place.await_args.kwargs["dry_run"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redispatch_rejects_expired_without_preview() -> None:
    place = AsyncMock(return_value=_preview())
    group = _group(valid_until=NOW)

    result = await validate_proposal_redispatch(
        group=group,
        rungs=[_rung()],
        now=NOW,
        now_fn=lambda: NOW,
        window_evaluator=_allow,
        place_order_fn=place,
    )

    assert result.eligible is False
    assert result.failure_code == "EXPIRED/now_at_or_after_valid_until"
    place.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redispatch_rejects_fresh_preview_price_change() -> None:
    result = await validate_proposal_redispatch(
        group=_group(),
        rungs=[_rung()],
        now=NOW,
        now_fn=lambda: NOW,
        window_evaluator=_allow,
        place_order_fn=AsyncMock(return_value=_preview(price="69500")),
    )

    assert result.eligible is False
    assert result.failure_code == "redispatch_price_changed"
    assert result.detail["expected_price"] == "70000"
    assert result.detail["preview_price"] == "69500"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redispatch_rejects_already_approved_or_cancelled_rungs() -> None:
    for group, rung in (
        (_group(lifecycle_state="approved"), _rung(state="approved")),
        (_group(lifecycle_state="terminal"), _rung(state="cancelled")),
    ):
        result = await validate_proposal_redispatch(
            group=group,
            rungs=[rung],
            now=NOW,
            now_fn=lambda: NOW,
            window_evaluator=_allow,
            place_order_fn=AsyncMock(return_value=_preview()),
        )
        assert result.eligible is False
        assert result.failure_code == "redispatch_proposal_not_active"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redispatch_rejects_current_dispatch_without_preview() -> None:
    place = AsyncMock(return_value=_preview())

    result = await validate_proposal_redispatch(
        group=_group(approval_dispatch_state="sent_current"),
        rungs=[_rung()],
        now=NOW,
        now_fn=lambda: NOW,
        window_evaluator=_allow,
        place_order_fn=place,
    )

    assert result.eligible is False
    assert result.failure_code == "redispatch_already_sent"
    place.assert_not_awaited()
