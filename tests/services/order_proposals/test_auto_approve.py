from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    evaluate_auto_approve_eligibility,
)
from app.services.order_proposals.service import RungInput


def _group(**overrides):
    values = {
        "market": "equity_kr",
        "account_mode": "kis_live",
        "broker_account_id": "acct-1",
        "order_type": "limit",
        "action": "place",
        "exit_intent": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(**overrides):
    values = {
        "side": "buy",
        "limit_price": Decimal("97000"),
        "quantity": Decimal("2"),
        "notional": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


_LIMITS = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("200000"),
    daily_cap=Decimal("500000"),
    policy_version="test-policy",
)


def test_buy_at_distance_and_daily_cap_boundary_is_eligible():
    decision = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("306000"),
    )

    assert decision.eligible is True
    assert decision.reason == "eligible"
    assert decision.details["policy_version"] == "test-policy"
    assert decision.details["daily_notional_after"] == "500000"


def test_sell_requires_distance_and_previewed_loss_guard():
    eligible = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(side="sell", limit_price=Decimal("103000"), quantity=Decimal("1")),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("0"),
    )
    blocked = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(side="sell", limit_price=Decimal("103000"), quantity=Decimal("1")),
        preview={
            "success": False,
            "current_price": "100000",
            "error": "sell price below average purchase price floor",
        },
        limits=_LIMITS,
        daily_notional=Decimal("0"),
    )

    assert eligible.eligible is True
    assert eligible.details["loss_guard"] == "preview_passed"
    assert blocked.eligible is False
    assert blocked.reason == "preview_guard_failed"


@pytest.mark.parametrize(
    ("group_overrides", "rung_overrides", "expected_reason"),
    [
        ({"order_type": "market"}, {}, "order_type_not_limit"),
        ({"action": "replace"}, {}, "action_not_place"),
        ({"action": "cancel"}, {}, "action_not_place"),
        ({"exit_intent": "loss_cut"}, {"side": "sell"}, "exit_intent_present"),
        ({"account_mode": "toss_live"}, {}, "account_not_veto_capable"),
        ({}, {"limit_price": Decimal("98000")}, "distance_below_minimum"),
        ({}, {"quantity": Decimal("3")}, "per_order_cap_exceeded"),
    ],
)
def test_ineligible_orders_fail_closed(
    group_overrides, rung_overrides, expected_reason
):
    decision = evaluate_auto_approve_eligibility(
        group=_group(**group_overrides),
        rung=_rung(**rung_overrides),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is False
    assert decision.reason == expected_reason


def test_daily_cap_one_unit_over_boundary_is_ineligible():
    decision = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("306001"),
    )

    assert decision.eligible is False
    assert decision.reason == "daily_cap_exceeded"


@pytest.mark.asyncio
async def test_daily_notional_uses_auto_approval_time_not_create_time(db_session):
    service = OrderProposalsService(db_session)
    now = datetime.now(UTC)
    account_id = f"daily-{uuid.uuid4()}"

    for approved_at in (now, now - timedelta(days=1)):
        await service.create_proposal(
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            broker_account_id=account_id,
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("200000"), None)],
            source_asof={
                "auto_approved": {
                    "policy_version": "test-policy",
                    "approved_at": approved_at.isoformat(),
                    "eligibility": [],
                    "outcomes": ["submitted_resting"],
                }
            },
        )
    await db_session.commit()
    probe = await service.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id=account_id,
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("1"), None)],
    )

    total = await service.auto_approved_daily_notional(probe, now=now)

    assert total == Decimal("200000")
