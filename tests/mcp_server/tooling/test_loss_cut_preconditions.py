import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import order_validation as ov
from app.mcp_server.tooling.order_validation import (
    DefensiveTrimContext,
    LossCutContext,
    ScalpingExitContext,
    evaluate_sell_price_guards,
)


def _loss_cut_ctx(max_slip=0.02):
    return LossCutContext(
        retrospective_id=1,
        exit_reason="stop_loss",
        approval_issue_id="ROB-800",
        requester_agent_id="agent-x",
        max_slip=max_slip,
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )


@pytest.mark.unit
def test_loss_cut_allows_price_within_slip_band():
    # current 1245, slip 0.02 -> floor 1220.1; price 1244 (below current, below avg*1.01) allowed
    err = evaluate_sell_price_guards(
        price=1244.0,
        current_price=1245.0,
        avg_price=2000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        loss_cut_ctx=_loss_cut_ctx(),
    )
    assert err is None


@pytest.mark.unit
def test_loss_cut_blocks_below_slip_band():
    # floor = 1245 * 0.98 = 1220.1; price 1200 is a fat-finger -> blocked
    err = evaluate_sell_price_guards(
        price=1200.0,
        current_price=1245.0,
        avg_price=2000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        loss_cut_ctx=_loss_cut_ctx(),
    )
    assert err is not None and "band" in err.lower()


@pytest.mark.unit
def test_defensive_trim_unchanged_still_enforces_current_price():
    # defensive_trim exempts floor but NOT current price -> price below current still blocked
    dt = DefensiveTrimContext(
        approval_issue_id="ROB-164",
        requester_agent_id="a",
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )
    err = evaluate_sell_price_guards(
        price=1244.0,
        current_price=1245.0,
        avg_price=2000.0,
        defensive_trim_ctx=dt,
        scalping_exit_ctx=None,
        loss_cut_ctx=None,
    )
    assert err is not None and "below current price" in err


@pytest.mark.unit
def test_plain_sell_unchanged_enforces_floor():
    err = evaluate_sell_price_guards(
        price=1990.0,
        current_price=1245.0,
        avg_price=2000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        loss_cut_ctx=None,
    )
    assert err is not None and "below minimum" in err


@pytest.mark.unit
def test_scalping_and_allow_loss_sell_unchanged_bypass_all():
    sc = ScalpingExitContext(strategy_id="s", reason="stop_loss")
    assert (
        evaluate_sell_price_guards(
            price=1.0,
            current_price=1245.0,
            avg_price=2000.0,
            defensive_trim_ctx=None,
            scalping_exit_ctx=sc,
            loss_cut_ctx=None,
        )
        is None
    )
    assert (
        evaluate_sell_price_guards(
            price=1.0,
            current_price=1245.0,
            avg_price=2000.0,
            defensive_trim_ctx=None,
            scalping_exit_ctx=None,
            loss_cut_ctx=None,
            allow_loss_sell=True,
        )
        is None
    )


# ---------------------------------------------------------------------------
# ROB-800 Task 4: aggregating precondition validator
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loss_cut_preconditions_collects_all_violations():
    # side=buy, order_type=market, no exit_reason, bad approval fmt, no retrospective_id,
    # caller not allowlisted -> every violation surfaced in one list.
    with patch.object(ov, "get_caller_agent_id", return_value="not-allowed"):
        ctx, errors = await ov._validate_loss_cut_preconditions(
            exit_intent="loss_cut",
            retrospective_id=None,
            exit_reason=None,
            approval_issue_id="bad-id",
            side="buy",
            order_type="market",
            is_mock=False,
            symbol="KRW-DOT",
        )
    assert ctx is None
    joined = " | ".join(errors)
    assert "side='sell'" in joined
    assert "order_type='limit'" in joined
    assert "exit_reason" in joined
    assert "retrospective_id" in joined
    assert "approval_issue_id" in joined
    assert "not permitted" in joined
    assert len(errors) >= 6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loss_cut_preconditions_pass_builds_context():
    fake_retro = type(
        "R",
        (),
        {
            "id": 42,
            "symbol": "KRW-DOT",
            "trigger_type": "stop_loss",
            "created_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
        },
    )()
    with (
        patch.object(
            ov,
            "get_caller_agent_id",
            return_value="6b2192cc-14fa-4335-b572-2fe1e0cb54a7",
        ),
        patch.object(
            ov,
            "_fetch_approval_issue_status",
            new=AsyncMock(return_value="done"),
        ),
        patch.object(
            ov,
            "_get_retrospective_by_id_for_loss_cut",
            new=AsyncMock(return_value=fake_retro),
        ),
    ):
        ctx, errors = await ov._validate_loss_cut_preconditions(
            exit_intent="loss_cut",
            retrospective_id=42,
            exit_reason="stop_loss",
            approval_issue_id="ROB-800",
            side="sell",
            order_type="limit",
            is_mock=False,
            symbol="KRW-DOT",
        )
    assert errors == []
    assert ctx is not None and ctx.retrospective_id == 42 and ctx.max_slip > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loss_cut_preconditions_reject_stale_retrospective():
    old = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ) - __import__("datetime").timedelta(hours=100)
    fake_retro = type(
        "R",
        (),
        {
            "id": 42,
            "symbol": "KRW-DOT",
            "trigger_type": "stop_loss",
            "created_at": old,
        },
    )()
    with (
        patch.object(
            ov,
            "get_caller_agent_id",
            return_value="6b2192cc-14fa-4335-b572-2fe1e0cb54a7",
        ),
        patch.object(
            ov,
            "_fetch_approval_issue_status",
            new=AsyncMock(return_value="done"),
        ),
        patch.object(
            ov,
            "_get_retrospective_by_id_for_loss_cut",
            new=AsyncMock(return_value=fake_retro),
        ),
    ):
        ctx, errors = await ov._validate_loss_cut_preconditions(
            exit_intent="loss_cut",
            retrospective_id=42,
            exit_reason="stop_loss",
            approval_issue_id="ROB-800",
            side="sell",
            order_type="limit",
            is_mock=False,
            symbol="KRW-DOT",
        )
    assert ctx is None
    assert any("72h" in e or "stale" in e.lower() for e in errors)
