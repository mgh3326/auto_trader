"""Real PostgreSQL recorder transactions and fencing; run in CI, not host builders."""

import asyncio
import runpy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import AsyncSessionLocal
from app.services.mock_strategy.recorder import MockStrategyRecorder
from tests.services.mock_strategy.test_intents import intent

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def recorder():
    return MockStrategyRecorder()


@pytest.mark.parametrize(
    "venue",
    [
        "alpaca_paper",
        "alpaca_paper_crypto",
        "binance_spot_demo",
        "binance_futures_demo",
    ],
)
async def test_recorder_round_trip_and_real_two_session_claim(
    db_session, recorder, venue
):
    coid = f"r1474-db-{uuid4().hex}"
    order = replace(
        intent(client_order_id=coid),
        venue=venue,
        side="short" if venue == "binance_futures_demo" else "buy",
    )
    first = await recorder.record(order)
    assert (await recorder.record(order)).id == first.id
    won = await asyncio.gather(recorder.claim(coid), recorder.claim(coid))
    assert sorted(won) == [False, True]
    assert (await recorder.get(coid)).status == "sending"
    await recorder.update(coid, status="uncertain", meta={"reason": "synthetic"})
    assert not await recorder.claim(coid)
    assert (await recorder.record(order)).status == "uncertain"
    updated = await recorder.update(
        coid, status="accepted", broker_order_id="fixture-order"
    )
    assert updated.meta["reason"] == "synthetic"
    assert updated.notional == order.notional
    assert updated.exit_due_at == order.exit_due_at
    assert coid in {
        r.client_order_id
        for r in await recorder.list_open(venue=venue, account_ref="fixture")
    }
    assert coid not in {
        r.client_order_id
        for r in await recorder.list_due(
            venue=venue, asof=datetime(2026, 11, 9, 14, 29, tzinfo=UTC)
        )
    }
    assert coid in {
        r.client_order_id
        for r in await recorder.list_due(venue=venue, asof=order.exit_due_at)
    }
    await recorder.update(coid, status="closed", closed_at=datetime.now(UTC))
    assert coid not in {
        r.client_order_id for r in await recorder.list_open(venue=venue)
    }
    with pytest.raises(ValueError, match="terminal"):
        await recorder.update(coid, status="accepted")


async def test_conflicting_batch_rolls_back_new_intents(db_session, recorder):
    suffix = uuid4().hex
    old = intent(client_order_id=f"z-{suffix}")
    await recorder.record(old)
    new = intent(client_order_id=f"a-{suffix}")
    with pytest.raises(ValueError, match="conflict"):
        await recorder.record_many([new, replace(old, symbol="NVDA")])
    assert await recorder.get(new.client_order_id) is None


async def test_migration_trigger_preserves_the_send_fence(db_session, recorder):
    migration = runpy.run_path(
        str(
            Path(__file__).parents[3]
            / "alembic/versions/20261102_rob1474_mock_strategy_orders.py"
        )
    )
    exists = await db_session.scalar(
        text("SELECT to_regprocedure('review.mock_strategy_orders_guard()')")
    )
    if exists is None:
        for ddl in migration["GUARD_DDL"]:
            await db_session.execute(text(ddl))
        await db_session.commit()
    coid = f"r1474-guard-{uuid4().hex}"
    await recorder.record(intent(client_order_id=coid))
    await recorder.claim(coid)
    for sql in (
        "UPDATE review.mock_strategy_orders SET status='planned' WHERE client_order_id=:coid",
        "UPDATE review.mock_strategy_orders SET symbol='NVDA' WHERE client_order_id=:coid",
        "DELETE FROM review.mock_strategy_orders WHERE client_order_id=:coid",
    ):
        async with AsyncSessionLocal() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(sql), {"coid": coid})
                await session.commit()
            await session.rollback()
    assert (await recorder.get(coid)).status == "sending"
