from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

import scripts.rob837_reconcile_upbit_proposal as cli
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.order_proposals.service import OrderProposalsService, RungInput

PROPOSAL_ID = uuid.UUID("b81ffd0e-0000-4000-8000-000000000000")
BROKER_ORDER_ID = "35bee07f-full"


def _broker_order(**overrides: str) -> dict[str, str]:
    order = {
        "uuid": BROKER_ORDER_ID,
        "identifier": "oprop-fixed",
        "market": "KRW-BTC",
        "state": "wait",
        "side": "bid",
        "ord_type": "limit",
        "price": "88800000",
        "volume": "0.0004",
    }
    order.update(overrides)
    return order


async def _seed_rejected_proposal(db_session, monkeypatch):  # noqa: ANN001
    await db_session.execute(
        delete(OrderProposal).where(OrderProposal.proposal_id == PROPOSAL_ID)
    )
    await db_session.commit()
    monkeypatch.setattr(
        "app.services.order_proposals.service.uuid.uuid4", lambda: PROPOSAL_ID
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="KRW-BTC",
        market="crypto",
        account_mode="upbit",
        side="buy",
        order_type="limit",
        proposer="rob837-test",
        rungs=[RungInput(0, "buy", Decimal("0.0004"), Decimal("88800000"), None)],
    )
    _, [rung] = await service.get_proposal(group.proposal_id)
    rung.state = "rejected"
    rung.void_reason = "submit failed"
    group.lifecycle_state = "rejected"
    await db_session.commit()
    return group, rung


async def _reload(db_session, group_id: int, rung_id: int):  # noqa: ANN001
    db_session.expire_all()
    group = await db_session.get(OrderProposal, group_id)
    rung = await db_session.get(OrderProposalRung, rung_id)
    assert group is not None
    assert rung is not None
    return group, rung


@pytest.mark.asyncio
async def test_repair_dry_run_reports_resting_without_mutating(db_session, monkeypatch):
    group, rung = await _seed_rejected_proposal(db_session, monkeypatch)
    group_id, rung_id = group.id, rung.id

    result = await cli.repair_incident(
        db_session,
        proposal_id=group.proposal_id,
        rung_index=0,
        broker_order_id=BROKER_ORDER_ID,
        commit=False,
        fetch_order_fn=AsyncMock(return_value=_broker_order()),
    )

    fresh_group, fresh_rung = await _reload(db_session, group_id, rung_id)
    assert result["mode"] == "dry-run"
    assert result["after"]["state"] == "resting"
    assert result["after"]["group_lifecycle_state"] == "submitted"
    assert result["evidence"] == _broker_order()
    assert fresh_rung.state == "rejected"
    assert fresh_rung.broker_order_id is None
    assert fresh_rung.idempotency_key is None
    assert fresh_rung.void_reason == "submit failed"
    assert fresh_group.lifecycle_state == "rejected"


@pytest.mark.asyncio
async def test_repair_commit_updates_only_verified_ledger_fields(
    db_session, monkeypatch
):
    group, rung = await _seed_rejected_proposal(db_session, monkeypatch)
    group_id, rung_id = group.id, rung.id

    result = await cli.repair_incident(
        db_session,
        proposal_id=group.proposal_id,
        rung_index=0,
        broker_order_id=BROKER_ORDER_ID,
        commit=True,
        fetch_order_fn=AsyncMock(return_value=_broker_order()),
    )

    fresh_group, fresh_rung = await _reload(db_session, group_id, rung_id)
    assert result["mode"] == "commit"
    assert fresh_rung.state == "resting"
    assert fresh_rung.broker_order_id == BROKER_ORDER_ID
    assert fresh_rung.idempotency_key == "oprop-fixed"
    assert fresh_rung.void_reason is None
    assert fresh_rung.validated_at is not None
    assert fresh_rung.updated_at is not None
    assert fresh_group.lifecycle_state == "submitted"
    assert fresh_group.updated_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proposal_id", "broker_order_id", "broker_order", "match"),
    [
        (
            uuid.UUID("a81ffd0e-0000-4000-8000-000000000000"),
            BROKER_ORDER_ID,
            _broker_order(),
            "proposal id is not the ROB-837 incident",
        ),
        (
            PROPOSAL_ID,
            "45bee07f-full",
            _broker_order(uuid="45bee07f-full"),
            "broker order id is not the ROB-837 incident",
        ),
        (PROPOSAL_ID, BROKER_ORDER_ID, _broker_order(state="done"), "state"),
        (PROPOSAL_ID, BROKER_ORDER_ID, _broker_order(market="KRW-ETH"), "market"),
        (PROPOSAL_ID, BROKER_ORDER_ID, _broker_order(side="ask"), "side"),
        (PROPOSAL_ID, BROKER_ORDER_ID, _broker_order(ord_type="price"), "ord_type"),
        (PROPOSAL_ID, BROKER_ORDER_ID, _broker_order(price="88800001"), "price"),
        (PROPOSAL_ID, BROKER_ORDER_ID, _broker_order(volume="0.0005"), "volume"),
    ],
)
async def test_repair_rejects_bad_incident_or_broker_evidence_without_mutation(
    db_session,
    monkeypatch,
    proposal_id,
    broker_order_id,
    broker_order,
    match,
):
    group, rung = await _seed_rejected_proposal(db_session, monkeypatch)
    group_id, rung_id = group.id, rung.id

    with pytest.raises(ValueError, match=match):
        await cli.repair_incident(
            db_session,
            proposal_id=proposal_id,
            rung_index=0,
            broker_order_id=broker_order_id,
            commit=True,
            fetch_order_fn=AsyncMock(return_value=broker_order),
        )

    fresh_group, fresh_rung = await _reload(db_session, group_id, rung_id)
    assert fresh_rung.state == "rejected"
    assert fresh_rung.broker_order_id is None
    assert fresh_rung.idempotency_key is None
    assert fresh_rung.void_reason == "submit failed"
    assert fresh_group.lifecycle_state == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("symbol", "KRW-ETH", "symbol"),
        ("side", "sell", "side"),
        ("order_type", "market", "order type"),
    ],
)
async def test_repair_rejects_local_order_mismatch_without_mutation(
    db_session, monkeypatch, field, value, match
):
    group, rung = await _seed_rejected_proposal(db_session, monkeypatch)
    group_id, rung_id = group.id, rung.id
    if field == "side":
        group.side = value
        rung.side = value
    else:
        setattr(group, field, value)
    await db_session.commit()

    with pytest.raises(ValueError, match=match):
        await cli.repair_incident(
            db_session,
            proposal_id=group.proposal_id,
            rung_index=0,
            broker_order_id=BROKER_ORDER_ID,
            commit=True,
            fetch_order_fn=AsyncMock(return_value=_broker_order()),
        )

    fresh_group, fresh_rung = await _reload(db_session, group_id, rung_id)
    assert fresh_rung.state == "rejected"
    assert fresh_rung.broker_order_id is None
    assert fresh_group.lifecycle_state == "rejected"


@pytest.mark.asyncio
async def test_repair_recomputes_group_lifecycle_from_all_rungs(
    db_session, monkeypatch
):
    group, rung = await _seed_rejected_proposal(db_session, monkeypatch)
    group_id, rung_id = group.id, rung.id
    db_session.add(
        OrderProposalRung(
            proposal_pk=group.id,
            rung_index=1,
            side="buy",
            quantity=Decimal("0.0005"),
            limit_price=Decimal("88000000"),
            state="pending_approval",
        )
    )
    await db_session.commit()

    await cli.repair_incident(
        db_session,
        proposal_id=group.proposal_id,
        rung_index=0,
        broker_order_id=BROKER_ORDER_ID,
        commit=True,
        fetch_order_fn=AsyncMock(return_value=_broker_order()),
    )

    fresh_group, fresh_rung = await _reload(db_session, group_id, rung_id)
    assert fresh_rung.state == "resting"
    assert fresh_group.lifecycle_state == "partially_submitted"


@pytest.mark.asyncio
async def test_repair_rejects_non_rejected_rung_without_mutation(
    db_session, monkeypatch
):
    group, rung = await _seed_rejected_proposal(db_session, monkeypatch)
    group_id, rung_id = group.id, rung.id
    rung.state = "approved"
    await db_session.commit()

    with pytest.raises(ValueError, match="rung state is not rejected"):
        await cli.repair_incident(
            db_session,
            proposal_id=group.proposal_id,
            rung_index=0,
            broker_order_id=BROKER_ORDER_ID,
            commit=True,
            fetch_order_fn=AsyncMock(return_value=_broker_order()),
        )

    _, fresh_rung = await _reload(db_session, group_id, rung_id)
    assert fresh_rung.state == "approved"
    assert fresh_rung.broker_order_id is None


@pytest.mark.asyncio
async def test_repair_rejects_duplicate_broker_order_id_without_mutation(
    db_session, monkeypatch
):
    group, rung = await _seed_rejected_proposal(db_session, monkeypatch)
    group_id, rung_id = group.id, rung.id
    duplicate = OrderProposalRung(
        proposal_pk=group.id,
        rung_index=1,
        side="buy",
        quantity=Decimal("0.0004"),
        limit_price=Decimal("88800000"),
        state="rejected",
        broker_order_id=BROKER_ORDER_ID,
    )
    db_session.add(duplicate)
    await db_session.commit()

    with pytest.raises(ValueError, match="already bound to another rung"):
        await cli.repair_incident(
            db_session,
            proposal_id=group.proposal_id,
            rung_index=0,
            broker_order_id=BROKER_ORDER_ID,
            commit=True,
            fetch_order_fn=AsyncMock(return_value=_broker_order()),
        )

    _, fresh_rung = await _reload(db_session, group_id, rung_id)
    assert fresh_rung.state == "rejected"
    assert fresh_rung.broker_order_id is None


@pytest.mark.asyncio
async def test_repair_rolls_back_when_broker_lookup_fails(db_session, monkeypatch):
    group, rung = await _seed_rejected_proposal(db_session, monkeypatch)
    group_id, rung_id = group.id, rung.id

    with pytest.raises(RuntimeError, match="lookup unavailable"):
        await cli.repair_incident(
            db_session,
            proposal_id=group.proposal_id,
            rung_index=0,
            broker_order_id=BROKER_ORDER_ID,
            commit=True,
            fetch_order_fn=AsyncMock(side_effect=RuntimeError("lookup unavailable")),
        )

    fresh_group, fresh_rung = await _reload(db_session, group_id, rung_id)
    assert fresh_rung.state == "rejected"
    assert fresh_rung.broker_order_id is None
    assert fresh_group.lifecycle_state == "rejected"


def test_repair_source_never_imports_mutating_broker_operations():
    source = (
        Path(__file__).parents[2] / "scripts" / "rob837_reconcile_upbit_proposal.py"
    ).read_text()

    for forbidden in (
        "place_order",
        "cancel_order",
        "replace_order",
        "_execute_order",
    ):
        assert forbidden not in source


def test_parse_args_is_dry_run_by_default(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "rob837_reconcile_upbit_proposal.py",
            "--proposal-id",
            str(PROPOSAL_ID),
            "--broker-order-id",
            BROKER_ORDER_ID,
        ],
    )

    args = cli.parse_args()

    assert args.commit is False
    assert args.rung_index == 0
