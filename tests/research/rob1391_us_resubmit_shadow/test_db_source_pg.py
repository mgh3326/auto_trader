"""DbParentSource end to end on a throwaway PostgreSQL database.

Runs only when ``AUTO_TRADER_TEST_DATABASE_URL`` names a disposable server
(never the local :5432). The test creates its own uniquely named database,
creates the six tables from the ORM models, seeds rows through the ORM, runs
the real read-only SELECTs, and drops the database.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytestmark = pytest.mark.unit

BASE = os.environ.get("AUTO_TRADER_TEST_DATABASE_URL")


@pytest.mark.skipif(
    not BASE, reason="needs a disposable PostgreSQL via AUTO_TRADER_TEST_DATABASE_URL"
)
def test_real_selects_against_orm_tables(monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.models.order_proposals import OrderProposal, OrderProposalRung
    from app.models.review import (
        KISLiveOrderLedger,
        LiveOrderLedger,
        TossLiveOrderLedger,
    )
    from app.models.symbol_sectors import SymbolSector
    from app.models.us_symbol_universe import USSymbolUniverse
    from research.rob1391_us_resubmit_shadow.db_source import DbParentSource
    from research.rob1391_us_resubmit_shadow.parents import assemble_orders

    base = make_url(BASE).set(drivername="postgresql+asyncpg")
    name = f"rob1391_probe_{secrets.token_hex(4)}"
    probe = base.set(database=name)
    tables = [
        t.__table__
        for t in (
            TossLiveOrderLedger,
            LiveOrderLedger,
            KISLiveOrderLedger,
            OrderProposal,
            OrderProposalRung,
            SymbolSector,
            USSymbolUniverse,
        )
    ]
    ordered_at = datetime(2025, 11, 17, 13, 5, tzinfo=UTC)

    async def setup():
        admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        await admin.dispose()
        engine = create_async_engine(probe)
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE SCHEMA review")
            for table in tables:
                await conn.run_sync(lambda c, t=table: t.create(c))
        async with AsyncSession(engine) as session:
            session.add(
                TossLiveOrderLedger(
                    trade_date=ordered_at,
                    operation_kind="place",
                    market="us",
                    symbol="SYMA",
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("3"),
                    price=Decimal("99.5"),
                    currency="USD",
                    client_order_id="tossp6-fixture",
                    broker_order_id="TOSSORD00000001",
                    status="expired",
                    filled_qty=Decimal("1"),
                    avg_fill_price=Decimal("99.4"),
                    expired_at=datetime(2025, 11, 17, 23, 50, tzinfo=UTC),
                )
            )
            session.add(
                LiveOrderLedger(
                    trade_date=ordered_at,
                    broker="kis",
                    account_scope="kis_live",
                    market="us",
                    symbol="SYMB",
                    exchange="NASD",
                    side="buy",
                    order_kind="limit",
                    quantity=Decimal("1"),
                    price=Decimal("412.1"),
                    currency="USD",
                    order_no="0030100055",
                    status="expired",
                    lifecycle_state="expired",
                )
            )
            session.add(
                USSymbolUniverse(
                    symbol="SYMA",
                    exchange="NASD",
                    name_en="Syma Inc",
                    is_common_stock=True,
                )
            )
            await session.commit()
        await engine.dispose()

    async def teardown():
        admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()

    asyncio.run(setup())
    try:
        monkeypatch.setenv(
            "ROB1391_PROBE_DSN", probe.render_as_string(hide_password=False)
        )
        snap = DbParentSource("ROB1391_PROBE_DSN").fetch(
            datetime(2025, 11, 1, tzinfo=UTC)
        )
        assert sorted(r["source"] for r in snap.order_rows) == [
            "live_order_ledger",
            "toss_live_order_ledger",
        ]
        assert [u["symbol"] for u in snap.universe_rows] == ["SYMA"]
        orders = {
            o.source: o
            for o in assemble_orders(
                snap.order_rows, snap.rung_rows, snap.universe_rows
            )
        }
        toss = orders["toss_live_order_ledger"]
        assert (
            toss.price == Decimal("99.5")
            and toss.filled_qty == Decimal("1")
            and toss.ordered_at == ordered_at
        )
        assert orders["live_order_ledger"].broker_order_id == "0030100055"
    finally:
        asyncio.run(teardown())
