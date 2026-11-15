"""hk 4702 trend state service, guard triggers and migration on the test DB.

CI only: runs against the run-owned test database (never a production database).
The trend tables are append-only for every role, so teardown turns triggers off
for its own transaction only.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
from decimal import Decimal as D
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import DBAPIError

from app.core.db import AsyncSessionLocal, engine
from app.services.brokers.binance.trend.entry_gate import EntryRefusal
from app.services.brokers.binance.trend.state import (
    TrendEntryRefused,
    TrendStateBlocked,
    TrendStateService,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# Saturday 2026-11-14 03:00 UTC; the UTC week began Monday 2026-11-09.
NOW = dt.datetime(2026, 11, 14, 3, tzinfo=dt.UTC)
BASE_TS = int(NOW.timestamp() * 1000)
MIGRATION = (
    Path(__file__).resolve().parents[5]
    / "alembic/versions/20261114_hk4702_trend_state.py"
)


async def _wipe() -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        await conn.execute(sa.text("DELETE FROM review.binance_trend_intents"))
        await conn.execute(sa.text("DELETE FROM review.binance_trend_signals"))


@pytest_asyncio.fixture(autouse=True)
async def clean(_bootstrap_test_schema):
    await _wipe()
    yield
    await _wipe()


@pytest.fixture
def svc() -> TrendStateService:
    return TrendStateService(AsyncSessionLocal)


_ts = iter(range(BASE_TS, BASE_TS + 10**9, 60_000))


async def observe(svc: TrendStateService, symbol: str = "BTCUSDT", side: str = "BUY"):
    return await svc.observe_signal(
        symbol=symbol, side=side, decision_ts=next(_ts), price_text="100.5"
    )


async def reserve(svc: TrendStateService, symbol: str = "BTCUSDT", notional="60"):
    signal = await observe(svc, symbol)
    return await svc.reserve_entry(
        signal_key=signal.signal_key, notional_usdt=D(notional), now=NOW
    )


async def seed(**values) -> None:
    """Direct row for a state the service cannot reach yet (test-only)."""
    row = {
        "side": "BUY",
        "decision_ts": next(_ts),
        "signal_price_text": "100",
        "state": "observed",
        "entry_qty": 0,
        "closed_qty": 0,
        "realized_pnl_usdt": 0,
        "fees_usdt": 0,
    } | values
    row.setdefault("signal_key", f"{row['symbol']}|seed|{row['decision_ts']}")
    row.setdefault("correlation_id", "binance-trend:" + row["signal_key"])
    cols = ", ".join(row)
    binds = ", ".join(f":{k}" for k in row)
    async with engine.begin() as conn:
        await conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        await conn.execute(
            sa.text(
                f"INSERT INTO review.binance_trend_signals ({cols}) VALUES ({binds})"
            ),
            row,
        )


async def refused(svc, symbol, notional="60") -> tuple[str, ...]:
    signal = await observe(svc, symbol)
    with pytest.raises(TrendEntryRefused) as caught:
        await svc.reserve_entry(
            signal_key=signal.signal_key, notional_usdt=D(notional), now=NOW
        )
    return caught.value.reasons


async def test_reservation_fills_four_slots_then_refuses_the_fifth(svc) -> None:
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
        assert (await reserve(svc, symbol)).state == "entry_reserved"
    assert len(await svc.list_active_signals()) == 4
    reasons = await refused(svc, "BTCUSDT")
    assert EntryRefusal.MAX_CONCURRENT_POSITIONS_REACHED in reasons
    assert EntryRefusal.SYMBOL_ALREADY_ACTIVE in reasons


async def test_second_position_on_one_symbol_is_refused(svc) -> None:
    await reserve(svc, "ETHUSDT")
    assert EntryRefusal.SYMBOL_ALREADY_ACTIVE in await refused(svc, "ETHUSDT")
    assert (await reserve(svc, "SOLUSDT")).state == "entry_reserved"


async def test_leg_cap_is_enforced(svc) -> None:
    assert EntryRefusal.LEG_NOTIONAL_CAP_EXCEEDED in await refused(
        svc, "BTCUSDT", "60.01"
    )


async def test_gross_over_240_is_refused(svc) -> None:
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        await seed(symbol=symbol, state="holding", entry_notional_usdt=D("70"))
    assert await refused(svc, "XRPUSDT") == (EntryRefusal.GROSS_NOTIONAL_CAP_EXCEEDED,)


async def test_two_consecutive_hard_stops_in_a_utc_day_block_entries(svc) -> None:
    for hours, symbol in ((2, "BTCUSDT"), (1, "ETHUSDT")):
        await seed(
            symbol=symbol,
            state="closed",
            exit_reason="hard_stop",
            exit_at=NOW - dt.timedelta(hours=hours),
        )
    assert EntryRefusal.CONSECUTIVE_HARD_STOP_LIMIT_REACHED in await refused(
        svc, "SOLUSDT"
    )


async def test_the_daily_hard_stop_stop_is_a_latch(svc) -> None:
    # Two consecutive hard stops, then another position exits otherwise: the
    # newest row is no longer a hard stop but the day stays stopped.
    for minutes, symbol, reason in (
        (120, "BTCUSDT", "hard_stop"),
        (90, "ETHUSDT", "hard_stop"),
        (60, "SOLUSDT", "signal_reversal"),
    ):
        await seed(
            symbol=symbol,
            state="closed",
            exit_reason=reason,
            exit_at=NOW - dt.timedelta(minutes=minutes),
        )
    assert EntryRefusal.CONSECUTIVE_HARD_STOP_LIMIT_REACHED in await refused(
        svc, "XRPUSDT"
    )


async def test_non_consecutive_hard_stops_do_not_trip_the_daily_stop(svc) -> None:
    for minutes, symbol, reason in (
        (120, "BTCUSDT", "hard_stop"),
        (90, "ETHUSDT", "time_exit"),
        (60, "SOLUSDT", "hard_stop"),
    ):
        await seed(
            symbol=symbol,
            state="closed",
            exit_reason=reason,
            exit_at=NOW - dt.timedelta(minutes=minutes),
        )
    assert (await reserve(svc, "XRPUSDT")).state == "entry_reserved"


async def test_hard_stops_from_the_previous_utc_day_do_not_count(svc) -> None:
    yesterday = dt.datetime(2026, 11, 13, 23, tzinfo=dt.UTC)
    for symbol in ("BTCUSDT", "ETHUSDT"):
        await seed(
            symbol=symbol, state="closed", exit_reason="hard_stop", exit_at=yesterday
        )
    assert (await reserve(svc, "SOLUSDT")).state == "entry_reserved"


async def test_three_conflicts_in_a_utc_week_block_entries(svc) -> None:
    for symbol in ("BTCUSDT", "ETHUSDT"):
        signal = await observe(svc, symbol)
        await svc.record_conflict_blocked(signal.signal_key, now=NOW, detail="foreign")
    # The third is a broker rejection of a sent entry.
    signal = await reserve(svc, "SOLUSDT")
    intent = await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side="BUY",
        qty=D("0.5"),
        reference_price=D("10"),
        reduce_only=False,
        now=NOW,
    )
    await svc.mark_sending(intent.client_order_id, now=NOW)
    assert (await reserve(svc, "XRPUSDT")).state == "entry_reserved"
    await svc.record_conflict_rejected(
        intent.client_order_id, broker_status="REJECTED_CONFLICT", now=NOW
    )
    assert EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED in await refused(svc, "BTCUSDT")


async def _seed_conflicts(*when: dt.datetime) -> None:
    for symbol, moment in zip(
        ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"), when, strict=False
    ):
        await seed(symbol=symbol, state="conflict_blocked", conflict_at=moment)


async def test_the_conflict_stop_survives_the_week_boundary(svc) -> None:
    # Three on Sunday 2026-11-08; "now" is the following Saturday.
    sunday = dt.datetime(2026, 11, 8, 20, tzinfo=dt.UTC)
    await _seed_conflicts(sunday, sunday, sunday)
    assert EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED in await refused(svc, "XRPUSDT")


async def test_conflicts_spread_one_per_week_do_not_trip_the_stop(svc) -> None:
    await _seed_conflicts(
        dt.datetime(2026, 10, 27, tzinfo=dt.UTC),
        dt.datetime(2026, 11, 3, tzinfo=dt.UTC),
        dt.datetime(2026, 11, 10, tzinfo=dt.UTC),
    )
    assert (await reserve(svc, "XRPUSDT")).state == "entry_reserved"


async def test_only_an_operator_resolution_releases_the_conflict_stop(svc) -> None:
    sunday = dt.datetime(2026, 11, 8, 20, tzinfo=dt.UTC)
    await _seed_conflicts(sunday, sunday, sunday)
    assert EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED in await refused(svc, "XRPUSDT")
    assert await svc.resolve_conflicts(resolved_by="operator", now=NOW) == 3
    assert await svc.resolve_conflicts(resolved_by="operator", now=NOW) == 0
    assert (await reserve(svc, "XRPUSDT")).state == "entry_reserved"


async def test_the_database_allows_only_a_one_time_resolution(svc) -> None:
    await _seed_conflicts(NOW)
    for statement in (
        "UPDATE review.binance_trend_signals SET conflict_detail = 'edited'",
        "UPDATE review.binance_trend_signals SET state = 'blocked', "
        "conflict_resolved_at = now(), conflict_resolved_by = 'op'",
    ):
        async with AsyncSessionLocal() as db:
            with pytest.raises(DBAPIError, match="frozen"):
                await db.execute(sa.text(statement))
            await db.rollback()
    await svc.resolve_conflicts(resolved_by="op", now=NOW)
    async with AsyncSessionLocal() as db:
        with pytest.raises(DBAPIError, match="frozen"):
            await db.execute(
                sa.text(
                    "UPDATE review.binance_trend_signals SET "
                    "conflict_resolved_by = 'someone else'"
                )
            )
        await db.rollback()


async def test_the_send_fence_rejudges_the_kill_switches(svc) -> None:
    for symbol in ("BTCUSDT", "ETHUSDT"):
        await seed(symbol=symbol, state="conflict_blocked", conflict_at=NOW)
    signal = await reserve(svc, "XRPUSDT")
    intent = await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side="BUY",
        qty=D("1"),
        reference_price=D("10"),
        reduce_only=False,
        now=NOW,
    )
    # A third conflict lands between the reservation and the fence.
    other = await observe(svc, "SOLUSDT")
    await svc.record_conflict_blocked(other.signal_key, now=NOW)
    with pytest.raises(TrendEntryRefused) as caught:
        await svc.mark_sending(intent.client_order_id, now=NOW)
    assert EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED in caught.value.reasons
    (stored,) = await svc.list_intents(signal.signal_key)
    assert stored.state == "reserved"


async def test_the_send_fence_does_not_count_its_own_reservation(svc) -> None:
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        await reserve(svc, symbol)
    signal = await reserve(svc, "XRPUSDT")
    intent = await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side="BUY",
        qty=D("1"),
        reference_price=D("10"),
        reduce_only=False,
        now=NOW,
    )
    assert (await svc.mark_sending(intent.client_order_id, now=NOW)).state == "sending"


async def test_an_intent_cannot_exceed_the_reserved_notional(svc) -> None:
    signal = await reserve(svc, "BTCUSDT", notional="1")
    for qty in ("0.11", "1000"):
        with pytest.raises(TrendStateBlocked, match="reserved notional"):
            await svc.reserve_intent(
                signal_key=signal.signal_key,
                leg_key="entry",
                side="BUY",
                qty=D(qty),
                reference_price=D("10"),
                reduce_only=False,
                now=NOW,
            )
    ok = await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side="BUY",
        qty=D("0.1"),
        reference_price=D("10"),
        reduce_only=False,
        now=NOW,
    )
    assert ok.state == "reserved"


async def test_conflict_blocked_releases_an_unsent_reservation(svc) -> None:
    signal = await reserve(svc, "BTCUSDT")
    intent = await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side="BUY",
        qty=D("1"),
        reference_price=D("10"),
        reduce_only=False,
        now=NOW,
    )
    done = await svc.record_conflict_blocked(signal.signal_key, now=NOW, detail="x")
    assert done.state == "conflict_blocked" and done.conflict_at == NOW
    (stored,) = await svc.list_intents(signal.signal_key)
    assert stored.client_order_id == intent.client_order_id
    assert stored.state == "rejected"
    assert await svc.list_active_signals() == ()


async def test_a_sent_entry_cannot_be_conflict_blocked(svc) -> None:
    signal = await reserve(svc, "BTCUSDT")
    intent = await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side="BUY",
        qty=D("1"),
        reference_price=D("10"),
        reduce_only=False,
        now=NOW,
    )
    await svc.mark_sending(intent.client_order_id, now=NOW)
    with pytest.raises(TrendStateBlocked):
        await svc.record_conflict_blocked(signal.signal_key, now=NOW)
    with pytest.raises(TrendStateBlocked):
        await svc.block_unsent(signal.signal_key, now=NOW)


async def _uncertain(svc, symbol="BTCUSDT"):
    signal = await reserve(svc, symbol)
    intent = await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side="BUY",
        qty=D("1"),
        reference_price=D("10"),
        reduce_only=False,
        now=NOW,
    )
    await svc.mark_sending(intent.client_order_id, now=NOW)
    await svc.mark_uncertain(intent.client_order_id, now=NOW)
    return signal, intent


async def test_an_uncertain_intent_blocks_its_symbol_and_is_never_resent(svc) -> None:
    signal, intent = await _uncertain(svc, "BTCUSDT")
    assert (await svc.get_signal(signal.signal_key)).state == "uncertain"
    assert intent.client_order_id in {
        i.client_order_id for i in await svc.list_unresolved_intents()
    }
    # New entry on the symbol is refused, another symbol is not.
    assert EntryRefusal.UNCERTAIN_INTENT_UNRESOLVED in await refused(svc, "BTCUSDT")
    assert (await reserve(svc, "ETHUSDT")).state == "entry_reserved"
    # The send fence cannot be taken again, the leg cannot be re-reserved, the
    # signal cannot be released.
    with pytest.raises(TrendStateBlocked):
        await svc.mark_sending(intent.client_order_id, now=NOW)
    with pytest.raises(TrendStateBlocked):
        await svc.reserve_intent(
            signal_key=signal.signal_key,
            leg_key="entry",
            side="BUY",
            qty=D("1"),
            reference_price=D("10"),
            reduce_only=False,
            now=NOW,
        )
    with pytest.raises(TrendStateBlocked):
        await svc.block_unsent(signal.signal_key, now=NOW)
    with pytest.raises(TrendStateBlocked):
        await svc.record_conflict_rejected(
            intent.client_order_id, broker_status="x", now=NOW
        )


async def test_the_database_refuses_to_resend_an_uncertain_intent(svc) -> None:
    _, intent = await _uncertain(svc)
    for state in ("reserved", "sending", "acknowledged"):
        async with AsyncSessionLocal() as db:
            with pytest.raises(DBAPIError, match="move forward"):
                await db.execute(
                    sa.text(
                        "UPDATE review.binance_trend_intents SET state = :s "
                        "WHERE client_order_id = :c"
                    ),
                    {"s": state, "c": intent.client_order_id},
                )
            await db.rollback()


async def test_concurrent_reservations_cannot_both_take_the_last_slot(svc) -> None:
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        await reserve(svc, symbol)
    first, second = await observe(svc, "XRPUSDT"), await observe(svc, "XRPUSDT")
    results = await asyncio.gather(
        svc.reserve_entry(signal_key=first.signal_key, notional_usdt=D("60"), now=NOW),
        svc.reserve_entry(signal_key=second.signal_key, notional_usdt=D("60"), now=NOW),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == [
        "TrendEntryRefused",
        "TrendSignalSnapshot",
    ]
    assert len(await svc.list_active_signals()) == 4


async def test_observe_is_idempotent_and_reserve_is_single_use(svc) -> None:
    ts = next(_ts)
    a = await svc.observe_signal(
        symbol="BTCUSDT", side="BUY", decision_ts=ts, price_text="1.5"
    )
    b = await svc.observe_signal(
        symbol="BTCUSDT", side="BUY", decision_ts=ts, price_text="1.5"
    )
    assert a == b and a.correlation_id.startswith("binance-trend:")
    await svc.reserve_entry(signal_key=a.signal_key, notional_usdt=D("10"), now=NOW)
    with pytest.raises(TrendStateBlocked):
        await svc.reserve_entry(signal_key=a.signal_key, notional_usdt=D("10"), now=NOW)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM review.binance_trend_signals",
        "TRUNCATE review.binance_trend_signals, review.binance_trend_intents",
        "UPDATE review.binance_trend_signals SET symbol = 'ETHUSDT'",
        "UPDATE review.binance_trend_signals SET signal_key = 'other'",
        "UPDATE review.binance_trend_signals SET decision_ts = 1",
        "INSERT INTO review.binance_trend_signals (signal_key, correlation_id, symbol, "
        "side, decision_ts, signal_price_text, state) VALUES "
        "('k', 'c', 'BTCUSDT', 'BUY', 1, '1', 'closed')",
        "DELETE FROM review.binance_trend_intents",
        "UPDATE review.binance_trend_intents SET qty = qty + 1",
        "UPDATE review.binance_trend_intents SET side = 'SELL'",
        "UPDATE review.binance_trend_intents SET leg_key = 'exit'",
        "UPDATE review.binance_trend_intents SET reduce_only = true",
    ],
)
async def test_the_database_keeps_trend_state_append_only(svc, statement) -> None:
    signal = await reserve(svc, "BTCUSDT")
    await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side="BUY",
        qty=D("1"),
        reference_price=D("10"),
        reduce_only=False,
        now=NOW,
    )
    async with AsyncSessionLocal() as db:
        with pytest.raises(DBAPIError, match="trend"):
            await db.execute(sa.text(statement))
        await db.rollback()


async def test_terminal_signals_are_frozen_and_intents_start_unsent(svc) -> None:
    signal = await observe(svc, "BTCUSDT")
    await svc.block_unsent(signal.signal_key, now=NOW)
    async with AsyncSessionLocal() as db:
        with pytest.raises(DBAPIError, match="frozen"):
            await db.execute(
                sa.text(
                    "UPDATE review.binance_trend_signals SET state = 'observed' "
                    "WHERE signal_key = :k"
                ),
                {"k": signal.signal_key},
            )
        await db.rollback()
    live = await reserve(svc, "ETHUSDT")
    async with AsyncSessionLocal() as db:
        with pytest.raises(DBAPIError, match="unsent reserved"):
            await db.execute(
                sa.text(
                    "INSERT INTO review.binance_trend_intents (client_order_id, "
                    "signal_key, leg_key, side, qty, reduce_only, state) VALUES "
                    "('c1', :k, 'entry', 'BUY', 1, false, 'sending')"
                ),
                {"k": live.signal_key},
            )
        await db.rollback()


async def test_check_constraints_hold_the_notional_and_conflict_record(svc) -> None:
    for statement in (
        # An active signal needs its reserved notional.
        "INSERT INTO review.binance_trend_signals (signal_key, correlation_id, "
        "symbol, side, decision_ts, signal_price_text, state) VALUES "
        "('a', 'ca', 'BTCUSDT', 'BUY', 1, '1', 'entry_reserved')",
        # A conflict record needs its time, and only a conflict state has one.
        "INSERT INTO review.binance_trend_signals (signal_key, correlation_id, "
        "symbol, side, decision_ts, signal_price_text, state) VALUES "
        "('b', 'cb', 'BTCUSDT', 'BUY', 1, '1', 'conflict_blocked')",
    ):
        async with AsyncSessionLocal() as db:
            await db.execute(sa.text("SET LOCAL session_replication_role = replica"))
            with pytest.raises(DBAPIError, match="ck_binance_trend"):
                await db.execute(sa.text(statement))
            await db.rollback()


def _migration():
    spec = importlib.util.spec_from_file_location("hk4702_db", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(sync_conn, fn: str) -> None:
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(_migration(), fn)()


async def _drop_trend_objects(conn) -> None:
    await conn.execute(
        sa.text("DROP TABLE IF EXISTS review.binance_trend_intents CASCADE")
    )
    await conn.execute(
        sa.text("DROP TABLE IF EXISTS review.binance_trend_signals CASCADE")
    )


async def test_migration_upgrade_then_downgrade_round_trips() -> None:
    """Runs inside one transaction that is always rolled back."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await _drop_trend_objects(conn)
            await conn.run_sync(_run_migration, "upgrade")
            found = (
                await conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_trigger WHERE tgname LIKE "
                        "'trg_binance_trend_%' AND NOT tgisinternal"
                    )
                )
            ).scalar_one()
            assert found == 4
            await conn.run_sync(_run_migration, "downgrade")
            gone = (
                await conn.execute(
                    sa.text("SELECT to_regclass('review.binance_trend_signals')")
                )
            ).scalar_one()
            assert gone is None
        finally:
            await trans.rollback()


async def test_migration_downgrade_refuses_while_rows_exist() -> None:
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await _drop_trend_objects(conn)
            await conn.run_sync(_run_migration, "upgrade")
            await conn.execute(
                sa.text(
                    "INSERT INTO review.binance_trend_signals (signal_key, "
                    "correlation_id, symbol, side, decision_ts, signal_price_text, "
                    "state) VALUES ('k', 'c', 'BTCUSDT', 'BUY', 1, '1', 'observed')"
                )
            )
            with pytest.raises(DBAPIError, match="holds rows"):
                await conn.run_sync(_run_migration, "downgrade")
        finally:
            await trans.rollback()
