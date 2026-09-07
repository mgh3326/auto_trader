"""§S177 execution guards and direct-tool proposal-only boundary."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from app.mcp_server.tooling import (
    order_execution,
    orders_kis_variants,
    orders_registration,
    orders_toss_variants,
)
from app.mcp_server.tooling.order_validation import (
    CashFundingContext,
    LossCutContext,
    evaluate_market_sell_loss_guard,
    evaluate_sell_price_guards,
)
from app.services.order_proposals.cash_funding_exemption import CashFundingVerdict


def _cash_context() -> CashFundingContext:
    return CashFundingContext.from_verdict(
        CashFundingVerdict(
            exempt=True,
            reason="exempt",
            max_quantity=Decimal("2"),
            scope_currency="USD",
            details={},
        )
    )


def _loss_cut_context() -> LossCutContext:
    return LossCutContext(
        retrospective_id=1,
        exit_reason="stop_loss",
        approval_issue_id="ROB-test",
        requester_agent_id="test-agent",
        max_slip=0.02,
        approval_verified_at=datetime.datetime.now(datetime.UTC),
    )


def test_cash_funding_limit_guard_bypasses_only_avg_floor():
    cash = _cash_context()

    # 99 is below avg * 1.01 (=111.1) but inside the retained 2% fat-finger
    # band (>=98), so this proves the narrow intended exemption.
    assert (
        evaluate_sell_price_guards(
            price=99.0,
            current_price=100.0,
            avg_price=110.0,
            defensive_trim_ctx=None,
            scalping_exit_ctx=None,
            loss_cut_ctx=None,
            cash_funding_ctx=cash,
        )
        is None
    )
    retained_band = evaluate_sell_price_guards(
        price=97.0,
        current_price=100.0,
        avg_price=110.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        loss_cut_ctx=None,
        cash_funding_ctx=cash,
    )
    assert retained_band is not None
    assert "marketable band floor" in retained_band


def test_cash_funding_and_loss_cut_contexts_are_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        evaluate_sell_price_guards(
            price=99.0,
            current_price=100.0,
            avg_price=110.0,
            defensive_trim_ctx=None,
            scalping_exit_ctx=None,
            loss_cut_ctx=_loss_cut_context(),
            cash_funding_ctx=_cash_context(),
        )


def test_market_sell_keeps_existing_loss_guard_even_with_cash_context():
    blocked = evaluate_market_sell_loss_guard(
        current_price=100.0,
        avg_price=110.0,
        cash_funding_ctx=_cash_context(),
    )

    assert blocked is not None
    assert blocked.startswith("Live market sell blocked:")
    assert "Loss-selling is disabled" in blocked


@pytest.mark.asyncio
async def test_toss_guard_delegates_cash_context_to_shared_price_guard(monkeypatch):
    class Holding:
        average_purchase_price = Decimal("110")

    class Client:
        pass

    forwarded: dict[str, object] = {}

    async def find_holding(_client, _symbol):
        return Holding()

    def shared_guard(**kwargs):
        forwarded.update(kwargs)
        return None

    monkeypatch.setattr(orders_toss_variants, "_find_holding", find_holding)
    monkeypatch.setattr(
        orders_toss_variants, "evaluate_sell_price_guards", shared_guard
    )

    result = await orders_toss_variants._sell_loss_guard(
        Client(),
        "SGOV",
        "limit",
        Decimal("99"),
        {"success": True},
        cash_funding_ctx=_cash_context(),
        current_price=Decimal("100"),
    )

    assert result is None
    assert forwarded["cash_funding_ctx"] == _cash_context()
    assert forwarded["loss_cut_ctx"] is None


@pytest.mark.asyncio
async def test_order_execution_direct_cash_funding_points_to_proposal_create():
    result = await order_execution._place_order_impl(
        symbol="SGOV",
        side="sell",
        market="equity_us",
        order_type="limit",
        quantity=1,
        price=100,
        dry_run=True,
        exit_intent="cash_funding",
    )

    assert result["success"] is False
    assert (
        result["error"] == "cash_funding_direct_path_disabled_use_order_proposal_create"
    )


@pytest.mark.asyncio
async def test_toss_preview_and_place_direct_cash_funding_point_to_proposal_create(
    monkeypatch,
):
    monkeypatch.setattr(orders_toss_variants, "_entry_guard", lambda *_args: None)

    preview = await orders_toss_variants.toss_preview_order(
        symbol="SGOV",
        side="sell",
        order_type="limit",
        quantity="1",
        price="100",
        market="us",
        account_mode="toss_live",
        exit_intent="cash_funding",
    )
    place = await orders_toss_variants._toss_place_order_impl(
        symbol="SGOV",
        side="sell",
        order_type="limit",
        quantity="1",
        price="100",
        market="us",
        account_mode="toss_live",
        exit_intent="cash_funding",
    )

    assert (
        preview["error"]
        == "cash_funding_direct_path_disabled_use_order_proposal_create"
    )
    assert (
        place["error"] == "cash_funding_direct_path_disabled_use_order_proposal_create"
    )


@pytest.mark.asyncio
async def test_kis_variant_direct_cash_funding_points_to_proposal_create():
    result = await orders_kis_variants._place_order_variant(
        tool_name="kis_live_place_order",
        pinned_mode="kis_live",
        symbol="SGOV",
        side="sell",
        order_type="limit",
        quantity=1,
        price=100,
        amount=None,
        dry_run=True,
        reason="",
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        defensive_trim=False,
        approval_issue_id=None,
        exit_intent="cash_funding",
        retrospective_id=None,
        account_mode=None,
        account_type=None,
    )

    assert result["success"] is False
    assert (
        result["error"] == "cash_funding_direct_path_disabled_use_order_proposal_create"
    )


@pytest.mark.asyncio
async def test_generic_registration_direct_cash_funding_points_to_proposal_create():
    captured: dict[str, object] = {}

    class MCP:
        def tool(self, name: str, description: str, **_options):
            del description

            def decorator(function):
                captured[name] = function
                return function

            return decorator

    orders_registration.register_order_tools(MCP())
    place_order = captured["place_order"]
    result = await place_order(
        symbol="SGOV",
        side="sell",
        order_type="limit",
        quantity=1,
        price=100,
        exit_intent="cash_funding",
    )

    assert result["success"] is False
    assert (
        result["error"] == "cash_funding_direct_path_disabled_use_order_proposal_create"
    )
