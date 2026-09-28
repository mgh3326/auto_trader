"""Task 817: offline evidence for proposal-bound parking liquidation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.mcp_server.tooling import (
    order_execution,
    order_validation,
    orders_toss_variants,
)
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    evaluate_auto_approve_eligibility,
)
from app.services.order_proposals.parking_allowlist import ParkingExposure
from app.services.order_proposals.parking_sell_exemption import (
    ParkingSellContext,
    bind_parking_sell_context,
    kr_regular_session_open,
)
from app.services.order_proposals.service import RungInput
from tests.services.order_proposals.test_dispatch import _FakeNotifier, _session_factory
from tests.services.order_proposals.window_fakes import allow_known_session

SCOPES = (
    ("SGOV", "kis_live", "equity_us", "100", "99"),
    ("BIL", "kis_live", "equity_us", "100", "99"),
    ("459580", "kis_live", "equity_kr", "1000", "990"),
    ("357870", "kis_live", "equity_kr", "1000", "990"),
    ("SGOV", "toss_live", "equity_us", "100", "99"),
    ("BIL", "toss_live", "equity_us", "100", "99"),
    ("459580", "toss_live", "equity_kr", "1000", "990"),
    ("357870", "toss_live", "equity_kr", "1000", "990"),
)
REGULAR = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)


def _account(monkeypatch, mode: str) -> str:
    if mode == "kis_live":
        monkeypatch.setattr(settings, "kis_account_no", "12345678-01")
        return "12345678-01"
    monkeypatch.setattr(settings, "toss_api_account_seq", 7)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED", True)
    return "7"


def _group(symbol, mode, market, account, **changes):
    values = {
        "proposal_id": uuid.uuid4(),
        "symbol": symbol,
        "account_mode": mode,
        "market": market,
        "broker_account_id": account,
        "side": "sell",
        "order_type": "limit",
        "action": "place",
        "exit_intent": None,
        "thesis": "Sell parked cash after operator instruction.",
        "source_asof": {},
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _rung(price="99", quantity="2", **changes):
    values = {
        "rung_index": 0,
        "side": "sell",
        "limit_price": Decimal(price),
        "quantity": Decimal(quantity),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _limits(market):
    return AutoApproveLimits(
        min_distance_pct=Decimal("3"),
        per_order_cap=Decimal("2000000" if market == "equity_kr" else "1500"),
        daily_cap=Decimal("5000000" if market == "equity_kr" else "20000"),
        policy_version="test-817",
        mode="expanded",
        breakeven_band_pct=Decimal("1"),
        round_trip_cost_bps=Decimal("90"),
    )


def _decision(group, rung, *, current, preview=None, now=REGULAR, exposure="default"):
    if exposure == "default":
        exposure = (
            ParkingExposure.observed(Decimal("0")) if rung.side == "sell" else None
        )
    return evaluate_auto_approve_eligibility(
        group=group,
        rung=rung,
        preview=preview
        or {
            "success": True,
            "parking_sell_exempt": True,
            "current_price": current,
            "avg_buy_price": str(Decimal(current) * Decimal("1.1")),
            "realized_pnl": "-20",
        },
        limits=_limits(group.market),
        daily_notional=Decimal("0"),
        parking_exposure=exposure,
        now=now,
    )


@pytest.mark.parametrize("symbol,mode,market,current,price", SCOPES)
def test_each_exact_tuple_auto_approves_loss_or_breakeven_without_target(
    monkeypatch, symbol, mode, market, current, price
):
    account = _account(monkeypatch, mode)
    group = _group(symbol, mode, market, account)
    rung = _rung(price)

    token = bind_parking_sell_context(group, rung)
    assert token is not None
    assert token.matches(
        symbol=symbol,
        market=market,
        account_mode=mode,
        side="sell",
        order_type="limit",
        quantity=Decimal("2"),
        price=Decimal(price),
    )
    decision = _decision(group, rung, current=current)
    assert (decision.eligible, decision.reason) == (True, "eligible")
    assert decision.details["loss_guard"] == "parking_sell_exempt"
    assert decision.details["marketability"] == "parking_sell_marketable"
    assert decision.details["per_order_cap"] == (
        "10000000" if market == "equity_kr" else "10000"
    )


@pytest.mark.parametrize("symbol,mode,market,current,price", SCOPES)
def test_explicit_account_is_required_and_must_match(
    monkeypatch, symbol, mode, market, current, price
):
    account = _account(monkeypatch, mode)
    rung = _rung(price)
    for invalid in (None, "", "wrong", "  " + account, account + " "):
        group = _group(symbol, mode, market, invalid)
        assert bind_parking_sell_context(group, rung) is None
        result = _decision(group, rung, current=current)
        assert (result.eligible, result.reason) == (
            False,
            "parking_sell_account_identity_unavailable",
        )


def test_other_symbol_side_tuple_action_and_order_type_do_not_inherit_exemption(
    monkeypatch,
):
    account = _account(monkeypatch, "kis_live")
    rung = _rung()
    for group, changed_rung, expected in (
        (
            _group("SPY", "kis_live", "equity_us", account),
            rung,
            "expected_pnl_not_positive",
        ),
        (
            _group("SGOV", "kis_live", "equity_kr", account),
            rung,
            "expected_pnl_not_positive",
        ),
        (
            _group("SGOV", "kis_mock", "equity_us", account),
            rung,
            "account_not_veto_capable",
        ),
        (
            _group("SGOV", "kis_live", "equity_us", account),
            _rung(side="buy"),
            "parking_exposure_unavailable",
        ),
        (
            _group("SGOV", "kis_live", "equity_us", account, action="amend"),
            rung,
            "action_not_supported",
        ),
        (
            _group("SGOV", "kis_live", "equity_us", account, order_type="market"),
            rung,
            "order_type_not_limit",
        ),
    ):
        assert bind_parking_sell_context(group, changed_rung) is None
        result = _decision(group, changed_rung, current="100")
        assert (result.eligible, result.reason) == (False, expected)


def test_retained_cap_tag_veto_and_fat_finger_gates(monkeypatch):
    account = _account(monkeypatch, "kis_live")
    group = _group("SGOV", "kis_live", "equity_us", account)
    cases = (
        (group, _rung(quantity="101"), "per_order_cap_exceeded"),
        (
            _group(
                "SGOV",
                "kis_live",
                "equity_us",
                account,
                rationale={"deep": ["policy_deviation"]},
            ),
            _rung(),
            "approval_required_tag",
        ),
        (
            _group("SGOV", "kis_live", "equity_us", account, thesis="  "),
            _rung(),
            "thesis_required_for_veto_card",
        ),
        (group, _rung(price="97"), "parking_sell_price_band_failed"),
    )
    for candidate, rung, expected in cases:
        result = _decision(candidate, rung, current="100")
        assert (result.eligible, result.reason) == (False, expected)
    assert (
        _decision(
            group,
            _rung(),
            current="100",
            preview={"success": True, "current_price": "100"},
        ).reason
        == "parking_sell_preview_binding_missing"
    )
    assert (
        _decision(
            group,
            _rung(),
            current="100",
            preview={
                "success": False,
                "parking_sell_exempt": True,
                "current_price": "100",
            },
        ).reason
        == "preview_guard_failed"
    )


def test_unavailable_account_meter_demotes_parking_sell(monkeypatch):
    account = _account(monkeypatch, "toss_live")
    group = _group("SGOV", "toss_live", "equity_us", account)
    for exposure in (None, ParkingExposure.unavailable("account_lookup_failed")):
        result = _decision(group, _rung(), current="100", exposure=exposure)
        assert (result.eligible, result.reason) == (
            False,
            "parking_exposure_unavailable",
        )


@pytest.mark.parametrize(
    "when",
    (
        datetime(2026, 9, 27, 23, 59, tzinfo=UTC),  # pre-open/NXT
        datetime(2026, 9, 28, 6, 30, tzinfo=UTC),  # 15:30 KST boundary
        datetime(2026, 9, 28, 8, 0, tzinfo=UTC),  # after/NXT
    ),
)
def test_kr_sells_require_xkrx_regular_session(monkeypatch, when):
    account = _account(monkeypatch, "kis_live")
    group = _group("459580", "kis_live", "equity_kr", account)
    assert not kr_regular_session_open("equity_kr", when)
    result = _decision(group, _rung("990"), current="1000", now=when)
    assert (result.eligible, result.reason) == (
        False,
        "parking_sell_regular_session_required",
    )


def test_shortened_session_close_is_exclusive(monkeypatch):
    from app.services.order_proposals import parking_sell_exemption as module

    monkeypatch.setattr(
        module,
        "regular_session_bounds",
        lambda _market, _date: (
            datetime(2026, 9, 28, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 28, 3, 0, tzinfo=UTC),
        ),
    )
    assert kr_regular_session_open(
        "equity_kr", datetime(2026, 9, 28, 2, 59, tzinfo=UTC)
    )
    assert not kr_regular_session_open(
        "equity_kr", datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
    )


@pytest.mark.asyncio
async def test_kis_preview_and_submit_sell_guards_keep_band(monkeypatch):
    account = _account(monkeypatch, "kis_live")
    ctx = bind_parking_sell_context(
        _group("SGOV", "kis_live", "equity_us", account), _rung()
    )
    assert ctx is not None

    async def holding(*_args, **_kwargs):
        return {"avg_price": 110.0, "quantity": 10.0, "total_quantity": 10.0}

    monkeypatch.setattr(order_validation, "_get_holdings_for_order", holding)
    preview = await order_validation._preview_sell(
        symbol="SGOV",
        order_type="limit",
        quantity=2.0,
        price=99.0,
        current_price=100.0,
        market_type="equity_us",
        parking_sell_ctx=ctx,
    )
    assert preview["parking_sell_exempt"] is True
    assert preview["realized_pnl"] < 0
    qty, avg, error = await order_validation._validate_sell_side(
        symbol="SGOV",
        normalized_symbol="SGOV",
        market_type="equity_us",
        quantity=2.0,
        order_type="limit",
        price=99.0,
        current_price=100.0,
        order_error_fn=lambda message: {"error": message},
        parking_sell_ctx=ctx,
    )
    assert (qty, avg, error) == (2.0, 110.0, None)
    _, _, deep_error = await order_validation._validate_sell_side(
        symbol="SGOV",
        normalized_symbol="SGOV",
        market_type="equity_us",
        quantity=2.0,
        order_type="limit",
        price=97.0,
        current_price=100.0,
        order_error_fn=lambda message: {"error": message},
        parking_sell_ctx=ctx,
    )
    assert "marketable band floor" in deep_error["error"]


@pytest.mark.asyncio
async def test_toss_preview_and_submit_sell_guard_keep_band(monkeypatch):
    account = _account(monkeypatch, "toss_live")
    ctx = bind_parking_sell_context(
        _group("SGOV", "toss_live", "equity_us", account), _rung()
    )
    assert ctx is not None

    class Holding:
        average_purchase_price = Decimal("110")

    async def holding(*_args):
        return Holding()

    monkeypatch.setattr(orders_toss_variants, "_find_holding", holding)
    passed = await orders_toss_variants._sell_loss_guard(
        object(),
        "SGOV",
        "limit",
        Decimal("99"),
        {},
        parking_sell_ctx=ctx,
        current_price=Decimal("100"),
    )
    blocked = await orders_toss_variants._sell_loss_guard(
        object(),
        "SGOV",
        "limit",
        Decimal("97"),
        {},
        parking_sell_ctx=ctx,
        current_price=Decimal("100"),
    )
    assert passed is None
    assert "marketable band floor" in blocked["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,mode,market,current,price", SCOPES)
async def test_offline_default_binding_previews_and_submits_each_tuple(
    monkeypatch, symbol, mode, market, current, price
):
    """Exercise production proposal routing with fake broker functions only."""
    account = _account(monkeypatch, mode)
    ctx = bind_parking_sell_context(_group(symbol, mode, market, account), _rung(price))
    assert ctx is not None
    original_send_ready = ParkingSellContext.send_ready
    monkeypatch.setattr(
        ParkingSellContext,
        "send_ready",
        lambda self: original_send_ready(self, REGULAR),
    )
    calls = []
    hook_calls = []

    async def outer_hook():
        hook_calls.append("checked")

    async def kis_impl(**kwargs):
        assert kwargs["proposal_flow"] is True
        assert kwargs["parking_sell_ctx"] == ctx
        calls.append(kwargs["dry_run"])
        if kwargs["dry_run"]:
            return {
                "success": True,
                "price": float(price),
                "quantity": 2.0,
                "current_price": current,
                "avg_buy_price": str(Decimal(current) * Decimal("1.1")),
                "parking_sell_exempt": True,
            }
        await kwargs["pre_send_hook"]()
        return {"success": True, "broker_status": "accepted", "order_id": "offline-id"}

    async def toss_preview(**kwargs):
        bound = orders_toss_variants._order_proposal_context.get()
        assert bound is not None and bound.parking_sell_ctx == ctx
        calls.append(True)
        return {
            "success": True,
            "parking_sell_exempt": True,
            "current_price": current,
            "avg_buy_price": str(Decimal(current) * Decimal("1.1")),
            "approval_hash": "offline-hash",
            "payload_preview": {
                "price": price,
                "quantity": "2",
                "clientOrderId": "offline-client-id",
            },
        }

    async def toss_submit(**kwargs):
        bound = orders_toss_variants._order_proposal_context.get()
        assert bound is not None and bound.parking_sell_ctx == ctx
        assert kwargs["confirm"] is True and kwargs["dry_run"] is False
        calls.append(False)
        hook = orders_toss_variants._toss_pre_send_hook.get()
        assert hook is not None
        await hook()
        return {
            "success": True,
            "order_id": "offline-id",
            "approval_hash_digest": "offline-digest",
            "client_order_id": "offline-client-id",
        }

    monkeypatch.setattr(order_execution, "_place_order_impl", kis_impl)
    monkeypatch.setattr(orders_toss_variants, "toss_preview_order", toss_preview)
    monkeypatch.setattr(orders_toss_variants, "toss_place_order", toss_submit)
    common = {
        "account_mode": mode,
        "symbol": symbol,
        "side": "sell",
        "market": market,
        "order_type": "limit",
        "quantity": Decimal("2"),
        "price": Decimal(price),
        "exit_intent": None,
        "parking_sell_ctx": ctx,
        "proposal_client_order_id": "offline-client-id",
    }
    preview = await revalidation_module._default_place_order_fn(dry_run=True, **common)
    submit = await revalidation_module._default_place_order_fn(
        dry_run=False,
        pre_send_hook=outer_hook,
        approval_hash=preview.get("approval_hash"),
        **common,
    )
    assert preview["success"] is True and preview["parking_sell_exempt"] is True
    assert submit["success"] is True and submit["status"] == "resting"
    assert calls == [True, False]
    assert hook_calls == ["checked"]


@pytest.mark.asyncio
async def test_account_change_at_send_hook_blocks_mutation(monkeypatch):
    account = _account(monkeypatch, "kis_live")
    ctx = bind_parking_sell_context(
        _group("SGOV", "kis_live", "equity_us", account), _rung()
    )
    assert ctx is not None
    sent = []

    async def change_account():
        monkeypatch.setattr(settings, "kis_account_no", "another-account")

    async def fake_broker(**kwargs):
        await kwargs["pre_send_hook"]()
        sent.append(True)
        return {"success": True}

    monkeypatch.setattr(order_execution, "_place_order_impl", fake_broker)
    with pytest.raises(revalidation_module.PreSendFreshnessError):
        await revalidation_module._default_place_order_fn(
            account_mode="kis_live",
            symbol="SGOV",
            side="sell",
            market="equity_us",
            order_type="limit",
            quantity=Decimal("2"),
            price=Decimal("99"),
            exit_intent=None,
            dry_run=False,
            pre_send_hook=change_account,
            parking_sell_ctx=ctx,
        )
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", (None, "mismatched-account"))
async def test_missing_account_dispatches_usable_manual_card(
    monkeypatch, db_session, account
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE_MODE", "expanded")
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "chat-817"
    )
    monkeypatch.setattr(settings, "kis_account_no", "12345678-01")
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="SGOV",
        market="equity_us",
        account_mode="kis_live",
        broker_account_id=account,
        side="sell",
        order_type="limit",
        proposer="test",
        thesis="Operator instructed liquidation of parked USD.",
        rungs=[RungInput(0, "sell", Decimal("2"), Decimal("99"), None)],
        now=REGULAR,
    )
    await db_session.commit()
    notifier = _FakeNotifier()
    result = await dispatch_module.dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=REGULAR,
        service_factory=_session_factory(db_session),
        window_evaluator=allow_known_session,
    )
    assert result.ok is True
    assert notifier.sent_messages
    keyboard = notifier.sent_messages[-1][1]
    assert any(
        button.get("callback_data")
        for row in keyboard["inline_keyboard"]
        for button in row
    )
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.approval_nonce is not None
    assert rungs[0].state == "pending_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,reason",
    (
        ("cap", "per_order_cap_exceeded"),
        ("tag", "approval_required_tag"),
        ("band", "parking_sell_price_band_failed"),
        ("session", "parking_sell_regular_session_required"),
        ("veto", "thesis_required_for_veto_card"),
        ("account", "parking_sell_account_identity_unavailable"),
    ),
)
async def test_retained_guard_rejects_before_offline_submit(
    monkeypatch, db_session, case, reason
):
    account = _account(monkeypatch, "kis_live")
    market = "equity_kr" if case == "session" else "equity_us"
    symbol = "459580" if case == "session" else "SGOV"
    current = "1000" if case == "session" else "100"
    price = "990" if case == "session" else "97" if case == "band" else "99"
    quantity = "101" if case == "cap" else "2"
    when = datetime(2026, 9, 27, 23, 59, tzinfo=UTC) if case == "session" else REGULAR
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol=symbol,
        market=market,
        account_mode="kis_live",
        broker_account_id=None if case == "account" else account,
        side="sell",
        order_type="limit",
        proposer="test",
        thesis=(
            "policy_deviation"
            if case == "tag"
            else " "
            if case == "veto"
            else "Operator instruction to liquidate parked cash."
        ),
        rungs=[RungInput(0, "sell", Decimal(quantity), Decimal(price), None)],
        now=REGULAR,
    )
    await db_session.commit()
    calls = []

    async def fake_place(**kwargs):
        calls.append(kwargs["dry_run"])
        assert kwargs["dry_run"] is True, "guard must precede broker mutation"
        return {
            "success": True,
            "price": Decimal(price),
            "quantity": Decimal(quantity),
            "current_price": current,
            "avg_buy_price": str(Decimal(current) * Decimal("1.1")),
            "parking_sell_exempt": True,
        }

    async def gate(**kwargs):
        return evaluate_auto_approve_eligibility(
            group=kwargs["group"],
            rung=kwargs["rung"],
            preview=kwargs["preview"],
            limits=_limits(market),
            daily_notional=Decimal("0"),
            parking_exposure=ParkingExposure.observed(Decimal("0")),
            now=kwargs["now"],
        )

    observed = await allow_known_session(group, now=when)
    outcome = await revalidation_module.revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=when,
        place_order_fn=fake_place,
        eligibility_gate=gate,
        window_evaluator=allow_known_session,
        expected_policy_stamp=observed.policy_stamp,
        now_fn=lambda: when,
        parking_sell_auto_enabled=True,
    )
    assert calls == [True]
    assert outcome[0].result == "approval_required"
    assert outcome[0].detail["reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("kis_live", "toss_live"))
async def test_created_proposal_auto_dispatches_through_pre_send_with_fake_broker(
    monkeypatch, db_session, mode
):
    from app.services.order_proposals import parking_sell_exemption as module

    account = _account(monkeypatch, mode)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE_MODE", "expanded")
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "chat-817"
    )
    original_send_ready = module.ParkingSellContext.send_ready
    monkeypatch.setattr(
        module.ParkingSellContext,
        "send_ready",
        lambda self: original_send_ready(self, REGULAR),
    )

    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="SGOV",
        market="equity_us",
        account_mode=mode,
        broker_account_id=account,
        side="sell",
        order_type="limit",
        proposer="test",
        thesis="Operator instruction to liquidate parked USD.",
        rungs=[RungInput(0, "sell", Decimal("2"), Decimal("99"), None)],
        now=REGULAR,
    )
    await db_session.commit()
    persisted, persisted_rungs = await service.get_proposal(group.proposal_id)
    assert bind_parking_sell_context(persisted, persisted_rungs[0]) is not None, (
        type(persisted.proposal_id),
        type(persisted_rungs[0].rung_index),
        persisted.broker_account_id,
        persisted.action,
    )

    async def exposure(**_kwargs):
        return ParkingExposure.observed(Decimal("0"))

    monkeypatch.setattr(dispatch_module, "load_parking_exposure", exposure)
    calls = []

    async def kis_impl(**kwargs):
        ctx = kwargs["parking_sell_ctx"]
        assert ctx is not None and ctx.broker_account_id == account
        calls.append(kwargs["dry_run"])
        if kwargs["dry_run"]:
            return {
                "success": True,
                "price": 99.0,
                "quantity": 2.0,
                "current_price": 100.0,
                "avg_buy_price": 110.0,
                "parking_sell_exempt": True,
                "approval_hash": "offline-hash",
            }
        await kwargs["pre_send_hook"]()
        return {
            "success": True,
            "broker_status": "accepted",
            "order_id": "offline-order",
            "correlation_id": kwargs["correlation_id"],
        }

    async def toss_preview(**_kwargs):
        ctx = orders_toss_variants._order_proposal_context.get()
        assert ctx is not None and ctx.parking_sell_ctx is not None
        calls.append(True)
        return {
            "success": True,
            "parking_sell_exempt": True,
            "current_price": "100",
            "avg_buy_price": "110",
            "approval_hash": "offline-hash",
            "payload_preview": {
                "price": "99",
                "quantity": "2",
                "clientOrderId": ctx.client_order_id,
            },
        }

    async def toss_submit(**_kwargs):
        ctx = orders_toss_variants._order_proposal_context.get()
        assert ctx is not None and ctx.parking_sell_ctx is not None
        calls.append(False)
        hook = orders_toss_variants._toss_pre_send_hook.get()
        assert hook is not None
        await hook()
        return {
            "success": True,
            "order_id": "offline-order",
            "approval_hash_digest": "offline-digest",
            "client_order_id": ctx.client_order_id,
            "correlation_id": ctx.correlation_id,
        }

    monkeypatch.setattr(order_execution, "_place_order_impl", kis_impl)
    monkeypatch.setattr(orders_toss_variants, "toss_preview_order", toss_preview)
    monkeypatch.setattr(orders_toss_variants, "toss_place_order", toss_submit)
    notifier = _FakeNotifier()
    result = await dispatch_module.dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=REGULAR,
        service_factory=_session_factory(db_session),
        window_evaluator=allow_known_session,
        now_fn=lambda: REGULAR,
    )
    assert result.ok is True
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert calls == [True, False]
    assert rungs[0].state == "resting"
    assert (
        refreshed.source_asof["auto_approved"]["eligibility"][0]["loss_guard"]
        == "parking_sell_exempt"
    )
