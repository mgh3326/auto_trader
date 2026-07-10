import datetime

import pytest

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
