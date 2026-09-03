from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals.web_approvals import WebApprovalService


@pytest.mark.asyncio
async def test_card_list_uses_one_joined_source_query_for_many_rungs() -> None:
    proposal_id = uuid.uuid4()
    now = datetime(2026, 9, 4, tzinfo=UTC)
    group = SimpleNamespace(
        proposal_id=proposal_id,
        market="equity_us",
        account_mode="kis_live",
        symbol="BRK.B",
        side="buy",
        action="place",
        valid_until=now + timedelta(minutes=5),
        approval_nonce="server-only-nonce",
        approval_nonce_used_at=None,
        commit_lease_until=None,
        approved_by_channel=None,
        approved_at=None,
    )
    rungs = [
        SimpleNamespace(
            rung_index=index,
            quantity=Decimal("1"),
            limit_price=Decimal("100.25"),
            notional=None,
            state="pending_approval",
            approval_hash_digest="digest",
            void_reason=None,
        )
        for index in range(3)
    ]

    class Proposals:
        calls = 0

        async def list_web_approval_card_rows(self, **kwargs):
            self.calls += 1
            assert kwargs == {"limit": 100}
            return [(group, rung) for rung in rungs]

    proposals = Proposals()
    cards = await WebApprovalService(proposals, now=now).list_cards()

    assert proposals.calls == 1
    assert len(cards) == 1
    assert cards[0]["status"] == "pending"
    assert cards[0]["approval_hash_present"] is True
    assert (
        cards[0]["preview"]
        == [{"quantity": "1", "limit_price": "100.25", "expected_amount": "100.25"}] * 3
    )
