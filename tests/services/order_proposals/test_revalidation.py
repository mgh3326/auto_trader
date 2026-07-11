import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.revalidation import (
    _adapt_live_submit_response,
    revalidate_and_submit,
)
from app.services.order_proposals.service import RungInput


def _bound_toss_context():
    import app.mcp_server.tooling.orders_toss_variants as toss

    context = toss._order_proposal_context.get()
    assert context is not None
    return context


def _fake_place_order(*, preview_price, preview_qty, submit_result):
    async def _fn(**kw):
        if kw.get("dry_run"):
            return {
                "success": True,
                "approval_hash": "TESTTOKEN",
                "price": str(preview_price),
                "quantity": str(preview_qty),
            }
        return submit_result

    return _fn


async def _create_proposal(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()
    return service, group


@pytest.mark.asyncio
async def test_unchanged_submits_resting(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()
    fn = _fake_place_order(
        preview_price=Decimal("2226000"),
        preview_qty=Decimal("10"),
        submit_result={
            "success": True,
            "status": "resting",
            "broker_order_id": "B1",
            "correlation_id": "c1",
            "idempotency_key": "k1",
            "approval_hash_digest": "d1",
        },
    )
    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert out[0].result == "submitted_resting"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "resting"
    assert rungs[0].filled_qty is None  # accepted != filled


@pytest.mark.asyncio
async def test_unchanged_submits_acked(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()
    fn = _fake_place_order(
        preview_price=Decimal("2226000"),
        preview_qty=Decimal("10"),
        submit_result={
            "success": True,
            "status": "acked",
            "broker_order_id": "B2",
            "correlation_id": "c2",
            "idempotency_key": "k2",
            "approval_hash_digest": "d2",
        },
    )
    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert out[0].result == "submitted_acked"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "acked"
    assert rungs[0].filled_qty is None  # accepted != filled


@pytest.mark.asyncio
async def test_price_change_needs_reconfirm_no_submit(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()
    fn = _fake_place_order(
        preview_price=Decimal("2340000"),
        preview_qty=Decimal("10"),
        submit_result={"success": True},  # should never be reached for submit
    )
    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert out[0].result == "needs_reconfirm"
    assert out[0].detail["before"]["limit_price"] == "2226000"
    assert out[0].detail["after"]["limit_price"] == "2340000"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "needs_reconfirm"


@pytest.mark.asyncio
async def test_qty_change_needs_reconfirm_no_submit(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="067160",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()
    fn = _fake_place_order(
        preview_price=Decimal("2226000"),
        preview_qty=Decimal("9"),
        submit_result={"success": True},  # should never be reached for submit
    )
    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert out[0].result == "needs_reconfirm"
    assert out[0].detail["before"]["quantity"] == "10"
    assert out[0].detail["after"]["quantity"] == "9"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "needs_reconfirm"


@pytest.mark.asyncio
async def test_guard_block_fail_closed(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("50"), None)],
    )
    await db_session.commit()

    async def blocked_fn(**kw):
        return {"success": False, "error": "loss_sell_blocked"}

    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=blocked_fn,
    )
    assert out[0].result == "guard_blocked"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "pending_approval"  # retryable, not submitted


@pytest.mark.asyncio
async def test_preview_internal_failure_keeps_error_label(db_session):
    service, group = await _create_proposal(db_session)

    async def internal_failure(**kwargs):
        return {
            "success": False,
            "error": "Order preview failed: unsupported operand type(s) for *",
        }

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=internal_failure,
    )
    assert outcomes[0].result == "error"


@pytest.mark.asyncio
async def test_preview_insufficient_cash_is_guard_blocked(db_session):
    service, group = await _create_proposal(db_session)

    async def insufficient_cash(**kwargs):
        return {
            "success": False,
            "error": "Insufficient KRW balance",
            "insufficient_balance": True,
        }

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=insufficient_cash,
    )
    assert outcomes[0].result == "guard_blocked"


@pytest.mark.asyncio
async def test_preview_kis_no_sellable_holdings_is_guard_blocked(db_session):
    service, group = await _create_proposal(db_session)

    async def no_sellable_holdings(**kwargs):
        return {
            "success": False,
            "error": (
                "No sellable holdings for A in the KIS subaccount that "
                "kis_live routes to."
            ),
        }

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=no_sellable_holdings,
    )
    assert outcomes[0].result == "guard_blocked"


@pytest.mark.asyncio
async def test_loss_cut_bindings_forwarded_to_preview_and_submit(
    db_session, monkeypatch
):
    async def fake_lookup(session, retrospective_id):
        return SimpleNamespace(
            symbol="005930", trigger_type="stop_loss", created_at=datetime.now(UTC)
        )

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("65000"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-800",
    )
    await db_session.commit()
    calls: list[dict] = []

    async def accepted(**kwargs):
        calls.append(kwargs)
        if kwargs["dry_run"]:
            return {
                "success": True,
                "approval_hash": "TESTTOKEN",
                "price": "65000",
                "quantity": "1",
            }
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "B-loss-cut",
        }

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=accepted,
    )

    expected = {
        "exit_intent": "loss_cut",
        "exit_reason": "stop_loss",
        "retrospective_id": 42,
        "approval_issue_id": "ROB-800",
    }
    assert outcomes[0].result == "submitted_resting"
    assert [call["dry_run"] for call in calls] == [True, False]
    assert [{key: call[key] for key in expected} for call in calls] == [
        expected,
        expected,
    ]


@pytest.mark.asyncio
async def test_upbit_loss_cut_default_binding_forwards_identity_and_exit_fields(
    db_session, monkeypatch
):
    from app.mcp_server.caller_identity import caller_agent_id_var, get_caller_agent_id
    from app.services.order_proposals import revalidation as mod

    async def fake_lookup(session, retrospective_id):
        return SimpleNamespace(
            symbol="KRW-DOT",
            trigger_type="stop_loss",
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
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
    await db_session.commit()
    calls: list[dict] = []

    async def fake_impl(**kwargs):
        calls.append(kwargs)
        assert get_caller_agent_id() == "proposal-agent"
        assert "account_mode" not in kwargs
        assert kwargs["market"] == "crypto"
        if kwargs["dry_run"]:
            return {
                "success": True,
                "approval_hash": "upbit-token",
                "price": "3200",
                "quantity": "0.1",
            }
        return {
            "success": True,
            "broker_status": "accepted",
            "order_id": "upbit-order",
            "correlation_id": "upbit-correlation",
            "idempotency_key": "upbit-client",
            "approval_hash_digest": "upbit-digest",
        }

    import app.mcp_server.tooling.order_execution as order_execution

    monkeypatch.setattr(order_execution, "_place_order_impl", fake_impl)
    token = caller_agent_id_var.set("proposal-agent")
    try:
        outcomes = await revalidate_and_submit(
            service=svc,
            proposal_id=group.proposal_id,
            now=datetime.now(UTC),
            place_order_fn=mod._default_place_order_fn,
        )
    finally:
        caller_agent_id_var.reset(token)

    assert outcomes[0].result == "submitted_resting"
    assert len(calls) == 2
    expected = {
        "exit_intent": "loss_cut",
        "exit_reason": "stop_loss",
        "retrospective_id": 42,
        "approval_issue_id": "ROB-800",
    }
    assert [{key: call[key] for key in expected} for call in calls] == [
        expected,
        expected,
    ]
    assert get_caller_agent_id() is None


@pytest.mark.asyncio
async def test_submit_rejected_records_rejected(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()
    fn = _fake_place_order(
        preview_price=Decimal("2226000"),
        preview_qty=Decimal("10"),
        submit_result={"success": False, "error": "broker_rejected"},
    )
    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert out[0].result == "error"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "rejected"


@pytest.mark.asyncio
async def test_submit_ambiguous_records_unverified(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()
    fn = _fake_place_order(
        preview_price=Decimal("2226000"),
        preview_qty=Decimal("10"),
        submit_result={"success": True, "status": "unknown"},
    )
    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert out[0].result == "unverified"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "unverified"


@pytest.mark.asyncio
async def test_submit_exception_records_unverified(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()

    async def flaky_fn(**kw):
        if kw.get("dry_run"):
            return {
                "success": True,
                "approval_hash": "TESTTOKEN",
                "price": "2226000",
                "quantity": "10",
            }
        raise TimeoutError("broker timeout")

    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=flaky_fn,
    )
    assert out[0].result == "unverified"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "unverified"


@pytest.mark.asyncio
async def test_only_pending_approval_rungs_are_revalidated(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(0, "buy", Decimal("10"), Decimal("100"), None),
            RungInput(1, "buy", Decimal("10"), Decimal("100"), None),
        ],
    )
    await db_session.commit()
    # rung 1 already moved on (e.g. previously submitted/acked) — simulate by
    # driving it through the state machine directly.
    await svc.transition_rung(g.proposal_id, 1, new_state="revalidating")
    await svc.transition_rung(g.proposal_id, 1, new_state="approved")
    await svc.transition_rung(g.proposal_id, 1, new_state="submitting")
    await svc.record_ack(
        g.proposal_id,
        1,
        broker_order_id="B-pre",
        correlation_id="c-pre",
        idempotency_key="k-pre",
        approval_hash_digest="d-pre",
        now=datetime.now(UTC),
    )
    await db_session.commit()

    fn = _fake_place_order(
        preview_price=Decimal("100"),
        preview_qty=Decimal("10"),
        submit_result={
            "success": True,
            "status": "resting",
            "broker_order_id": "B0",
            "correlation_id": "c0",
            "idempotency_key": "k0",
            "approval_hash_digest": "d0",
        },
    )
    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert len(out) == 1
    assert out[0].rung_index == 0
    assert out[0].result == "submitted_resting"
    _, rungs = await svc.get_proposal(g.proposal_id)
    by_index = {r.rung_index: r for r in rungs}
    assert by_index[0].state == "resting"
    assert by_index[1].state == "acked"  # untouched


# ---------------------------------------------------------------------------
# Finding 1 — `_adapt_live_submit_response` unit tests (no network; adapts a
# fixture dict shaped like the real `_record_kis_live_order`/
# `_record_live_order` accepted-only-at-send response into the
# `{status, broker_order_id}` shape `_classify_submit` expects).
# ---------------------------------------------------------------------------


def test_adapt_live_submit_response_accepted_market_is_acked():
    submit = {
        "success": True,
        "broker_status": "accepted",
        "order_id": "B-mkt",
        "correlation_id": "c-mkt",
    }
    adapted = _adapt_live_submit_response(submit, order_type="market")
    assert adapted["status"] == "acked"
    assert adapted["broker_order_id"] == "B-mkt"
    assert adapted["correlation_id"] == "c-mkt"
    assert adapted["success"] is True


def test_adapt_live_submit_response_accepted_limit_is_resting():
    submit = {
        "success": True,
        "broker_status": "accepted",
        "order_id": "B-lmt",
        "correlation_id": "c-lmt",
    }
    adapted = _adapt_live_submit_response(submit, order_type="limit")
    assert adapted["status"] == "resting"
    assert adapted["broker_order_id"] == "B-lmt"


def test_adapt_live_submit_response_rejected_flips_success_false():
    submit = {
        "success": True,  # the real ledger call always sets success=True...
        "broker_status": "rejected",
        "order_id": None,
        "response_message": "insufficient balance",
    }
    adapted = _adapt_live_submit_response(submit, order_type="limit")
    assert adapted["success"] is False
    assert adapted["error"] == "insufficient balance"


def test_adapt_live_submit_response_rejected_falls_back_to_message():
    submit = {
        "success": True,
        "broker_status": "rejected",
        "order_id": None,
        "message": "Live order not accepted (broker_status=rejected)",
    }
    adapted = _adapt_live_submit_response(submit, order_type="market")
    assert adapted["success"] is False
    assert adapted["error"] == "Live order not accepted (broker_status=rejected)"


def test_adapt_live_submit_response_unknown_broker_status_passes_through():
    submit = {"success": True, "broker_status": None}
    adapted = _adapt_live_submit_response(submit, order_type="limit")
    assert adapted == submit


# ---------------------------------------------------------------------------
# Finding 2 — market-order rungs must not be permanently stuck in
# needs_reconfirm just because the live preview backfills `price` with the
# current market price (rung.limit_price is always None for market orders).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_order_unchanged_quantity_submits(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="market",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), None, None)],
    )
    await db_session.commit()

    async def fn(**kw):
        if kw.get("dry_run"):
            return {
                "success": True,
                "approval_hash": "TESTTOKEN",
                "price": "2226000",  # live current price, no stored limit_price
                "quantity": "10",
            }
        return {
            "success": True,
            "status": "acked",
            "broker_order_id": "B-mkt",
            "correlation_id": "c-mkt",
            "idempotency_key": "k-mkt",
            "approval_hash_digest": "d-mkt",
        }

    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert out[0].result == "submitted_acked"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "acked"


@pytest.mark.asyncio
async def test_market_order_qty_change_still_needs_reconfirm(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="market",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), None, None)],
    )
    await db_session.commit()

    async def fn(**kw):
        if kw.get("dry_run"):
            return {
                "success": True,
                "approval_hash": "TESTTOKEN",
                "price": "2226000",
                "quantity": "9",  # changed
            }
        return {"success": True}  # should never be reached for submit

    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fn,
    )
    assert out[0].result == "needs_reconfirm"
    assert out[0].detail["before"]["limit_price"] is None
    assert out[0].detail["after"]["limit_price"] is None
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "needs_reconfirm"


# ---------------------------------------------------------------------------
# Finding 3 — a preview-phase exception must not orphan the rung in
# "revalidating"; it should be retryable back in pending_approval.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_exception_returns_to_pending_approval(db_session):
    svc = OrderProposalsService(db_session)
    g = await svc.create_proposal(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("2226000"), None)],
    )
    await db_session.commit()

    async def preview_raises_fn(**kw):
        if kw.get("dry_run"):
            raise ValueError("quote fetch failed")
        raise AssertionError("submit should never be reached")

    out = await revalidate_and_submit(
        service=svc,
        proposal_id=g.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=preview_raises_fn,
    )
    assert out[0].result == "error"
    _, rungs = await svc.get_proposal(g.proposal_id)
    assert rungs[0].state == "pending_approval"


class TestDefaultPlaceOrderFnDecimalCoercion:
    """2026-07-11 activation smoke regression: the proposal ledger hands
    Decimal quantity/limit_price to the default place_order binding, but
    `_place_order_impl`'s numeric paths (e.g. `_preview_buy` fee math)
    assume float — Decimal raised TypeError which was mislabeled as
    guard_blocked. The default fn must coerce Decimal kwargs to float."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_decimal_kwargs_coerced_to_float(self, monkeypatch):
        from decimal import Decimal

        from app.services.order_proposals import revalidation as mod

        seen: dict = {}

        async def fake_impl(**kwargs):
            seen.update(kwargs)
            return {
                "success": True,
                "price": kwargs["price"],
                "quantity": kwargs["quantity"],
            }

        import app.mcp_server.tooling.order_execution as oe

        monkeypatch.setattr(oe, "_place_order_impl", fake_impl)
        result = await mod._default_place_order_fn(
            dry_run=True,
            symbol="KRW-BTC",
            side="buy",
            market="crypto",
            order_type="limit",
            quantity=Decimal("0.0001"),
            price=Decimal("70000000"),
            reason="regression",
        )
        assert result["success"] is True
        assert isinstance(seen["quantity"], float)
        assert isinstance(seen["price"], float)
        assert seen["quantity"] == 0.0001
        assert seen["price"] == 70000000.0


def test_toss_decimal_args_are_exact_and_canonical():
    from app.services.order_proposals import revalidation as mod

    assert mod._toss_decimal_arg(None) is None
    assert mod._toss_decimal_arg(Decimal("10.000")) == 10
    assert mod._toss_decimal_arg(Decimal("0.125000")) == "0.125"
    assert mod._toss_decimal_arg(Decimal("0.000000000001")) == "0.000000000001"


def test_toss_proposal_client_ids_are_stable_and_rung_scoped():
    from app.services.order_proposals import revalidation as mod

    proposal_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    same = mod._toss_proposal_client_order_id(proposal_id, 0)
    assert same == mod._toss_proposal_client_order_id(proposal_id, 0)
    assert same != mod._toss_proposal_client_order_id(proposal_id, 1)
    assert same != mod._toss_proposal_client_order_id(uuid.uuid4(), 0)
    assert same.startswith("tosprop-")


@pytest.mark.asyncio
async def test_toss_kr_routes_preview_and_accepted_submit(db_session, monkeypatch):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10.000"), Decimal("222600"), None)],
    )
    await db_session.commit()
    calls: dict[str, dict] = {}

    async def fake_preview(**kwargs):
        calls["preview"] = kwargs
        calls["preview_context"] = _bound_toss_context()
        return {
            "success": True,
            "approval_hash": "preview-token",
            "payload_preview": {
                "price": "222600",
                "quantity": "10",
                "clientOrderId": calls["preview_context"].client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        calls["submit"] = kwargs
        calls["submit_context"] = _bound_toss_context()
        return {
            "success": True,
            "order_id": "toss-order-1",
            "client_order_id": calls["submit_context"].client_order_id,
            "approval_hash_digest": "canonical-ledger-digest",
        }

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        correlation_mint=lambda **_: "proposal-correlation",
    )

    assert outcomes[0].result == "submitted_resting"
    assert calls["preview"]["market"] == "kr"
    assert calls["preview"]["quantity"] == 10
    assert calls["preview"]["price"] == 222600
    assert calls["submit"]["dry_run"] is False
    assert calls["submit"]["confirm"] is True
    assert calls["submit"]["approval_hash"] == "preview-token"
    assert calls["submit"]["rung"] == 0
    assert (
        calls["preview_context"].client_order_id
        == calls["submit_context"].client_order_id
    )
    assert calls["submit_context"].correlation_id == "proposal-correlation"
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].broker_order_id == "toss-order-1"
    assert rungs[0].correlation_id == "proposal-correlation"
    assert rungs[0].idempotency_key == calls["preview_context"].client_order_id
    assert rungs[0].approval_hash_digest == "canonical-ledger-digest"


@pytest.mark.asyncio
async def test_toss_retry_across_dates_reuses_proposal_client_id(
    db_session, monkeypatch
):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("50000"), None)],
    )
    await db_session.commit()
    preview_ids: list[str] = []

    async def fake_preview(**kwargs):
        client_order_id = _bound_toss_context().client_order_id
        preview_ids.append(client_order_id)
        if len(preview_ids) == 1:
            raise RuntimeError("temporary preview failure")
        return {
            "success": True,
            "approval_hash": "retry-token",
            "payload_preview": {
                "price": "50000",
                "quantity": "1",
                "clientOrderId": client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        context = _bound_toss_context()
        return {
            "success": True,
            "broker_status": "accepted",
            "order_id": "retry-order",
            "client_order_id": context.client_order_id,
            "approval_hash_digest": "retry-digest",
        }

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    first = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime(2026, 7, 11, 14, 59, tzinfo=UTC),
    )
    second = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime(2026, 7, 12, 15, 1, tzinfo=UTC),
    )

    assert first[0].result == "error"
    assert second[0].result == "submitted_resting"
    assert preview_ids[0] == preview_ids[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "missing_payload",
        "malformed_payload",
        "missing_hash",
        "missing_client_id",
        "missing_quantity",
        "missing_limit_price",
        "mismatched_client_id",
    ],
)
async def test_toss_incomplete_preview_fails_closed(db_session, monkeypatch, defect):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("50000"), None)],
    )
    await db_session.commit()
    submitted = False

    async def fake_preview(**kwargs):
        context = _bound_toss_context()
        preview = {
            "success": True,
            "approval_hash": "capability-token",
            "payload_preview": {
                "price": "50000",
                "quantity": "1",
                "clientOrderId": context.client_order_id,
            },
        }
        if defect == "missing_payload":
            preview.pop("payload_preview")
        elif defect == "malformed_payload":
            preview["payload_preview"] = []
        elif defect == "missing_hash":
            preview.pop("approval_hash")
        elif defect == "missing_client_id":
            preview["payload_preview"].pop("clientOrderId")
        elif defect == "missing_quantity":
            preview["payload_preview"].pop("quantity")
        elif defect == "missing_limit_price":
            preview["payload_preview"].pop("price")
        else:
            preview["payload_preview"]["clientOrderId"] = "external-client-id"
        return preview

    async def fake_submit(**kwargs):
        nonlocal submitted
        submitted = True
        raise AssertionError("incomplete preview must never submit")

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
    )

    assert outcomes[0].result == "error"
    assert outcomes[0].detail["error"].startswith("invalid_toss_preview:")
    assert submitted is False
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].state == "pending_approval"


@pytest.mark.asyncio
async def test_toss_multi_rung_ids_and_correlations_stay_distinct(
    db_session, monkeypatch
):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(0, "buy", Decimal("1"), Decimal("50000"), None),
            RungInput(1, "buy", Decimal("2"), Decimal("49000"), None),
        ],
    )
    await db_session.commit()
    submit_contexts = []

    async def fake_preview(**kwargs):
        context = _bound_toss_context()
        return {
            "success": True,
            "approval_hash": f"token-{context.rung}",
            "payload_preview": {
                "price": str(kwargs["price"]),
                "quantity": str(kwargs["quantity"]),
                "clientOrderId": context.client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        context = _bound_toss_context()
        submit_contexts.append(context)
        return {
            "success": True,
            "broker_status": "accepted",
            "order_id": f"order-{context.rung}",
            "client_order_id": context.client_order_id,
            "correlation_id": context.correlation_id,
            "approval_hash_digest": f"digest-{context.rung}",
        }

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        correlation_mint=lambda **kwargs: f"proposal-corr-{kwargs['rung'].rung_index}",
    )

    assert [outcome.result for outcome in outcomes] == [
        "submitted_resting",
        "submitted_resting",
    ]
    assert submit_contexts[0].client_order_id != submit_contexts[1].client_order_id
    assert [context.correlation_id for context in submit_contexts] == [
        "proposal-corr-0",
        "proposal-corr-1",
    ]
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert [rung.correlation_id for rung in rungs] == [
        "proposal-corr-0",
        "proposal-corr-1",
    ]


@pytest.mark.asyncio
async def test_toss_us_preserves_fractional_numeric_precision(db_session, monkeypatch):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(
                0,
                "buy",
                Decimal("0.125000"),
                Decimal("189.123400"),
                None,
            )
        ],
    )
    await db_session.commit()
    calls: list[dict] = []

    async def fake_preview(**kwargs):
        calls.append(kwargs)
        client_order_id = _bound_toss_context().client_order_id
        return {
            "success": True,
            "approval_hash": "us-token",
            "payload_preview": {
                "price": "189.1234",
                "quantity": "0.125",
                "clientOrderId": client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        calls.append(kwargs)
        client_order_id = _bound_toss_context().client_order_id
        return {
            "success": True,
            "broker_status": "accepted",
            "order_id": "us-order",
            "client_order_id": client_order_id,
            "correlation_id": "toss-correlation",
            "approval_hash_digest": "us-ledger-digest",
        }

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
    )

    assert outcomes[0].result == "submitted_resting"
    assert calls[0]["market"] == "us"
    assert calls[0]["quantity"] == "0.125"
    assert calls[0]["price"] == "189.1234"
    assert not any(isinstance(value, float) for value in calls[0].values())
    assert calls[1]["quantity"] == "0.125"
    assert calls[1]["price"] == "189.1234"
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].approval_hash_digest == "us-ledger-digest"


@pytest.mark.asyncio
async def test_toss_tick_normalized_preview_needs_reconfirm_without_submit(
    db_session, monkeypatch
):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("10"), Decimal("222601"), None)],
    )
    await db_session.commit()
    submitted = False

    async def fake_preview(**kwargs):
        client_order_id = _bound_toss_context().client_order_id
        return {
            "success": True,
            "approval_hash": "normalized-token",
            "payload_preview": {
                "price": "222600",
                "quantity": "10",
                "clientOrderId": client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        nonlocal submitted
        submitted = True
        raise AssertionError("normalized preview must require reconfirmation")

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
    )

    assert outcomes[0].result == "needs_reconfirm"
    assert outcomes[0].detail["before"]["limit_price"] == "222601"
    assert outcomes[0].detail["after"]["limit_price"] == "222600"
    assert submitted is False


@pytest.mark.asyncio
async def test_toss_explicit_rejection_records_rejected(db_session, monkeypatch):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("50000"), None)],
    )
    await db_session.commit()

    async def fake_preview(**kwargs):
        client_order_id = _bound_toss_context().client_order_id
        return {
            "success": True,
            "approval_hash": "reject-token",
            "payload_preview": {
                "price": "50000",
                "quantity": "1",
                "clientOrderId": client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        return {
            "success": True,
            "broker_status": "rejected",
            "response_message": "insufficient balance",
        }

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
    )

    assert outcomes[0].result == "error"
    assert outcomes[0].detail["error"] == "insufficient balance"
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].state == "rejected"
    assert rungs[0].void_reason == "insufficient balance"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submit_result", "expected_order_id"),
    [
        (
            {
                "success": False,
                "mutation_sent": True,
                "error": "broker timeout; order state unknown",
            },
            None,
        ),
        (
            {
                "success": False,
                "mutation_sent": True,
                "error": "ledger write failed after broker accepted",
                "order_id": "preserved-broker-order",
                "client_order_id": "ambiguous-client",
            },
            "preserved-broker-order",
        ),
    ],
    ids=["timeout_unknown", "ledger_failure_preserves_order_id"],
)
async def test_toss_post_send_failure_records_unverified(
    db_session, monkeypatch, submit_result, expected_order_id
):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("50000"), None)],
    )
    await db_session.commit()

    async def fake_preview(**kwargs):
        client_order_id = _bound_toss_context().client_order_id
        return {
            "success": True,
            "approval_hash": "ambiguous-token",
            "payload_preview": {
                "price": "50000",
                "quantity": "1",
                "clientOrderId": client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        return submit_result

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
    )

    assert outcomes[0].result == "unverified"
    assert outcomes[0].detail["submit"]["error"] == submit_result["error"]
    assert outcomes[0].detail["submit"].get("order_id") == expected_order_id
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"
    assert rungs[0].state != "pending_approval"
    assert rungs[0].void_reason.startswith("ambiguous_submit_response:")


@pytest.mark.asyncio
async def test_toss_market_order_accepted_is_acked_not_filled(db_session, monkeypatch):
    svc = OrderProposalsService(db_session)
    group = await svc.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="buy",
        order_type="market",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), None, None)],
    )
    await db_session.commit()

    async def fake_preview(**kwargs):
        client_order_id = _bound_toss_context().client_order_id
        return {
            "success": True,
            "approval_hash": "market-token",
            "payload_preview": {
                "price": None,
                "quantity": "1",
                "clientOrderId": client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        client_order_id = _bound_toss_context().client_order_id
        return {
            "success": True,
            "broker_status": "accepted",
            "order_id": "market-order",
            "client_order_id": client_order_id,
            "approval_hash_digest": "market-digest",
        }

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
    )

    assert outcomes[0].result == "submitted_acked"
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].state == "acked"
    assert rungs[0].filled_qty is None
