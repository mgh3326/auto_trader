"""hk 3717 — read-only ledger/rung loader of ``kis_live_get_daily_orders`` (test DB)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.timezone import KST
from app.mcp_server.tooling.kis_live_daily_orders import (
    _load_system_facts,
    kis_live_get_daily_orders_impl,
)
from app.models.order_proposals import OrderProposalRung
from app.models.review import KISLiveOrderLedger
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_VOID_REASON,
)
from app.services.order_proposals.service import RungInput

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DAY = "2026-11-02"


def _ts(day: int, hour: int = 10) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, 0, tzinfo=KST)


class _Kis:
    def __init__(self, order_no: str) -> None:
        self.order_no = order_no

    async def inquire_daily_order_domestic(self, **_kw: Any) -> list[dict[str, Any]]:
        return [
            {
                "odno": self.order_no,
                "orgn_odno": "",
                "sll_buy_dvsn_cd": "02",
                "pdno": "005930",
                "ord_qty": "1",
                "ord_unpr": "1000",
                "tot_ccld_qty": "0",
                "rmn_qty": "1",
                "rjct_qty": "0",
            }
        ]


async def _seed(*, ledger_day: int, marked: bool, rung_stripped: bool = False) -> str:
    order_no = f"00{uuid.uuid4().int % 10**8:08d}"
    rung_order_no = order_no.lstrip("0") if rung_stripped else order_no
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        group = await service.create_proposal(
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="p",
            strategy="underwater_support_net",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("1000"), None)],
            creator_agent_id="test",
        )
        for state in ("revalidating", "approved", "submitting"):
            await service.transition_rung(group.proposal_id, 0, new_state=state)
        await service.record_resting(
            group.proposal_id,
            0,
            broker_order_id=rung_order_no,
            correlation_id=f"corr-{order_no}",
            idempotency_key=f"idem-{order_no}",
            approval_hash_digest="digest",
            now=_ts(ledger_day),
        )
        if marked:
            rung = (
                await session.execute(
                    select(OrderProposalRung).where(
                        OrderProposalRung.broker_order_id == rung_order_no
                    )
                )
            ).scalar_one()
            rung.void_reason = EXPIRED_INFERENCE_VOID_REASON
        session.add(
            KISLiveOrderLedger(
                trade_date=_ts(ledger_day),
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("1000"),
                amount=Decimal("1000"),
                currency="KRW",
                order_no=order_no,
                account_mode="kis_live",
                broker="kis",
                status="accepted",
                lifecycle_state="accepted",
                idempotency_key=f"idem-{order_no}",
                correlation_id=f"corr-{order_no}",
            )
        )
        await session.commit()
    return order_no


async def test_loader_returns_ledger_and_marked_rung_without_writing() -> None:
    order_no = await _seed(ledger_day=2, marked=True)
    ledger, rungs = await _load_system_facts([order_no, order_no.lstrip("0")])
    assert [f.order_no for f in ledger] == [order_no]
    assert [r.broker_order_id for r in rungs] == [order_no]


async def test_end_to_end_inference_basis_on_the_matching_date() -> None:
    order_no = await _seed(ledger_day=2, marked=True)
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=lambda: _Kis(order_no)
    )
    (row,) = out["orders"]
    assert row["system_status"] == {"status": "accepted", "lifecycle_state": "accepted"}
    assert row["system_status_basis"] == "inference"
    assert out["system_status_error"] is None


async def test_end_to_end_unmarked_rung_is_ledger_basis() -> None:
    order_no = await _seed(ledger_day=2, marked=False)
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=lambda: _Kis(order_no)
    )
    assert out["orders"][0]["system_status_basis"] == "ledger"


async def test_reused_order_number_from_another_date_is_not_joined() -> None:
    order_no = await _seed(ledger_day=1, marked=True)
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=lambda: _Kis(order_no)
    )
    (row,) = out["orders"]
    assert row["system_status"] is None
    assert row["system_status_basis"] is None


async def test_marked_rung_stored_without_zero_padding_still_marks_inference() -> None:
    order_no = await _seed(ledger_day=2, marked=True, rung_stripped=True)
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=lambda: _Kis(order_no)
    )
    assert out["orders"][0]["system_status_basis"] == "inference"
