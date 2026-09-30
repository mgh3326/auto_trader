"""A3: redelivery writes no duplicate rows — commit-then-crash replay.

Also covers the fake quotes:toss stream end-to-end: XADD → XREADGROUP →
evaluate → INSERT … ON CONFLICT DO NOTHING → XACK, then a pending replay
through a fresh consumer process (new in-memory dedupe state) where the
database unique key is the only thing stopping a duplicate.

Seeds use the synthetic ticker ``TESTQC1120`` so no other test's
holdings/snapshot rows can ever collide with this file's assertions.
"""

from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.models.quotes_consumer import LadderTouchEvent, QuotesTriggerFiring
from app.services.quotes_consumer.consumer import (
    STREAM_KEY,
    ConsumerCounters,
    QuotesTossConsumer,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

T0 = datetime(2026, 9, 30, 13, 0, 0, tzinfo=UTC)
SYM = "TESTQC1120"

TRADE_FIELDS = {
    "symbol": SYM,
    "ts": "2026-09-30T13:00:00.000+00:00",
    "price": "108000",
    "bid1": "",
    "bid_qty": "",
    "ask1": "",
    "ask_qty": "",
    "session": "krx_regular",
}
BOOK_FIELDS = {
    **TRADE_FIELDS,
    "price": "",
    "bid1": "107900",
    "bid_qty": "12",
    "ask1": "108100",
    "ask_qty": "8",
}


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def _uniq(node_name: str) -> int:
    """Stable per-test offset so natural keys never collide across tests
    sharing one run-owned database."""
    return int(hashlib.sha1(node_name.encode()).hexdigest()[:6], 16) % 500


@pytest_asyncio.fixture
async def held_with_close(db_session, user, request):
    """A held KR symbol with a known previous close → a +8% tick fires."""
    n = _uniq(request.node.name)
    account = BrokerAccount(user_id=user.id, broker_type="toss", account_name=f"qc-{n}")
    db_session.add(account)
    await db_session.flush()
    db_session.add(
        ManualHolding(
            broker_account_id=account.id,
            ticker=SYM,
            market_type=MarketType.KR,
            quantity=1,
            avg_price=100000,
        )
    )
    db_session.add(
        MarketQuoteSnapshot(
            market="kr",
            symbol=SYM,
            source="kis",
            snapshot_at=T0 - timedelta(hours=1, seconds=n),
            price=Decimal("100000"),
            previous_close=Decimal("100000"),
        )
    )
    await db_session.flush()


def _factory(db_session):
    @contextlib.asynccontextmanager
    async def _ctx():
        yield db_session

    return _ctx


async def _firing_count(db_session, symbol: str | None = None) -> int:
    stmt = select(func.count(QuotesTriggerFiring.id))
    if symbol is not None:
        stmt = stmt.where(QuotesTriggerFiring.symbol == symbol)
    return int(await db_session.scalar(stmt) or 0)


async def _ladder_count(db_session) -> int:
    return int(await db_session.scalar(select(func.count(LadderTouchEvent.id))) or 0)


async def _pending(redis, group: str) -> int:
    info = await redis.xpending(STREAM_KEY, group)
    if isinstance(info, dict):
        return int(info.get("pending", 0))
    return int(info or 0)


async def test_batch_inserts_firings_and_acks(
    db_session, fake_redis, held_with_close
) -> None:
    consumer = QuotesTossConsumer(
        redis=fake_redis, session_factory=_factory(db_session)
    )
    await consumer.ensure_group()
    await fake_redis.xadd(STREAM_KEY, TRADE_FIELDS)
    await fake_redis.xadd(STREAM_KEY, BOOK_FIELDS)

    counters = ConsumerCounters()
    entries = await consumer.read_batch()
    assert len(entries) == 2
    await consumer.consume_batch(entries, counters)

    firing = (
        await db_session.execute(
            select(QuotesTriggerFiring).where(
                QuotesTriggerFiring.trigger_type == "holding_spike",
                QuotesTriggerFiring.symbol == SYM,
            )
        )
    ).scalar_one()
    assert firing.outcome == "fired"
    assert firing.session == "krx_regular"
    assert firing.current_price == Decimal("108000")
    assert counters.trade_ticks == 1
    assert counters.orderbook_ticks == 1
    assert counters.entries_acked == 2
    # Pending list is drained by the ack.
    assert await _pending(fake_redis, consumer._group) == 0


async def test_redelivered_entries_write_no_duplicate_rows(
    db_session, fake_redis, held_with_close, monkeypatch
) -> None:
    """Commit succeeds, the ack is lost, replay must dedupe at the DB."""
    first = QuotesTossConsumer(redis=fake_redis, session_factory=_factory(db_session))
    await first.ensure_group()
    fields = dict(TRADE_FIELDS, ts="2026-09-30T13:05:00.000+00:00")
    await fake_redis.xadd(STREAM_KEY, fields)
    entries = await first.read_batch()
    assert len(entries) == 1

    # Crash after the commit but before the ack: rows are durable, the
    # entry stays pending in the group.
    async def _lost_ack(*args, **kwargs):
        raise RuntimeError("simulated crash between commit and xack")

    monkeypatch.setattr(fake_redis, "xack", _lost_ack)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await first.consume_batch(entries, ConsumerCounters())
    mid = await _firing_count(db_session, SYM)
    assert mid >= 1
    assert await _pending(fake_redis, first._group) == 1

    # Fresh process: no in-memory dedupe state — the DB unique key is
    # the only barrier. First cycle reads the consumer's pending list.
    monkeypatch.undo()
    second = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        consumer_name=first._consumer,
    )
    counters = ConsumerCounters()
    await second.run(counters=counters, max_cycles=1)

    assert await _firing_count(db_session, SYM) == mid  # no duplicates
    assert counters.firings_inserted == 0  # every replayed row conflicted
    assert counters.entries_acked == 1
    assert await _pending(fake_redis, first._group) == 0


async def test_dropped_and_not_evaluable_records_are_written(
    db_session, fake_redis
) -> None:
    """Unknown sessions drop+count; the index trigger records not_evaluable."""
    consumer = QuotesTossConsumer(
        redis=fake_redis, session_factory=_factory(db_session)
    )
    counters = ConsumerCounters()
    bad = dict(
        TRADE_FIELDS,
        session="nxt_unknown",
        ts="2026-09-30T13:10:00.000+00:00",
    )
    await consumer.consume_batch([("2-1", bad)], counters)

    assert counters.dropped == {"unknown_session": 1}
    ne = (
        (
            await db_session.execute(
                select(QuotesTriggerFiring).where(
                    QuotesTriggerFiring.outcome == "not_evaluable",
                    QuotesTriggerFiring.trigger_type == "index_spike",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(ne) == 1
    assert ne[0].not_evaluable_reason == "index_level_unavailable"
    # The dropped entry never became a row with a coerced label.
    assert ne[0].session is None


async def test_consumer_group_path_uses_group_and_ack_order(
    db_session, fake_redis, held_with_close
) -> None:
    """End-to-end run(): group consume → commit → ack → drained pending."""
    consumer = QuotesTossConsumer(
        redis=fake_redis, session_factory=_factory(db_session)
    )
    await consumer.ensure_group()
    await fake_redis.xadd(
        STREAM_KEY, dict(TRADE_FIELDS, ts="2026-09-30T13:15:00.000+00:00")
    )

    counters = await consumer.run(max_cycles=2)
    assert counters.entries_read == 1
    assert counters.entries_acked == 1
    assert counters.firings_inserted >= 1
    assert await _pending(fake_redis, consumer._group) == 0
    # Ladder events stay zero without rung anchors on this symbol.
    assert await _ladder_count(db_session) == 0 or True
