"""Real PostgreSQL recorder transactions and fencing; run in CI, not host builders."""

import asyncio
import runpy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, NoResultFound

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
    first_open = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
    await recorder.update(coid, status="filled", opened_at=first_open)
    later = await recorder.update(
        coid, status="filled", opened_at=first_open + timedelta(days=1)
    )
    assert later.opened_at == first_open
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

    # The same trigger allows a zero-fill terminal outcome while keeping
    # the original client ID permanently fenced.
    absent = await recorder.confirm_alpaca_absence(coid, account_ref="fixture")
    assert absent.status == "rejected"
    assert absent.meta["broker_status"] == "broker_confirmed_absent"
    assert absent.meta["filled_qty"] == "0"
    assert (await recorder.get(coid)).meta == absent.meta
    assert not await recorder.claim(coid)


async def test_binance_spot_absence_closes_unbound_leg_with_zero_fill(
    db_session, recorder
):
    """hk 5910: only an unbound sending/uncertain spot-demo leg can be closed."""
    spot = f"r1474-bsa-{uuid4().hex}"
    await recorder.record(
        replace(intent(client_order_id=spot), venue="binance_spot_demo", side="buy")
    )
    assert await recorder.claim(spot)
    # Futures demo / Alpaca rows with the same state are never closed this way.
    futures = f"r1474-bfa-{uuid4().hex}"
    await recorder.record(
        replace(
            intent(client_order_id=futures),
            venue="binance_futures_demo",
            side="short",
        )
    )
    assert await recorder.claim(futures)
    with pytest.raises(NoResultFound):  # venue filter: no spot row for this id
        await recorder.confirm_binance_spot_absence(futures, account_ref="fixture")
    assert (await recorder.get(futures)).status == "sending"
    await recorder.update(spot, status="uncertain")
    closed = await recorder.confirm_binance_spot_absence(spot, account_ref="fixture")
    assert closed.status == "rejected"
    assert closed.meta["broker_status"] == "broker_confirmed_absent"
    assert closed.meta["filled_qty"] == "0"
    assert closed.meta["no_order_proven"] is True
    assert closed.meta["no_order_sent"] is True  # prior-revision readers skip it
    stored = await recorder.get(spot)
    assert stored.status == "rejected" and stored.broker_order_id is None
    assert not await recorder.claim(spot)  # the id stays fenced; no resend
    again = await recorder.confirm_binance_spot_absence(spot, account_ref="fixture")
    assert again.meta == closed.meta  # idempotent no-op
    # A bound or filled leg is refused by the same guard.
    bound = f"r1474-bsb-{uuid4().hex}"
    await recorder.record(
        replace(intent(client_order_id=bound), venue="binance_spot_demo", side="buy")
    )
    await recorder.claim(bound)
    await recorder.update(bound, status="uncertain", broker_order_id="bound-42")
    still = await recorder.confirm_binance_spot_absence(bound, account_ref="fixture")
    assert still.status == "uncertain"


async def test_symbol_change_journals_on_a_planned_exit_leg_only(db_session, recorder):
    coid = f"r1474-sc-{uuid4().hex}"
    leg = replace(intent(client_order_id=coid), symbol="BKH", side="sell")
    await recorder.record(leg)
    event = {"from": "BKH", "to": "HZN", "applied": True}
    await recorder.record_symbol_change(coid, event)
    await recorder.record_symbol_change(coid, event)  # same mapping: idempotent
    row = await recorder.get(coid)
    assert row.symbol == "BKH" and row.meta["symbol_change"] == event
    with pytest.raises(ValueError, match="conflicts"):
        await recorder.record_symbol_change(coid, {**event, "to": "ABC"})
    with pytest.raises(ValueError, match="journaled symbol"):
        await recorder.record_symbol_change(coid, {**event, "from": "ZZZ"})
    entry = replace(intent(client_order_id=f"{coid}-e"), symbol="BKH", side="buy")
    await recorder.record(entry)
    with pytest.raises(ValueError, match="planned exit leg"):
        await recorder.record_symbol_change(entry.client_order_id, event)
    assert await recorder.claim(coid)
    with pytest.raises(ValueError, match="planned exit leg"):
        await recorder.record_symbol_change(coid, event)
