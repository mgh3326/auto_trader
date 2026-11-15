"""hk 4783 — concurrent FIFO partial-lot consumption against a run-owned DB.

The shared close walks a symbol/account lot pool under ``SELECT ... FOR UPDATE``
with a deterministic (created_at, id) lock order, so two sells journaled at the
same time can never read the same lot snapshot and both consume it.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal, engine
from app.mcp_server.tooling.order_journal import _close_journals_on_sell
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType
from tests._run_owned_database import validate_run_owned_database_url

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

validate_run_owned_database_url(engine.url)

ACCOUNT = "test-hk4783"


async def _seed_lot(*, symbol: str, quantity: Decimal) -> int:
    async with AsyncSessionLocal() as db:
        row = TradeJournal(
            symbol=symbol,
            instrument_type=InstrumentType.crypto,
            thesis="hk 4783 concurrency lot",
            status=JournalStatus.active,
            side="buy",
            entry_price=Decimal("100"),
            quantity=quantity,
            amount=quantity * Decimal("100"),
            account_type="live",
            account=ACCOUNT,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def test_concurrent_sells_cannot_double_consume_one_lot_pool() -> None:
    symbol = f"H4783-{uuid.uuid4().hex[:10]}"
    lot_id = await _seed_lot(symbol=symbol, quantity=Decimal("10"))
    try:
        results = await asyncio.gather(
            _close_journals_on_sell(
                symbol=symbol,
                sell_quantity=6.0,
                sell_price=110.0,
                exit_reason="concurrent-1",
                account=ACCOUNT,
            ),
            _close_journals_on_sell(
                symbol=symbol,
                sell_quantity=6.0,
                sell_price=120.0,
                exit_reason="concurrent-2",
                account=ACCOUNT,
            ),
        )

        # 12 shares sold against a 10-share pool: exactly 10 are matched and the
        # 2-share shortfall is reported, never silently dropped.
        matched = sum(Decimal("10") - r["unmatched_quantity"] for r in results)
        assert matched == Decimal("10")
        assert sum(r["unmatched_quantity"] for r in results) == Decimal("2")
        assert all(r["journals_closed"] >= 1 for r in results)

        async with AsyncSessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(TradeJournal).where(TradeJournal.symbol == symbol)
                    )
                )
                .scalars()
                .all()
            )
            lot = await db.get(TradeJournal, lot_id)

        # Invariant: closed quantity equals the bought quantity, and no lot
        # stays active with double-consumed shares.
        closed_qty = sum(
            (row.quantity or Decimal("0"))
            for row in rows
            if row.status == JournalStatus.closed
        )
        assert closed_qty == Decimal("10")
        assert sum(1 for row in rows if row.status == JournalStatus.active) == 0
        assert lot is not None
        assert lot.status == JournalStatus.closed
        assert lot.exit_reason in {"concurrent-1", "concurrent-2"}
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(TradeJournal).where(TradeJournal.symbol == symbol))
            await db.commit()
