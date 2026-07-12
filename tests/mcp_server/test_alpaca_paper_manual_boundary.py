"""ROB-842 blocker 1: manual alpaca_paper_submit_order routes every real broker
POST through the durable packet + ledger atomic-claim coordinator.

No direct-POST fallback; exactly-once for duplicate manual intents; behaviour is
independent of the automated feature flag; dry-run mutates nothing.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.alpaca_paper_orders import (
    alpaca_paper_submit_order,
    reset_alpaca_paper_orders_service_factory,
    set_alpaca_paper_orders_service_factory,
)
from app.models.review import AlpacaPaperOrderLedger
from tests.test_alpaca_paper_orders_tools import FakeOrdersService

pytestmark = [pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean():
    stmt = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.client_order_id.like("rob73-%")
        | AlpacaPaperOrderLedger.client_order_id.like("rob74-crypto-%")
    )
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()


@pytest.fixture
def fake_service() -> FakeOrdersService:
    service = FakeOrdersService()
    set_alpaca_paper_orders_service_factory(lambda: service)  # type: ignore[arg-type]
    yield service
    reset_alpaca_paper_orders_service_factory()


_BUY = {
    "symbol": "AAPL",
    "side": "buy",
    "type": "limit",
    "qty": Decimal("1"),
    "limit_price": Decimal("1.00"),
}


async def _count_rows(coid_prefix: str) -> int:
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(AlpacaPaperOrderLedger).where(
                        AlpacaPaperOrderLedger.client_order_id.like(f"{coid_prefix}%")
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


async def test_manual_confirm_goes_through_claim_exactly_once_sequential(fake_service):
    first = await alpaca_paper_submit_order(**_BUY, confirm=True)
    second = await alpaca_paper_submit_order(**_BUY, confirm=True)

    assert first["submitted"] is True
    assert second["submitted"] is False
    assert second["status"] in {"replayed", "recovered"}
    submit_calls = [c for c in fake_service.calls if c[0] == "submit_order"]
    assert len(submit_calls) == 1


async def test_manual_confirm_exactly_once_parallel(fake_service):
    results = await asyncio.gather(
        alpaca_paper_submit_order(**_BUY, confirm=True),
        alpaca_paper_submit_order(**_BUY, confirm=True),
    )
    submit_calls = [c for c in fake_service.calls if c[0] == "submit_order"]
    assert len(submit_calls) == 1
    assert sum(1 for r in results if r["submitted"]) == 1


async def test_manual_works_with_automated_flag_off(fake_service, monkeypatch):
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", False)
    payload = await alpaca_paper_submit_order(**_BUY, confirm=True)
    # Manual path does not depend on the automated flag; it still routes through
    # the claim and POSTs exactly once.
    assert payload["submitted"] is True
    assert len([c for c in fake_service.calls if c[0] == "submit_order"]) == 1
    assert await _count_rows("rob73-") == 1  # a claim/execution row exists


async def test_manual_dry_run_mutates_nothing(fake_service):
    payload = await alpaca_paper_submit_order(**_BUY, confirm=False)
    assert payload["submitted"] is False
    assert payload["blocked_reason"] == "confirmation_required"
    assert fake_service.calls == []
    assert await _count_rows("rob73-") == 0  # no ledger row written on dry-run
