"""hk 4783 — concurrent FIFO partial-lot consumption against a run-owned DB.

The shared close walks a symbol/account lot pool under ``SELECT ... FOR UPDATE``
with a deterministic (created_at, id) lock order, so two sells journaled at the
same time can never read the same lot snapshot and both consume it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal, engine
from app.core.timezone import KST
from app.mcp_server.tooling.order_journal import _close_journals_on_sell
from app.models.review import Trade
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

        # 12 shares sold against a 10-share pool: exactly 10 are matched (each
        # sell covers 6 minus its unmatched part) and the 2-share shortfall is
        # reported, never silently dropped.
        matched = sum(Decimal("6") - r["unmatched_quantity"] for r in results)
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


async def test_an_order_number_of_another_day_never_rewrites_the_trade_row() -> None:
    """hk 5642: ``review.trades`` is unique on (account, order_id) and the broker
    reuses order numbers across trading days.

    A same-day cumulative update still advances the order's row; the same number
    on another trading day is another order, so the earlier row keeps its day and
    quantities and the later fill claims no trade id (no journal can point at the
    earlier day's row).
    """
    from app.mcp_server.tooling.order_journal import _record_trade_for_fill

    order_id = f"H5642-{uuid.uuid4().hex[:10]}"
    day1 = date(2026, 11, 16)
    day2 = date(2026, 11, 17)

    async def _record(quantity: float, total: float, day: date) -> int | None:
        async with AsyncSessionLocal() as db:
            trade_id = await _record_trade_for_fill(
                db,
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                price=70000.0,
                quantity=quantity,
                total_amount=total,
                fee=0.0,
                currency="KRW",
                account=ACCOUNT,
                order_id=order_id,
                trade_day=day,
            )
            await db.commit()
            return trade_id

    try:
        first = await _record(4.0, 280000.0, day1)
        assert first is not None

        again = await _record(10.0, 700000.0, day1)
        assert again == first

        later = await _record(6.0, 420000.0, day2)
        assert later is None

        async with AsyncSessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(Trade).where(
                            Trade.account == ACCOUNT, Trade.order_id == order_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].id == first
        assert rows[0].quantity == Decimal("10")
        assert rows[0].total_amount == Decimal("700000")
        assert rows[0].trade_date.astimezone(KST).date() == day1
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(Trade).where(
                    Trade.account == ACCOUNT, Trade.order_id == order_id
                )
            )
            await db.commit()
