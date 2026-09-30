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

from app.core.timezone import KST
from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.models.quotes_consumer import LadderTouchEvent, QuotesTriggerFiring
from app.services.quotes_consumer.consumer import (
    STREAM_KEY,
    ConsumerCounters,
    QuotesTossConsumer,
)
from app.services.quotes_consumer.repository import QuotesConsumerRepository
from app.services.quotes_consumer.triggers import ShadowKickGate, TriggerRow

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
    # Inside-band tick first: it clears any breach seeded from committed
    # rows so the later breach edge can fire (documented semantics).
    await fake_redis.xadd(
        STREAM_KEY,
        dict(TRADE_FIELDS, ts="2026-09-30T13:14:50.000+00:00", price="100000"),
    )
    await fake_redis.xadd(
        STREAM_KEY, dict(TRADE_FIELDS, ts="2026-09-30T13:15:00.000+00:00")
    )

    counters = await consumer.run(max_cycles=2)
    assert counters.entries_read == 2
    assert counters.entries_acked == 2
    assert counters.firings_inserted >= 1
    assert await _pending(fake_redis, consumer._group) == 0
    # Ladder events stay zero without rung anchors on this symbol.
    assert await _ladder_count(db_session) == 0


async def test_replayed_later_inbreach_batch_writes_no_second_firing(
    db_session, fake_redis, held_with_close, monkeypatch
) -> None:
    """BLOCKER-1 shape: batch1 holds the breach edge and is committed +
    acked; batch2 is still inside the same breach, commits, then loses
    its ack.  A fresh process replaying batch2 must not mint a second
    firing — the seeded in-breach state suppresses it."""
    first = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        now=lambda: T0 + timedelta(minutes=25),
    )
    await first.ensure_group()
    inside_fields = dict(
        TRADE_FIELDS, ts="2026-09-30T13:19:50.000+00:00", price="100000"
    )
    edge_fields = dict(TRADE_FIELDS, ts="2026-09-30T13:20:00.000+00:00", price="108000")
    inbreach_fields = dict(
        TRADE_FIELDS, ts="2026-09-30T13:20:07.000+00:00", price="108500"
    )
    await fake_redis.xadd(STREAM_KEY, inside_fields)
    await fake_redis.xadd(STREAM_KEY, edge_fields)
    await fake_redis.xadd(STREAM_KEY, inbreach_fields)

    # Batch1: an inside-band tick clears any breach seeded from prior
    # committed rows, then the edge fires once and acks normally.
    base = await _firing_count(db_session, SYM)
    entries = await first.read_batch()
    assert len(entries) == 3
    batch1 = entries[:2]
    batch2 = entries[2:]
    await first.consume_batch(batch1, ConsumerCounters())
    mid = await _firing_count(db_session, SYM)
    assert mid == base + 1

    # Batch2 commits nothing new (still in breach), then the ack is lost.
    async def _lost_ack(*args, **kwargs):
        raise RuntimeError("simulated crash between commit and xack")

    monkeypatch.setattr(fake_redis, "xack", _lost_ack)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await first.consume_batch(batch2, ConsumerCounters())
    assert await _pending(fake_redis, first._group) == 1

    # Fresh process — no in-memory edge state; committed rows are the
    # only barrier. The replayed in-breach tick cannot write a second row.
    monkeypatch.undo()
    second = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        consumer_name=first._consumer,
        now=lambda: T0 + timedelta(minutes=25),
    )
    counters = ConsumerCounters()
    await second.run(counters=counters, max_cycles=1)

    assert await _firing_count(db_session, SYM) == mid
    assert counters.entries_acked == 1
    assert await _pending(fake_redis, first._group) == 0


async def test_pending_beyond_read_count_is_fully_drained(
    db_session, fake_redis
) -> None:
    """Pending entries past the 256-entry claim window are paginated, not
    stranded until the next restart."""
    victim = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        consumer_name="victim-300",
    )
    await victim.ensure_group()
    for i in range(300):
        await fake_redis.xadd(
            STREAM_KEY,
            dict(TRADE_FIELDS, ts=f"2026-09-30T13:30:{i % 60:02d}.000+00:00"),
        )
    await fake_redis.xreadgroup(
        victim._group, "victim-300", {STREAM_KEY: ">"}, count=300
    )
    assert await _pending(fake_redis, victim._group) == 300

    claimed = await victim._claim_pending()
    assert len(claimed) == 300


async def test_live_peer_pending_entries_are_not_stolen(db_session, fake_redis) -> None:
    """A peer's just-read (in-flight) entries are never claimed — only
    entries idle past PENDING_MIN_IDLE_MS count as provably stalled."""
    group = "auto-trader-quotes-toss"
    peer_a = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        consumer_name="peer-a",
    )
    await peer_a.ensure_group()
    await fake_redis.xadd(
        STREAM_KEY, dict(TRADE_FIELDS, ts="2026-09-30T13:35:00.000+00:00")
    )
    assert await peer_a.read_batch() != []
    assert await _pending(fake_redis, group) == 1

    peer_b = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        consumer_name="peer-b",
    )
    claimed = await peer_b._claim_pending()
    assert claimed == []
    assert await _pending(fake_redis, group) == 1


async def test_restart_recovers_fills_that_landed_while_down(
    db_session, fake_redis, request
) -> None:
    """S5: the fill watermark resumes from the last recorded own_fill
    firing — not the current ledger max — so fills committed during
    downtime are still recorded exactly once."""
    from app.models.execution_ledger import ExecutionLedger
    from app.models.trading import InstrumentType

    n = _uniq(request.node.name)

    # Landed while the consumer was down — its id is past the recorded
    # watermark, which the committed own_fill row below encodes.
    missed = ExecutionLedger(
        broker="toss",
        venue="nxt",
        instrument_type=InstrumentType.equity_kr,
        symbol=SYM,
        raw_symbol=SYM,
        side="buy",
        broker_order_id=f"ORD-{n}-2",
        fill_seq=0,
        filled_qty=Decimal("1"),
        filled_price=Decimal("100000"),
        filled_notional=Decimal("100000"),
        filled_at=T0 + timedelta(minutes=40),
        currency="KRW",
    )
    db_session.add(missed)
    await db_session.flush()
    db_session.add(
        QuotesTriggerFiring(
            dedupe_key=f"qc-seed-ownfill-{n}",
            trigger_type="own_fill",
            outcome="fired",
            symbol=SYM,
            market="kr",
            window="fill",
            event_ts=T0 + timedelta(minutes=40),
            kst_date="2026-09-30",
            would_kick=False,
            daily_would_kick_count=0,
            source_ref=str(missed.id - 1),
            detail={},
        )
    )
    await db_session.flush()

    consumer = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        now=lambda: T0 + timedelta(minutes=45),
    )
    counters = ConsumerCounters()
    await consumer.consume_batch([], counters)

    recovered = (
        await db_session.execute(
            select(QuotesTriggerFiring).where(
                QuotesTriggerFiring.dedupe_key == f"ownfill:{missed.id}"
            )
        )
    ).scalar_one()
    assert recovered.trigger_type == "own_fill"
    assert counters.fills_seen >= 1


async def test_committed_touch_seeds_restart_state(
    db_session, fake_redis, request
) -> None:
    """BLOCKER-1 (ladder): a committed touch row seeds the fresh tracker
    as 'touched' — a replayed crossing tick writes no second row."""
    from app.models.review import TossLiveOrderLedger

    n = _uniq(request.node.name)
    sym = f"{SYM}T{n:03d}"
    order = TossLiveOrderLedger(
        trade_date=T0 - timedelta(minutes=10),
        operation_kind="place",
        market="kr",
        symbol=sym,
        side="buy",
        order_type="limit",
        price=Decimal("100000"),
        client_order_id=f"qc-touch-{n}",
        broker_order_id=f"TB-{n}",
        status="accepted",
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(
        LadderTouchEvent(
            dedupe_key=f"ladder:toss_live_order_ledger:{order.id}:touch",
            order_ledger="toss_live_order_ledger",
            order_ledger_id=order.id,
            broker_order_id=f"TB-{n}",
            event_type="touch",
            market="kr",
            symbol=sym,
            side="buy",
            session="krx_regular",
            anchor_price=Decimal("100000"),
            event_price=Decimal("99900"),
            event_ts=T0 + timedelta(minutes=50),
            stream_entry_id="99-1",
            detail={},
        )
    )
    await db_session.flush()

    consumer = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        now=lambda: T0 + timedelta(minutes=55),
    )
    counters = ConsumerCounters()
    crossing = dict(
        TRADE_FIELDS,
        symbol=sym,
        ts="2026-09-30T13:55:00.000+00:00",
        price="99000",
    )
    await consumer.consume_batch([("77-1", crossing)], counters)

    assert counters.ladder_events_inserted == 0
    events = (
        (
            await db_session.execute(
                select(LadderTouchEvent).where(
                    LadderTouchEvent.order_ledger_id == order.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1  # only the committed touch — no duplicate


async def test_open_rungs_keep_only_positive_limit_orders(
    db_session, fake_redis, request
) -> None:
    """Market orders and zero-priced rows are not rung anchors — only
    positive-price limit rows can produce ladder events."""
    from app.models.review import TossLiveOrderLedger

    n = _uniq(request.node.name)
    sym = f"{SYM}F{n:03d}"

    def _order(cid: str, order_type: str, price: str | None) -> TossLiveOrderLedger:
        return TossLiveOrderLedger(
            trade_date=T0 - timedelta(minutes=10),
            operation_kind="place",
            market="kr",
            symbol=sym,
            side="buy",
            order_type=order_type,
            price=Decimal(price) if price is not None else None,
            client_order_id=cid,
            status="accepted",
        )

    eligible = _order(f"qc-lim-{n}", "limit", "100000")
    db_session.add(eligible)
    db_session.add(_order(f"qc-mkt-{n}", "market", None))
    db_session.add(_order(f"qc-zero-{n}", "limit", "0"))
    await db_session.flush()

    consumer = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        now=lambda: T0 + timedelta(hours=1),
    )
    counters = ConsumerCounters()
    crossing = dict(
        TRADE_FIELDS,
        symbol=sym,
        ts="2026-09-30T14:00:00.000+00:00",
        price="99000",
    )
    await consumer.consume_batch([("88-1", crossing)], counters)

    events = (
        (
            await db_session.execute(
                select(LadderTouchEvent).where(
                    LadderTouchEvent.order_ledger_id.in_([eligible.id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert counters.ladder_events_inserted == 1
    assert len(events) == 1
    assert events[0].event_type == "touch"


async def test_gate_seed_restores_cooldown_across_midnight(db_session, request) -> None:
    """r2 SHOULD-5 (gate_seed scope): the seed returns the all-time
    latest would_kick timestamp, so a kick committed before KST
    midnight still suppresses on the new day after a restart — the
    same-day-only variant silently loses it."""
    n = _uniq(request.node.name)
    repo = QuotesConsumerRepository(db_session)
    kick_at = datetime(2026, 9, 29, 23, 40, tzinfo=KST)
    await repo.insert_firings(
        [
            TriggerRow(
                dedupe_key=f"qc-gateseed-{n}",
                trigger_type="own_fill",
                outcome="fired",
                symbol=f"{SYM}G{n:03d}",
                source_symbol=None,
                market="us",
                session=None,
                reference_price=None,
                current_price=Decimal("1"),
                window="fill",
                event_ts=kick_at,
                kst_date="2026-09-29",
                would_kick=True,
                suppress_reason=None,
                daily_would_kick_count=1,
                last_would_kick_at=kick_at,
                not_evaluable_reason=None,
                source_ref=f"gate-seed-{n}",
                detail={},
            )
        ]
    )
    await db_session.flush()

    gate = ShadowKickGate()
    for market, count, last_at in await repo.gate_seed("2026-09-30"):
        gate.seed(market, "2026-09-30", count, last_at)
    would_kick, reason, *_ = gate.decide("us", datetime(2026, 9, 30, 0, 10, tzinfo=KST))
    assert (would_kick, reason) == (False, "cooldown")


async def test_committed_approach_seeds_restart_state(
    db_session, fake_redis, request
) -> None:
    """r2 SHOULD-5 (rung_event_states seeding): a committed approach row
    seeds the fresh tracker as 'near' — a replayed in-band tick writes
    no second approach for the same rung."""
    from app.models.review import TossLiveOrderLedger

    n = _uniq(request.node.name)
    sym = f"{SYM}A{n:03d}"
    order = TossLiveOrderLedger(
        trade_date=T0 - timedelta(minutes=10),
        operation_kind="place",
        market="kr",
        symbol=sym,
        side="buy",
        order_type="limit",
        price=Decimal("100000"),
        client_order_id=f"qc-appr-{n}",
        broker_order_id=f"TA-{n}",
        status="accepted",
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(
        LadderTouchEvent(
            dedupe_key=f"ladder:toss_live_order_ledger:{order.id}:approach:11-1",
            order_ledger="toss_live_order_ledger",
            order_ledger_id=order.id,
            broker_order_id=f"TA-{n}",
            event_type="approach",
            market="kr",
            symbol=sym,
            side="buy",
            session="krx_regular",
            anchor_price=Decimal("100000"),
            event_price=Decimal("100400"),
            event_ts=T0 + timedelta(minutes=50),
            stream_entry_id="11-1",
            detail={},
        )
    )
    await db_session.flush()

    consumer = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        now=lambda: T0 + timedelta(minutes=56),
    )
    counters = ConsumerCounters()
    in_band = dict(
        TRADE_FIELDS,
        symbol=sym,
        ts="2026-09-30T13:56:00.000+00:00",
        price="100300",
    )
    await consumer.consume_batch([("78-1", in_band)], counters)

    assert counters.ladder_events_inserted == 0
    events = (
        (
            await db_session.execute(
                select(LadderTouchEvent).where(
                    LadderTouchEvent.order_ledger_id == order.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1  # only the committed approach — no duplicate


async def test_fill_from_wrong_broker_does_not_match_rung(
    db_session, fake_redis, request
) -> None:
    """r2 SHOULD-5 (broker constraint): a kis_live_order_ledger rung
    must not latch a toss-sourced execution_ledger row — broker
    identity is part of the match, not just symbol + order id."""
    from app.models.execution_ledger import ExecutionLedger
    from app.models.review import KISLiveOrderLedger
    from app.models.trading import InstrumentType

    n = _uniq(request.node.name)
    sym = f"{SYM}B{n:03d}"
    order = KISLiveOrderLedger(
        trade_date=T0 - timedelta(minutes=10),
        symbol=sym,
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("1"),
        price=Decimal("100000"),
        order_no=f"KB-{n}",
        account_mode="kis_live",
        broker="kis",
        status="accepted",
        lifecycle_state="open",
    )
    db_session.add(order)
    foreign_fill = ExecutionLedger(
        broker="toss",  # wrong broker for a kis-ledger rung
        venue="nxt",
        instrument_type=InstrumentType.equity_kr,
        symbol=sym,
        raw_symbol=sym,
        side="buy",
        broker_order_id=f"KB-{n}",
        fill_seq=0,
        filled_qty=Decimal("1"),
        filled_price=Decimal("100000"),
        filled_notional=Decimal("100000"),
        filled_at=T0 + timedelta(minutes=58),
        currency="KRW",
    )
    db_session.add(foreign_fill)
    await db_session.flush()
    # Seed the fill watermark just below this row so the poll returns
    # exactly it — otherwise a fresh install's ledger-max watermark
    # would hide it and the match path would never run.
    db_session.add(
        QuotesTriggerFiring(
            dedupe_key=f"qc-broker-seed-{n}",
            trigger_type="own_fill",
            outcome="fired",
            symbol=sym,
            market="kr",
            window="fill",
            event_ts=T0 + timedelta(minutes=58),
            kst_date="2026-09-30",
            would_kick=False,
            daily_would_kick_count=0,
            source_ref=str(foreign_fill.id - 1),
            detail={},
        )
    )
    await db_session.flush()

    consumer = QuotesTossConsumer(
        redis=fake_redis,
        session_factory=_factory(db_session),
        now=lambda: T0 + timedelta(minutes=59),
    )
    counters = ConsumerCounters()
    await consumer.consume_batch([], counters)

    fills = (
        (
            await db_session.execute(
                select(LadderTouchEvent).where(
                    LadderTouchEvent.order_ledger == "kis_live_order_ledger",
                    LadderTouchEvent.order_ledger_id == order.id,
                    LadderTouchEvent.event_type == "fill",
                )
            )
        )
        .scalars()
        .all()
    )
    assert fills == []
    # The fill itself is still recorded as an own_fill firing — the
    # broker mismatch only blocks the rung match.
    own = (
        await db_session.execute(
            select(QuotesTriggerFiring).where(
                QuotesTriggerFiring.dedupe_key == f"ownfill:{foreign_fill.id}"
            )
        )
    ).scalar_one()
    assert own.trigger_type == "own_fill"
