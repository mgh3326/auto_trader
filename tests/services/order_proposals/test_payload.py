import pytest

from app.services.order_proposals.payload import (
    ProposalRungSpec,
    compute_proposal_payload_hash,
)


def _rungs():
    return [
        ProposalRungSpec(0, "buy", "10", "2226000", None),
        ProposalRungSpec(1, "buy", "5", "2200000", None),
    ]


@pytest.mark.unit
def test_hash_is_deterministic_and_order_insensitive():
    a = compute_proposal_payload_hash(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        order_type="limit",
        rungs=_rungs(),
    )
    b = compute_proposal_payload_hash(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        order_type="limit",
        rungs=list(reversed(_rungs())),
    )
    assert a == b
    assert len(a) == 64


@pytest.mark.unit
def test_price_change_changes_hash():
    base = compute_proposal_payload_hash(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        order_type="limit",
        rungs=_rungs(),
    )
    changed = compute_proposal_payload_hash(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        order_type="limit",
        rungs=[
            ProposalRungSpec(0, "buy", "10", "2340000", None),
            ProposalRungSpec(1, "buy", "5", "2200000", None),
        ],
    )
    assert base != changed


@pytest.mark.unit
def test_ttl_only_change_keeps_hash_stable():
    # Same price/qty; no TTL/timestamp participates in the hash.
    assert compute_proposal_payload_hash(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        order_type="limit",
        rungs=[ProposalRungSpec(0, "buy", "1", "100", None)],
    ) == compute_proposal_payload_hash(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        order_type="limit",
        rungs=[ProposalRungSpec(0, "buy", "1", "100", None)],
    )


@pytest.mark.unit
def test_loss_cut_binding_changes_payload_hash():
    ordinary = compute_proposal_payload_hash(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        order_type="limit",
        rungs=[ProposalRungSpec(0, "sell", "1", "70000", None)],
    )
    loss_cut = compute_proposal_payload_hash(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        order_type="limit",
        rungs=[ProposalRungSpec(0, "sell", "1", "70000", None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-800",
    )
    assert ordinary != loss_cut


@pytest.mark.unit
def test_payload_hash_binds_target_identity_and_remaining_qty():
    common = {
        "symbol": "KRW-AVAX",
        "market": "crypto",
        "account_mode": "upbit",
        "order_type": "limit",
        "rungs": [ProposalRungSpec(0, "sell", "3.5", "43000", None)],
    }
    snapshot = {
        "broker_order_id": "old-1",
        "symbol": "KRW-AVAX",
        "side": "sell",
        "order_type": "limit",
        "limit_price": "42000",
        "remaining_quantity": "3.5",
        "status": "open",
        "observed_at": "2026-07-11T17:23:00+09:00",
    }
    hashes = {
        compute_proposal_payload_hash(**common),
        compute_proposal_payload_hash(
            **common,
            action="replace",
            target_broker_order_id="old-1",
            target_order_snapshot=snapshot,
        ),
        compute_proposal_payload_hash(
            **common,
            action="replace",
            target_broker_order_id="old-2",
            target_order_snapshot={**snapshot, "broker_order_id": "old-2"},
        ),
        compute_proposal_payload_hash(
            **common,
            action="replace",
            target_broker_order_id="old-1",
            target_order_snapshot={**snapshot, "remaining_quantity": "3.4"},
        ),
    }
    assert len(hashes) == 4
