from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.revalidation import (
    _adapt_live_submit_response,
    revalidate_and_submit,
)
from app.services.order_proposals.service import RungInput


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
