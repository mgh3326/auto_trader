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
    from app.models.market_quote_snapshot import MarketQuoteSnapshot

    stmt = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.client_order_id.like("rob73-%")
        | AlpacaPaperOrderLedger.client_order_id.like("rob74-crypto-%")
    )
    snap_stmt = delete(MarketQuoteSnapshot).where(MarketQuoteSnapshot.symbol == "AAPL")
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.execute(snap_stmt)
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.execute(snap_stmt)
        await db.commit()


async def _seed_snapshot(price: str = "150") -> int:
    from datetime import UTC, datetime, timedelta

    from app.models.market_quote_snapshot import MarketQuoteSnapshot

    async with AsyncSessionLocal() as db:
        row = MarketQuoteSnapshot(
            market="us",
            symbol="AAPL",
            source="yahoo",
            snapshot_at=datetime.now(UTC) - timedelta(seconds=10),
            price=Decimal(price),
        )
        db.add(row)
        await db.commit()
        return row.id


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
    sid = await _seed_snapshot()
    first = await alpaca_paper_submit_order(**_BUY, quote_snapshot_id=sid, confirm=True)
    second = await alpaca_paper_submit_order(
        **_BUY, quote_snapshot_id=sid, confirm=True
    )

    assert first["submitted"] is True
    assert second["submitted"] is False
    assert second["status"] in {"replayed", "recovered"}
    submit_calls = [c for c in fake_service.calls if c[0] == "submit_order"]
    assert len(submit_calls) == 1


async def test_manual_confirm_exactly_once_parallel(fake_service):
    sid = await _seed_snapshot()
    results = await asyncio.gather(
        alpaca_paper_submit_order(**_BUY, quote_snapshot_id=sid, confirm=True),
        alpaca_paper_submit_order(**_BUY, quote_snapshot_id=sid, confirm=True),
    )
    submit_calls = [c for c in fake_service.calls if c[0] == "submit_order"]
    assert len(submit_calls) == 1
    assert sum(1 for r in results if r["submitted"]) == 1


async def test_manual_works_with_automated_flag_off(fake_service, monkeypatch):
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", False)
    sid = await _seed_snapshot()
    payload = await alpaca_paper_submit_order(
        **_BUY, quote_snapshot_id=sid, confirm=True
    )
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


# ---------------------------------------------------------------------------
# F1 — manual confirm requires server-observed market evidence (no origin bypass)
# ---------------------------------------------------------------------------
async def test_manual_confirm_without_snapshot_fails_close(fake_service):
    payload = await alpaca_paper_submit_order(**_BUY, confirm=True)
    assert payload["success"] is False
    assert payload["reason_code"] == "missing_market_evidence"
    assert fake_service.calls == []
    assert await _count_rows("rob73-") == 0


async def test_manual_confirm_with_stale_snapshot_fails_close(fake_service):
    from datetime import UTC, datetime, timedelta

    from app.models.market_quote_snapshot import MarketQuoteSnapshot

    async with AsyncSessionLocal() as db:
        row = MarketQuoteSnapshot(
            market="us",
            symbol="AAPL",
            source="yahoo",
            snapshot_at=datetime.now(UTC) - timedelta(hours=1),
            price=Decimal("150"),
        )
        db.add(row)
        await db.commit()
        sid = row.id
    payload = await alpaca_paper_submit_order(
        **_BUY, quote_snapshot_id=sid, confirm=True
    )
    assert payload["success"] is False
    assert payload["reason_code"] == "stale_trusted_snapshot"
    assert fake_service.calls == []


# ---------------------------------------------------------------------------
# F3 — public success contract for manual (422 => success=false)
# ---------------------------------------------------------------------------
async def test_manual_http_422_success_false(monkeypatch):
    from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError

    class _Raising(FakeOrdersService):
        async def submit_order(self, request):  # type: ignore[override]
            self.calls.append(("submit_order", {"request": request}))
            raise AlpacaPaperRequestError("bad", status_code=422)

    service = _Raising()
    set_alpaca_paper_orders_service_factory(lambda: service)  # type: ignore[arg-type]
    try:
        sid = await _seed_snapshot()
        payload = await alpaca_paper_submit_order(
            **_BUY, quote_snapshot_id=sid, confirm=True
        )
        assert payload["status"] == "failed"
        assert payload["success"] is False
        assert len([c for c in service.calls if c[0] == "submit_order"]) == 1
    finally:
        reset_alpaca_paper_orders_service_factory()


# ---------------------------------------------------------------------------
# G2 — manual equity market qty is bound by trusted reference_price × qty
# ---------------------------------------------------------------------------
async def test_manual_market_qty_bypass_of_hard_cap_fails_close(fake_service):
    # AAPL market qty=5 at trusted price 100,000 => $500,000 implied notional.
    sid = await _seed_snapshot(price="100000")
    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="market",
        qty=Decimal("5"),
        quote_snapshot_id=sid,
        confirm=True,
    )
    assert payload["success"] is False
    assert payload["reason_code"] == "notional_exceeds_max"
    assert [c for c in fake_service.calls if c[0] == "submit_order"] == []


async def test_manual_notional_within_cap_still_posts(fake_service):
    # Regression: a normal small order still goes through.
    sid = await _seed_snapshot(price="150")
    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="market",
        notional=Decimal("300"),
        quote_snapshot_id=sid,
        confirm=True,
    )
    assert payload["submitted"] is True
    assert len([c for c in fake_service.calls if c[0] == "submit_order"]) == 1


# ---------------------------------------------------------------------------
# G4c — the cancel tool releases a sell reservation via the ledger
# ---------------------------------------------------------------------------
async def test_cancel_tool_releases_sell_reservation(fake_service):
    from datetime import UTC, datetime

    from app.mcp_server.tooling.alpaca_paper_orders import alpaca_paper_cancel_order
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
    from app.services.brokers.alpaca.schemas import Order

    coid = "rob74-crypto-cxlreserve"
    # Seed an OPEN (submitted) sell reservation row.
    async with AsyncSessionLocal() as db:
        db.add(
            AlpacaPaperOrderLedger(
                client_order_id=coid,
                lifecycle_correlation_id=coid,
                record_kind="execution",
                broker="alpaca",
                account_mode="alpaca_paper",
                lifecycle_state="submitted",
                execution_symbol="BTC/USD",
                execution_venue="alpaca_paper",
                instrument_type=InstrumentType.crypto,
                side="sell",
                order_type="limit",
                currency="USD",
                requested_qty=Decimal("0.5"),
                submitted_at=datetime.now(UTC),
                broker_order_id="paper-cxl-1",
                confirm_flag=True,
            )
        )
        await db.commit()

    # The cancel read-back returns the order carrying the client_order_id.
    async def _get_order(_id):
        return Order(
            id="paper-cxl-1",
            client_order_id=coid,
            symbol="BTC/USD",
            filled_qty=Decimal("0"),
            side="sell",
            type="limit",
            time_in_force="gtc",
            status="canceled",
        )

    fake_service.get_order = _get_order  # type: ignore[assignment]
    result = await alpaca_paper_cancel_order(order_id="paper-cxl-1", confirm=True)
    assert result["reservation_released"] is True

    # The row is now canceled -> no longer counts against sellable position.
    from app.services.alpaca_paper_ledger_service import (
        AlpacaPaperLedgerService as _L,
    )

    async with AsyncSessionLocal() as db:
        claim = await _L(db).reserve_sell_and_claim(
            client_order_id="rob74-crypto-afterc",
            lifecycle_correlation_id="rob74-crypto-afterc",
            execution_symbol="BTC/USD",
            execution_venue="alpaca_paper",
            instrument_type=InstrumentType.crypto,
            requested_qty=Decimal("1"),
            position_qty=Decimal("1"),
        )
        assert claim.insufficient is False  # reservation released -> full position free
        _ = AlpacaPaperLedgerService  # keep import used
