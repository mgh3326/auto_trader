import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.broker_gateway import SubmitEvidence
from app.services.order_proposals.revalidation import (
    _adapt_live_submit_response,
    preview_loss_cut_confirmation,
    revalidate_and_submit,
)
from app.services.order_proposals.service import RungInput
from app.services.order_proposals.target_order import TargetOrderSnapshot


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
async def test_loss_cut_confirmation_preview_is_read_only_and_builds_evidence(
    db_session, monkeypatch
):
    retro = SimpleNamespace(
        id=42,
        symbol="005930",
        trigger_type="stop_loss",
        created_at=datetime.now(UTC),
        lesson="손절 기준을 늦추지 않는다",
    )

    async def fake_lookup(session, retrospective_id):
        return retro

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
        rungs=[RungInput(0, "sell", Decimal("2"), Decimal("99"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
    )
    calls = []

    async def fake_preview(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "price": "99",
            "quantity": "2",
            "current_price": "100",
            "avg_buy_price": "200",
            "loss_cut_slip_band": "98",
        }

    async def fake_retro_lookup(retrospective_id):
        assert retrospective_id == 42
        return retro

    evidence = await preview_loss_cut_confirmation(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=fake_preview,
        retrospective_lookup_fn=fake_retro_lookup,
    )

    assert [call["dry_run"] for call in calls] == [True]
    assert evidence == {
        "rungs": [
            {
                "rung_index": 0,
                "current_price": "100",
                "avg_buy_price": "200",
                "loss_pct": "-50.00",
                "loss_cut_slip_band": "98",
            }
        ],
        "retrospective_id": 42,
        "lesson_excerpt": "손절 기준을 늦추지 않는다",
    }
    _group, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "pending_approval"


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


def _target_snapshot(
    *,
    broker_order_id: str = "old-1",
    symbol: str = "KRW-AVAX",
    side: str = "sell",
    order_type: str = "limit",
    limit_price: str = "42000",
    remaining_quantity: str = "3.5",
    status: str = "open",
) -> TargetOrderSnapshot:
    return TargetOrderSnapshot(
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        limit_price=limit_price,
        remaining_quantity=remaining_quantity,
        status=status,
        observed_at="2026-07-11T08:23:00+00:00",
    )


async def _create_target_proposal(db_session, *, action: str, target_id: str = "old-1"):
    service = OrderProposalsService(db_session)
    approved = _target_snapshot(broker_order_id=target_id)
    group = await service.create_proposal(
        symbol="KRW-AVAX",
        market="crypto",
        account_mode="upbit",
        side="sell",
        order_type="limit",
        proposer="p",
        action=action,
        target_broker_order_id=target_id,
        target_order_snapshot=approved.to_payload(),
        rungs=[
            RungInput(
                0,
                "sell",
                Decimal("3.5"),
                Decimal("43000") if action == "replace" else Decimal("42000"),
                None,
            )
        ],
    )
    await db_session.commit()
    return service, group


async def _matching_preview(**kwargs):
    return {
        "success": True,
        "approval_hash": "fresh",
        "price": "43000",
        "quantity": "3.5",
    }


async def _forbidden_submit(**kwargs):
    if kwargs["dry_run"]:
        return await _matching_preview(**kwargs)
    raise AssertionError("replacement submit requires confirmed cancellation")


@pytest.mark.asyncio
async def test_replace_confirms_cancel_before_new_submit(db_session):
    service, group = await _create_target_proposal(db_session, action="replace")
    events = []
    snapshots = iter(
        [_target_snapshot(status="open"), _target_snapshot(status="cancelled")]
    )

    async def fetch_target_fn(**kwargs):
        snapshot = next(snapshots)
        events.append(f"fetch:{snapshot.status}")
        return snapshot

    async def cancel_target_fn(**kwargs):
        events.append("cancel")
        return {"success": True}

    async def place_order_fn(**kwargs):
        if kwargs["dry_run"]:
            events.append("preview")
            return await _matching_preview(**kwargs)
        events.append("submit")
        return {"success": True, "status": "resting", "broker_order_id": "new-1"}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert events == ["fetch:open", "preview", "cancel", "fetch:cancelled", "submit"]
    assert outcomes[0].result == "submitted_resting"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("broker_order_id", "old-2"),
        ("symbol", "KRW-SOL"),
        ("side", "buy"),
        ("order_type", "market"),
        ("limit_price", "42001"),
        ("remaining_quantity", "3.4"),
    ],
)
async def test_replace_target_drift_is_rejected_without_cancel(
    db_session, field, value
):
    service, group = await _create_target_proposal(db_session, action="replace")
    calls = []
    fresh = _target_snapshot(**{field: value})

    async def fetch_target_fn(**kwargs):
        calls.append("fetch")
        return fresh

    async def cancel_target_fn(**kwargs):
        calls.append("cancel")
        return {"success": True}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=_forbidden_submit,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "error"
    assert calls == ["fetch"]
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "rejected"
    assert rungs[0].void_reason == f"target_snapshot_mismatch:{field}"


def test_toss_decimal_args_are_exact_and_canonical():
    from app.services.order_proposals import revalidation as mod

    assert mod._toss_decimal_arg(None) is None
    assert mod._toss_decimal_arg(Decimal("10.000")) == 10
    assert mod._toss_decimal_arg(Decimal("0.125000")) == "0.125"
    assert mod._toss_decimal_arg(Decimal("0.000000000001")) == "0.000000000001"


def test_toss_proposal_client_ids_are_stable_and_rung_scoped():
    from app.services.order_proposals import revalidation as mod

    # Toss OpenAPI OrderCreateRequest.clientOrderId (openapi.json).
    toss_client_order_id_max_length = 36
    proposal_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    same = mod._toss_proposal_client_order_id(proposal_id, 0)
    assert same == mod._toss_proposal_client_order_id(proposal_id, 0)
    assert same != mod._toss_proposal_client_order_id(proposal_id, 1)
    assert same != mod._toss_proposal_client_order_id(uuid.uuid4(), 0)
    assert same.startswith("tosprop-")
    assert len(same) <= toss_client_order_id_max_length
    assert re.fullmatch(r"[a-zA-Z0-9\-_]+", same)


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "submit"])
async def test_toss_adapter_forwards_loss_cut_binding(monkeypatch, dry_run):
    import app.mcp_server.tooling.orders_toss_variants as toss
    from app.services.order_proposals import revalidation as mod

    calls: list[dict] = []

    async def fake_preview(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "approval_hash": "preview-token",
            "payload_preview": {
                "clientOrderId": "tosprop-loss-cut",
                "price": "50000",
                "quantity": "1",
            },
        }

    async def fake_submit(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "order_id": "toss-loss-cut-1",
            "client_order_id": "tosprop-loss-cut",
            "approval_hash_digest": "loss-cut-digest",
        }

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)

    await mod._default_place_order_fn(
        dry_run=dry_run,
        account_mode="toss_live",
        symbol="005930",
        side="sell",
        market="equity_kr",
        order_type="limit",
        quantity=Decimal("1"),
        price=Decimal("50000"),
        proposal_client_order_id="tosprop-loss-cut",
        approval_hash="preview-token",
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-858",
    )

    assert calls[0]["exit_intent"] == "loss_cut"
    assert calls[0]["exit_reason"] == "stop_loss"
    assert calls[0]["retrospective_id"] == 42
    assert calls[0]["approval_issue_id"] == "ROB-858"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_client_order_id",
    ["x" * 37, "invalid@client-order-id"],
)
async def test_toss_submit_rejects_invalid_client_order_id_before_broker_call(
    monkeypatch, invalid_client_order_id
):
    import app.mcp_server.tooling.orders_toss_variants as toss
    from app.services.order_proposals import revalidation as mod

    async def forbidden_broker_call(**kwargs):
        pytest.fail(f"broker POST must not be called: {kwargs}")

    monkeypatch.setattr(toss, "toss_place_order", forbidden_broker_call)

    result = await mod._default_place_order_fn(
        dry_run=False,
        account_mode="toss_live",
        symbol="000660",
        side="buy",
        market="equity_kr",
        order_type="limit",
        quantity=Decimal("1"),
        price=Decimal("222600"),
        proposal_client_order_id=invalid_client_order_id,
        approval_hash="preview-token",
    )

    assert result["success"] is False
    assert result["mutation_sent"] is False
    assert result["error_code"] == "invalid_toss_client_order_id"


def test_upbit_proposal_client_ids_are_stable_and_rung_scoped():
    from app.services.order_proposals import revalidation as mod

    proposal_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    first = mod._proposal_client_order_id(proposal_id, 0)
    assert first == mod._proposal_client_order_id(proposal_id, 0)
    assert first != mod._proposal_client_order_id(proposal_id, 1)
    assert first != mod._proposal_client_order_id(uuid.uuid4(), 0)
    assert first.startswith("oprop-")
    assert len(first) <= 40


@pytest.mark.asyncio
async def test_upbit_revalidation_binds_same_proposal_client_id_to_preview_and_submit(
    db_session,
):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="KRW-BTC",
        market="crypto",
        account_mode="upbit",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("0.01"), Decimal("70000000"), None)],
    )
    await db_session.commit()
    calls: list[dict] = []

    async def accepted(**kwargs):
        calls.append(kwargs)
        if kwargs["dry_run"]:
            return {
                "success": True,
                "approval_hash": "upbit-token",
                "price": "70000000",
                "quantity": "0.01",
            }
        return {"success": True, "status": "resting", "broker_order_id": "upbit-1"}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=accepted,
    )

    from app.services.order_proposals import revalidation as mod

    expected = mod._proposal_client_order_id(group.proposal_id, 0)
    assert outcomes[0].result == "submitted_resting"
    assert [call["proposal_client_order_id"] for call in calls] == [
        expected,
        expected,
    ]


async def _create_upbit_submit_proposal(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="KRW-BTC",
        market="crypto",
        account_mode="upbit",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("0.01"), Decimal("70000000"), None)],
    )
    await db_session.commit()
    return service, group


def _upbit_preview() -> dict[str, str | bool]:
    return {
        "success": True,
        "approval_hash": "upbit-approval",
        "price": "70000000",
        "quantity": "0.01",
    }


@pytest.mark.asyncio
async def test_upbit_submit_failure_found_evidence_converges_resting(
    db_session, monkeypatch
):
    from app.services.order_proposals import revalidation as mod

    service, group = await _create_upbit_submit_proposal(db_session)
    monkeypatch.setattr(mod, "_proposal_client_order_id", lambda *_: "oprop-expected")
    live_calls: list[str] = []
    evidence_calls: list[dict] = []

    async def place_order_fn(**kwargs):
        if kwargs["dry_run"]:
            return _upbit_preview()
        live_calls.append(kwargs["proposal_client_order_id"])
        return {"success": False}

    async def evidence_fn(**kwargs):
        evidence_calls.append(kwargs)
        return SubmitEvidence("found", "35bee07f-full", "wait")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        fetch_submit_evidence_fn=evidence_fn,
    )

    assert live_calls == ["oprop-expected"]
    assert len(evidence_calls) == 1
    assert outcomes[0].result == "submitted_resting"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"
    assert rungs[0].broker_order_id == "35bee07f-full"
    assert rungs[0].idempotency_key == "oprop-expected"


@pytest.mark.asyncio
async def test_upbit_true_rejection_absent_evidence_is_rejected(
    db_session, monkeypatch
):
    from app.services.order_proposals import revalidation as mod

    service, group = await _create_upbit_submit_proposal(db_session)
    monkeypatch.setattr(mod, "_proposal_client_order_id", lambda *_: "oprop-expected")
    live_calls: list[str] = []
    evidence_calls = 0

    async def place_order_fn(**kwargs):
        if kwargs["dry_run"]:
            return _upbit_preview()
        live_calls.append(kwargs["proposal_client_order_id"])
        return {"success": False, "error": "insufficient balance"}

    async def evidence_fn(**kwargs):
        nonlocal evidence_calls
        evidence_calls += 1
        return SubmitEvidence("absent")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        fetch_submit_evidence_fn=evidence_fn,
    )

    assert outcomes[0].result == "error"
    assert live_calls == ["oprop-expected"]
    assert evidence_calls == 1
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "rejected"
    assert rungs[0].void_reason == "insufficient balance"


@pytest.mark.asyncio
async def test_upbit_submit_evidence_unknown_is_unverified(db_session, monkeypatch):
    from app.services.order_proposals import revalidation as mod

    service, group = await _create_upbit_submit_proposal(db_session)
    monkeypatch.setattr(mod, "_proposal_client_order_id", lambda *_: "oprop-expected")
    live_calls: list[str] = []
    evidence_calls = 0

    async def place_order_fn(**kwargs):
        if kwargs["dry_run"]:
            return _upbit_preview()
        live_calls.append(kwargs["proposal_client_order_id"])
        return {"success": False, "error": "submit not confirmed"}

    async def evidence_fn(**kwargs):
        nonlocal evidence_calls
        evidence_calls += 1
        return SubmitEvidence("unknown", reason="timeout")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        correlation_mint=lambda **_: "corr-expected",
        fetch_submit_evidence_fn=evidence_fn,
    )

    assert outcomes[0].result == "unverified"
    assert live_calls == ["oprop-expected"]
    assert evidence_calls == 1
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"
    assert rungs[0].idempotency_key == "oprop-expected"
    assert rungs[0].correlation_id == "corr-expected"
    assert rungs[0].void_reason == "submit_evidence_unknown:timeout"


@pytest.mark.asyncio
async def test_upbit_submit_exception_found_evidence_converges_resting(
    db_session, monkeypatch
):
    from app.services.order_proposals import revalidation as mod

    service, group = await _create_upbit_submit_proposal(db_session)
    monkeypatch.setattr(mod, "_proposal_client_order_id", lambda *_: "oprop-expected")
    live_calls: list[str] = []
    evidence_calls = 0

    async def place_order_fn(**kwargs):
        if kwargs["dry_run"]:
            return _upbit_preview()
        live_calls.append(kwargs["proposal_client_order_id"])
        raise httpx.ReadTimeout("submit timeout")

    async def evidence_fn(**kwargs):
        nonlocal evidence_calls
        evidence_calls += 1
        return SubmitEvidence("found", "35bee07f-full", "watch")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        fetch_submit_evidence_fn=evidence_fn,
    )

    assert outcomes[0].result == "submitted_resting"
    assert live_calls == ["oprop-expected"]
    assert evidence_calls == 1
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"
    assert rungs[0].broker_order_id == "35bee07f-full"


@pytest.mark.asyncio
async def test_default_place_order_forwards_proposal_client_id_to_preview_and_submit(
    monkeypatch,
):
    import app.mcp_server.tooling.order_execution as order_execution
    from app.services.order_proposals import revalidation as mod

    calls: list[dict] = []

    async def fake_impl(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(order_execution, "_place_order_impl", fake_impl)
    expected = mod._proposal_client_order_id(
        uuid.UUID("12345678-1234-5678-1234-567812345678"), 0
    )
    for dry_run in (True, False):
        await mod._default_place_order_fn(
            dry_run=dry_run,
            account_mode="upbit",
            symbol="KRW-BTC",
            side="buy",
            market="crypto",
            order_type="limit",
            quantity=Decimal("0.01"),
            price=Decimal("70000000"),
            proposal_client_order_id=expected,
        )

    assert [call["client_order_id"] for call in calls] == [expected, expected]


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
async def test_toss_insufficient_buying_power_prevents_submit(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("1070300"), None)],
    )
    await db_session.commit()
    submit_calls = 0

    async def place_order(**kwargs):
        nonlocal submit_calls
        if kwargs["dry_run"]:
            client_order_id = kwargs["proposal_client_order_id"]
            return {
                "success": True,
                "approval_hash": "preview-token",
                "price": "1070300",
                "quantity": "1",
                "estimated_value": "1070300",
                "fee": "0",
                "payload_preview": {
                    "clientOrderId": client_order_id,
                    "price": "1070300",
                    "quantity": "1",
                },
            }
        submit_calls += 1
        pytest.fail("broker POST must not run with known insufficient buying power")

    async def buying_power_claimer(**kwargs):
        assert kwargs == {
            "account_mode": "toss_live",
            "broker_account_id": None,
            "currency": "KRW",
            "amount": Decimal("1070300"),
        }
        return Decimal("400000")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=buying_power_claimer,
    )

    assert outcomes[0].result == "needs_reconfirm"
    assert outcomes[0].detail == {
        "reason": "insufficient_buying_power",
        "currency": "KRW",
        "available": "400000",
        "required": "1070300",
        "shortfall": "670300",
    }
    assert submit_calls == 0
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "needs_reconfirm"


async def _create_toss_gate_proposal(db_session, *, side="buy", rungs=None):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="toss_live",
        side=side,
        order_type="limit",
        proposer="p",
        rungs=rungs or [RungInput(0, side, Decimal("1"), Decimal("100000"), None)],
    )
    await db_session.commit()
    return service, group


def _toss_gate_preview(kwargs, *, quantity="1", price="100000"):
    return {
        "success": True,
        "approval_hash": "preview-token",
        "price": price,
        "quantity": quantity,
        "estimated_value": str(Decimal(quantity) * Decimal(price)),
        "fee": "0",
        "payload_preview": {
            "clientOrderId": kwargs["proposal_client_order_id"],
            "price": price,
            "quantity": quantity,
        },
    }


@pytest.mark.asyncio
async def test_toss_sufficient_buying_power_preserves_submit_path(db_session):
    service, group = await _create_toss_gate_proposal(db_session)
    submit_calls = 0

    async def place_order(**kwargs):
        nonlocal submit_calls
        if kwargs["dry_run"]:
            return _toss_gate_preview(kwargs)
        submit_calls += 1
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "toss-enough-1",
        }

    async def reader(**kwargs):
        return Decimal("100001")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=reader,
    )

    assert [outcome.result for outcome in outcomes] == ["submitted_resting"]
    assert submit_calls == 1
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"


@pytest.mark.asyncio
async def test_toss_sell_rung_skips_buying_power_gate(db_session):
    service, group = await _create_toss_gate_proposal(db_session, side="sell")
    submit_calls = 0

    async def place_order(**kwargs):
        nonlocal submit_calls
        if kwargs["dry_run"]:
            return _toss_gate_preview(kwargs)
        submit_calls += 1
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "toss-sell-1",
        }

    async def forbidden_reader(**kwargs):
        pytest.fail(f"sell rung must not read buying power: {kwargs}")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=forbidden_reader,
    )

    assert [outcome.result for outcome in outcomes] == ["submitted_resting"]
    assert submit_calls == 1


@pytest.mark.asyncio
async def test_toss_buying_power_failure_fails_open_to_submit(db_session):
    service, group = await _create_toss_gate_proposal(db_session)
    submit_calls = 0

    async def place_order(**kwargs):
        nonlocal submit_calls
        if kwargs["dry_run"]:
            return _toss_gate_preview(kwargs)
        submit_calls += 1
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "toss-fail-open-1",
        }

    async def failed_reader(**kwargs):
        raise RuntimeError("buying-power endpoint unavailable")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=failed_reader,
    )

    assert [outcome.result for outcome in outcomes] == ["submitted_resting"]
    assert submit_calls == 1


@pytest.mark.asyncio
async def test_toss_deposit_then_same_proposal_reapproval_succeeds(db_session):
    service, group = await _create_toss_gate_proposal(db_session)
    buying_power = iter((Decimal("50000"), Decimal("150000")))
    submit_calls = 0

    async def place_order(**kwargs):
        nonlocal submit_calls
        if kwargs["dry_run"]:
            return _toss_gate_preview(kwargs)
        submit_calls += 1
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": "toss-after-deposit-1",
        }

    async def reader(**kwargs):
        return next(buying_power)

    first = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=reader,
    )
    await service.transition_rung(group.proposal_id, 0, new_state="pending_approval")
    second = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=reader,
    )

    assert [outcome.result for outcome in first] == ["needs_reconfirm"]
    assert [outcome.result for outcome in second] == ["submitted_resting"]
    assert submit_calls == 1
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"


@pytest.mark.asyncio
async def test_toss_successful_rung_reserves_cached_power_for_next_rung(db_session):
    service, group = await _create_toss_gate_proposal(
        db_session,
        rungs=[
            RungInput(0, "buy", Decimal("6"), Decimal("10000"), None),
            RungInput(1, "buy", Decimal("6"), Decimal("10000"), None),
        ],
    )
    available = Decimal("100000")
    submit_calls = 0
    reservations: list[Decimal] = []

    async def place_order(**kwargs):
        nonlocal submit_calls
        if kwargs["dry_run"]:
            return _toss_gate_preview(kwargs, quantity="6", price="10000")
        submit_calls += 1
        return {
            "success": True,
            "status": "resting",
            "broker_order_id": f"toss-reserved-{kwargs['rung']}",
        }

    async def claimer(**kwargs):
        nonlocal available
        reservations.append(kwargs["amount"])
        before = available
        if before >= kwargs["amount"]:
            available -= kwargs["amount"]
        return before

    async def forbidden_releaser(**kwargs):
        pytest.fail(f"accepted claims must not be released: {kwargs}")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=claimer,
        buying_power_releaser=forbidden_releaser,
    )

    assert [outcome.result for outcome in outcomes] == [
        "submitted_resting",
        "needs_reconfirm",
    ]
    assert submit_calls == 1
    assert reservations == [Decimal("60000"), Decimal("60000")]
    assert outcomes[1].detail["available"] == "40000"
    assert outcomes[1].detail["shortfall"] == "20000"


@pytest.mark.asyncio
async def test_toss_explicit_rejection_releases_provisional_power(db_session):
    service, group = await _create_toss_gate_proposal(db_session)
    released: list[Decimal] = []

    async def place_order(**kwargs):
        if kwargs["dry_run"]:
            return _toss_gate_preview(kwargs)
        return {"success": False, "error": "broker_rejected"}

    async def claimer(**kwargs):
        assert kwargs["amount"] == Decimal("100000")
        return Decimal("150000")

    async def releaser(**kwargs):
        released.append(kwargs["amount"])

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=claimer,
        buying_power_releaser=releaser,
    )

    assert outcomes[0].result == "error"
    assert released == [Decimal("100000")]


@pytest.mark.asyncio
async def test_toss_ambiguous_submit_keeps_provisional_power(db_session):
    service, group = await _create_toss_gate_proposal(db_session)

    async def place_order(**kwargs):
        if kwargs["dry_run"]:
            return _toss_gate_preview(kwargs)
        raise TimeoutError("submit response lost")

    async def claimer(**kwargs):
        return Decimal("150000")

    async def forbidden_releaser(**kwargs):
        pytest.fail(f"ambiguous submission must keep its claim: {kwargs}")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order,
        buying_power_claimer=claimer,
        buying_power_releaser=forbidden_releaser,
    )

    assert outcomes[0].result == "unverified"


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
    "preview",
    [
        {"success": False, "error": "loss_sell_blocked"},
        {
            "success": True,
            "approval_hash": "fresh",
            "price": "43001",
            "quantity": "3.5",
        },
    ],
    ids=["guard_blocked", "normalization_diff"],
)
async def test_replace_preview_failure_does_not_cancel(db_session, preview):
    service, group = await _create_target_proposal(db_session, action="replace")
    calls = []

    async def fetch_target_fn(**kwargs):
        calls.append("fetch")
        return _target_snapshot()

    async def place_order_fn(**kwargs):
        calls.append("preview" if kwargs["dry_run"] else "submit")
        return preview

    async def cancel_target_fn(**kwargs):
        calls.append("cancel")
        return {"success": True}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result in {"guard_blocked", "needs_reconfirm"}
    assert calls == ["fetch", "preview"]


@pytest.mark.asyncio
async def test_replace_cancel_rejection_forbids_submit(db_session):
    service, group = await _create_target_proposal(db_session, action="replace")

    async def fetch_target_fn(**kwargs):
        return _target_snapshot()

    async def cancel_target_fn(**kwargs):
        return {"success": False, "error": "broker_rejected"}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=_forbidden_submit,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "error"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "void_reason_prefix"),
    [
        ("cancel_exception", "cancel_exception:"),
        ("confirmation_exception", "cancel_confirmation_error:"),
        ("open_confirmation", "cancel_unconfirmed:open"),
        ("missing_evidence", "cancel_confirmation_missing_evidence"),
    ],
)
async def test_replace_unconfirmed_cancellation_forbids_submit(
    db_session, failure, void_reason_prefix
):
    service, group = await _create_target_proposal(db_session, action="replace")
    fetches = 0

    async def fetch_target_fn(**kwargs):
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return _target_snapshot()
        if failure == "confirmation_exception":
            raise TimeoutError("history timeout")
        if failure == "missing_evidence":
            return None
        return _target_snapshot(status="open")

    async def cancel_target_fn(**kwargs):
        if failure == "cancel_exception":
            raise TimeoutError("cancel timeout")
        return {"success": True}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=_forbidden_submit,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "unverified"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"
    assert rungs[0].void_reason.startswith(void_reason_prefix)


@pytest.mark.asyncio
async def test_cancel_confirms_target_without_preview_or_submit(db_session):
    service, group = await _create_target_proposal(db_session, action="cancel")
    events = []
    snapshots = iter([_target_snapshot(), _target_snapshot(status="cancelled")])

    async def fetch_target_fn(**kwargs):
        snapshot = next(snapshots)
        events.append(f"fetch:{snapshot.status}")
        return snapshot

    async def cancel_target_fn(**kwargs):
        events.append("cancel")
        return {"success": True}

    async def forbidden_place_order(**kwargs):
        raise AssertionError("cancel action must not preview or submit")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=forbidden_place_order,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert events == ["fetch:open", "cancel", "fetch:cancelled"]
    assert outcomes[0].result == "cancelled"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "cancelled"


@pytest.mark.asyncio
async def test_replace_manual_target_uses_fresh_broker_evidence_only(db_session):
    service, group = await _create_target_proposal(
        db_session, action="replace", target_id="manual-upbit-1"
    )
    snapshots = iter(
        [
            _target_snapshot(broker_order_id="manual-upbit-1"),
            _target_snapshot(broker_order_id="manual-upbit-1", status="cancelled"),
        ]
    )

    async def fetch_target_fn(**kwargs):
        assert kwargs["order_id"] == "manual-upbit-1"
        return next(snapshots)

    async def cancel_target_fn(**kwargs):
        return {"success": True}

    async def place_order_fn(**kwargs):
        if kwargs["dry_run"]:
            return await _matching_preview(**kwargs)
        return {"success": True, "status": "resting", "broker_order_id": "new-manual"}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "submitted_resting"


@pytest.mark.asyncio
async def test_replace_unconfirmed_returns_unverified_no_submit(db_session):
    service, group = await _create_target_proposal(db_session, action="replace")
    snapshots = iter([_target_snapshot(), _target_snapshot(status="open")])

    async def fetch_target_fn(**kwargs):
        return next(snapshots)

    async def cancel_target_fn(**kwargs):
        return {"success": True}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=_forbidden_submit,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "unverified"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"


@pytest.mark.asyncio
async def test_replace_confirmation_exception_returns_unverified_no_submit(db_session):
    service, group = await _create_target_proposal(db_session, action="replace")

    class StatusAccessError:
        @property
        def status(self):
            raise TypeError("invalid confirmation status")

    fetches = 0

    async def fetch_target_fn(**kwargs):
        nonlocal fetches
        fetches += 1
        return _target_snapshot() if fetches == 1 else StatusAccessError()

    async def cancel_target_fn(**kwargs):
        return {"success": True}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=_forbidden_submit,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "unverified"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("submit_result", ["exception", "ambiguous"])
async def test_replace_submit_ambiguity_persists_reconcile_lineage(
    db_session, submit_result
):
    from app.services.order_proposals import revalidation as mod

    service, group = await _create_target_proposal(db_session, action="replace")
    snapshots = iter([_target_snapshot(), _target_snapshot(status="cancelled")])

    async def fetch_target_fn(**kwargs):
        return next(snapshots)

    async def cancel_target_fn(**kwargs):
        return {"success": True}

    async def place_order_fn(**kwargs):
        if kwargs["dry_run"]:
            return {
                **(await _matching_preview(**kwargs)),
                "idempotency_key": "idem-replace-1",
            }
        if submit_result == "exception":
            raise TimeoutError("submit outcome unknown")
        return {"success": True, "status": "unknown"}

    async def evidence_fn(**kwargs):
        return SubmitEvidence("unknown", reason="lookup unavailable")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
        correlation_mint=lambda **kwargs: "corr-replace-1",
        fetch_submit_evidence_fn=evidence_fn,
    )

    assert outcomes[0].result == "unverified"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].correlation_id == "corr-replace-1"
    assert rungs[0].idempotency_key == mod._proposal_client_order_id(
        group.proposal_id, 0
    )


@pytest.mark.asyncio
async def test_replace_submit_failure_found_evidence_converges_resting(
    db_session, monkeypatch
):
    from app.services.order_proposals import revalidation as mod

    service, group = await _create_target_proposal(db_session, action="replace")
    monkeypatch.setattr(mod, "_proposal_client_order_id", lambda *_: "oprop-replace")
    snapshots = iter([_target_snapshot(), _target_snapshot(status="cancelled")])
    place_calls: list[dict] = []
    evidence_calls: list[dict] = []

    async def fetch_target_fn(**kwargs):
        return next(snapshots)

    async def cancel_target_fn(**kwargs):
        return {"success": True}

    async def place_order_fn(**kwargs):
        place_calls.append(kwargs)
        if kwargs["dry_run"]:
            return await _matching_preview(**kwargs)
        return {"success": False, "error": "duplicate identifier"}

    async def evidence_fn(**kwargs):
        evidence_calls.append(kwargs)
        return SubmitEvidence("found", "replacement-order", "wait")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=place_order_fn,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
        fetch_submit_evidence_fn=evidence_fn,
    )

    assert outcomes[0].result == "submitted_resting"
    assert [call["proposal_client_order_id"] for call in place_calls] == [
        "oprop-replace",
        "oprop-replace",
    ]
    assert evidence_calls == [
        {
            "identifier": "oprop-replace",
            "account_mode": "upbit",
            "market": "crypto",
        }
    ]
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "resting"
    assert rungs[0].broker_order_id == "replacement-order"
    assert rungs[0].idempotency_key == "oprop-replace"


@pytest.mark.asyncio
async def test_replace_initial_fetch_returns_pending_approval_on_transient_error(
    db_session,
):
    service, group = await _create_target_proposal(db_session, action="replace")

    async def fetch_target_fn(**kwargs):
        raise TimeoutError("target fetch unavailable")

    async def cancel_target_fn(**kwargs):
        raise AssertionError("cancel requires fresh target evidence")

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=_forbidden_submit,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "error"
    assert outcomes[0].detail["error"] == "target_fetch_error:target fetch unavailable"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "pending_approval"


@pytest.mark.asyncio
async def test_cancel_unconfirmed_returns_unverified(db_session):
    service, group = await _create_target_proposal(db_session, action="cancel")
    snapshots = iter([_target_snapshot(), _target_snapshot(status="open")])

    async def fetch_target_fn(**kwargs):
        return next(snapshots)

    async def cancel_target_fn(**kwargs):
        return {"success": True}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=_forbidden_submit,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "unverified"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"


@pytest.mark.asyncio
async def test_cancel_confirmation_exception_returns_unverified(db_session):
    service, group = await _create_target_proposal(db_session, action="cancel")

    class StatusAccessError:
        @property
        def status(self):
            raise TypeError("invalid confirmation status")

    fetches = 0

    async def fetch_target_fn(**kwargs):
        nonlocal fetches
        fetches += 1
        return _target_snapshot() if fetches == 1 else StatusAccessError()

    async def cancel_target_fn(**kwargs):
        return {"success": True}

    outcomes = await revalidate_and_submit(
        service=service,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
        place_order_fn=_forbidden_submit,
        fetch_target_fn=fetch_target_fn,
        cancel_target_fn=cancel_target_fn,
    )

    assert outcomes[0].result == "unverified"
    _, rungs = await service.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"


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
        "missing_success",
        "none_success",
        "string_success",
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
        elif defect == "mismatched_client_id":
            preview["payload_preview"]["clientOrderId"] = "external-client-id"
        elif defect == "missing_success":
            preview.pop("success")
        elif defect == "none_success":
            preview["success"] = None
        else:
            preview["success"] = "true"
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
async def test_toss_success_false_preview_keeps_guard_blocked_behavior(
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
    submitted = False

    async def fake_preview(**kwargs):
        return {
            "success": False,
            "error": "insufficient balance for Toss preview",
        }

    async def fake_submit(**kwargs):
        nonlocal submitted
        submitted = True
        raise AssertionError("guard-blocked preview must never submit")

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
    )

    assert outcomes[0].result == "guard_blocked"
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
async def test_toss_typed_400_rejection_converges_with_broker_diagnostics(
    db_session, monkeypatch
):
    from app.services.order_proposals.telegram_callback import _build_result_summary

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
            "success": False,
            "mutation_sent": True,
            "status_code": 400,
            "code": "invalid-client-order-id",
            "message": "clientOrderId must be at most 36 characters " + ("x" * 400),
            "error": "Toss API error status=400",
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
    assert "invalid-client-order-id" in outcomes[0].detail["error"]
    assert "clientOrderId must be at most 36 characters" in outcomes[0].detail["error"]
    assert len(outcomes[0].detail["error"]) <= 240
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].state == "rejected"
    assert "invalid-client-order-id" in rungs[0].void_reason
    assert "clientOrderId must be at most 36 characters" in rungs[0].void_reason
    assert "invalid-client-order-id" in _build_result_summary(outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submit_result", "expected_order_id", "reason_fragments"),
    [
        (
            {
                "success": False,
                "mutation_sent": True,
                "error": "broker timeout; order state unknown",
            },
            None,
            ("broker timeout",),
        ),
        (
            {
                "success": False,
                "mutation_sent": True,
                "status_code": 500,
                "code": "internal-error",
                "message": "temporary upstream failure",
                "error": "Toss API error status=500",
            },
            None,
            ("internal-error", "temporary upstream failure"),
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
            ("ledger write failed",),
        ),
    ],
    ids=["timeout_unknown", "server_500_unknown", "ledger_failure_preserves_order_id"],
)
async def test_toss_post_send_failure_records_unverified(
    db_session, monkeypatch, submit_result, expected_order_id, reason_fragments
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
    assert outcomes[0].detail["submit"]["error"]
    assert outcomes[0].detail["submit"].get("order_id") == expected_order_id
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"
    assert rungs[0].state != "pending_approval"
    assert rungs[0].void_reason.startswith("ambiguous_submit_response:")
    for fragment in reason_fragments:
        assert fragment in rungs[0].void_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submit_result",
    [
        {
            "success": True,
            "broker_status": "accepted",
            "client_order_id": "incomplete-client",
            "approval_hash_digest": "incomplete-digest",
        },
        {
            "success": True,
            "broker_status": "accepted",
            "order_id": "incomplete-order",
            "client_order_id": "incomplete-client",
        },
    ],
    ids=["missing_order_id", "missing_approval_hash_digest"],
)
async def test_toss_incomplete_accepted_submit_stays_unverified(
    db_session, monkeypatch, submit_result
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
            "approval_hash": "incomplete-token",
            "payload_preview": {
                "price": "50000",
                "quantity": "1",
                "clientOrderId": client_order_id,
            },
        }

    async def fake_submit(**kwargs):
        result = dict(submit_result)
        result["client_order_id"] = _bound_toss_context().client_order_id
        return result

    import app.mcp_server.tooling.orders_toss_variants as toss

    monkeypatch.setattr(toss, "toss_preview_order", fake_preview)
    monkeypatch.setattr(toss, "toss_place_order", fake_submit)
    outcomes = await revalidate_and_submit(
        service=svc,
        proposal_id=group.proposal_id,
        now=datetime.now(UTC),
    )

    assert outcomes[0].result == "unverified"
    assert outcomes[0].detail["submit"]["success"] is True
    assert outcomes[0].detail["submit"].get("order_id") == submit_result.get("order_id")
    assert outcomes[0].detail["submit"].get(
        "approval_hash_digest"
    ) == submit_result.get("approval_hash_digest")
    _, rungs = await svc.get_proposal(group.proposal_id)
    assert rungs[0].state == "unverified"
    assert rungs[0].broker_order_id is None


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
