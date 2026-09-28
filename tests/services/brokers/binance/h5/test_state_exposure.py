from __future__ import annotations

import asyncio
import datetime as dt
import multiprocessing
from decimal import Decimal
from multiprocessing.managers import SyncManager
from types import SimpleNamespace

import pytest

from app.models.binance_h5 import BinanceH5Intent, BinanceH5LaneState, BinanceH5Signal
from app.services.brokers.binance.demo_strategy_loop.strategy import Signal
from app.services.brokers.binance.futures_demo.dto import FuturesDemoOpenOrdersResult
from app.services.brokers.binance.h5.exposure import assert_account_exposure
from app.services.brokers.binance.h5.state import (
    H5OrderEvidence,
    H5StateBlocked,
    H5StateService,
)
from app.services.brokers.binance.h5.strategy import IDENTITY
from tests.services.brokers.binance.h5.fake_state_db import (
    NOW,
    FakeStore,
    intent_values,
    lane_values,
    signal_values,
)


def _reserve_process(data, lock, ready, results, key):
    store = FakeStore(data, lock)
    ready.wait()
    try:
        asyncio.run(
            H5StateService(store.factory).reserve_entry(
                signal_key=key,
                completed_bar_closes=(),
                entry_nav_usdt=Decimal("1000"),
                now=NOW,
            )
        )
        results.put("reserved")
    except H5StateBlocked:
        results.put("blocked")


def _local_counter_process(data, ready, key):
    # This deliberately flawed process-local check admits three simultaneous
    # reservations: every process sees zero before any process writes.
    open_count = sum(row["state"] == "entry_reserved" for row in data.values())
    ready.wait()
    if open_count < 2:
        row = dict(data[key])
        row["state"] = "entry_reserved"
        data[key] = row


def test_independent_process_local_counter_exhibits_three_position_counterexample():
    ctx = multiprocessing.get_context("spawn")
    # Loopback TCP keeps the shared store on an address class the ROB-1880
    # socket guard permits; the default AF_UNIX manager socket is blocked.
    with SyncManager(ctx=ctx, address=("127.0.0.1", 0)) as manager:
        data = manager.dict({f"s{i}": signal_values(key=f"s{i}") for i in range(3)})
        ready = manager.Barrier(3)
        processes = [
            ctx.Process(target=_local_counter_process, args=(data, ready, f"s{i}"))
            for i in range(3)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert sum(row["state"] == "entry_reserved" for row in data.values()) == 3


@pytest.mark.parametrize(
    "symbols,allowed",
    [
        (("BTCUSDT", "ETHUSDT", "SOLUSDT"), 2),
        (("BTCUSDT", "BTCUSDT", "BTCUSDT"), 1),
    ],
)
def test_independent_concurrent_processes_cannot_exceed_caps(symbols, allowed):
    # Each spawned process imports the service independently and uses only
    # the shared fake table store; no process opens a real DB connection.
    ctx = multiprocessing.get_context("spawn")
    with SyncManager(ctx=ctx, address=("127.0.0.1", 0)) as manager:
        data, lock = manager.dict(), manager.RLock()
        store = FakeStore(data, lock)
        store.put(BinanceH5LaneState, lane_values())
        for index, symbol in enumerate(symbols):
            store.put(BinanceH5Signal, signal_values(key=f"s{index}", symbol=symbol))
        ready, results = manager.Barrier(3), manager.Queue()
        processes = [
            ctx.Process(
                target=_reserve_process, args=(data, lock, ready, results, f"s{i}")
            )
            for i in range(3)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        outcomes = [results.get(timeout=2) for _ in processes]
        assert outcomes.count("reserved") == allowed
        assert (
            sum(
                row["state"] == "entry_reserved"
                for row in store.values(BinanceH5Signal)
            )
            == allowed
        )


def test_durable_exact_signal_record_and_h5_identity():
    store = FakeStore()
    state = H5StateService(store.factory)
    signal = Signal(
        symbol="BTCUSDT",
        side="BUY",
        decision_ts=int(NOW.timestamp() * 1000),
        strategy_id=IDENTITY,
        reason="formula",
    )
    first = asyncio.run(state.observe_signal(signal, "100.00"))
    restarted = H5StateService(store.factory)
    second = asyncio.run(restarted.observe_signal(signal, "100.00"))
    assert first == second
    assert first.signal_key == "BTCUSDT|2026-09-28 13:00|BUY|100.00"
    assert first.correlation_id.startswith("binance-h5:")
    assert len(store.values(BinanceH5Signal)) == 1


def test_reentry_bans_first_five_complete_bars_then_allows_sixth():
    store = FakeStore()
    store.put(BinanceH5LaneState, lane_values())
    timestamp = signal_values()["decision_ts"]
    store.put(
        BinanceH5Signal,
        signal_values(
            key="old", state="closed", exit_bar_close_ts=timestamp - 6 * 14_400_000
        ),
    )
    store.put(BinanceH5Signal, signal_values())
    state = H5StateService(store.factory)
    with pytest.raises(H5StateBlocked, match="five complete-bar"):
        asyncio.run(
            state.reserve_entry(
                signal_key="s1",
                completed_bar_closes=tuple(
                    timestamp - i * 14_400_000 for i in range(5)
                ),
                entry_nav_usdt=Decimal("1000"),
                now=NOW,
            )
        )
    result = asyncio.run(
        state.reserve_entry(
            signal_key="s1",
            completed_bar_closes=tuple(timestamp - i * 14_400_000 for i in range(6)),
            entry_nav_usdt=Decimal("1000"),
            now=NOW,
        )
    )
    assert result.state == "entry_reserved"


@pytest.mark.parametrize(
    "lane_changes,stops,reason",
    [
        ({"last_nav_usdt": Decimal("970")}, 0, "daily_loss_entry_stop"),
        ({"last_nav_usdt": Decimal("850")}, 0, "mdd_lane_stop"),
        ({}, 3, "three_stops_today"),
        ({"halt_reason": "mdd_lane_stop"}, 0, "mdd_lane_stop"),
    ],
)
def test_service_kill_gates_refuse_reservation(lane_changes, stops, reason):
    store = FakeStore()
    store.put(BinanceH5LaneState, lane_values(**lane_changes))
    store.put(BinanceH5Signal, signal_values())
    for i in range(stops):
        store.put(
            BinanceH5Signal,
            signal_values(
                key=f"exit{i}", state="closed", exit_at=NOW, exit_reason="hard_stop"
            ),
        )
    state = H5StateService(store.factory)
    with pytest.raises(H5StateBlocked, match=reason):
        asyncio.run(
            state.reserve_entry(
                signal_key="s1",
                completed_bar_closes=(),
                entry_nav_usdt=Decimal("1000"),
                now=NOW,
            )
        )
    assert (
        next(row for row in store.values(BinanceH5Signal) if row["signal_key"] == "s1")[
            "state"
        ]
        == "observed"
    )


def test_daily_loss_stop_stays_latched_until_next_kst_day():
    store = FakeStore()
    store.put(BinanceH5LaneState, lane_values())
    state = H5StateService(store.factory)
    asyncio.run(state.record_nav(nav_usdt=Decimal("970"), now=NOW))
    asyncio.run(
        state.record_nav(nav_usdt=Decimal("1000"), now=NOW + dt.timedelta(minutes=1))
    )
    assert store.values(BinanceH5LaneState)[0]["day_entry_halted"] is True
    asyncio.run(
        state.record_nav(nav_usdt=Decimal("1000"), now=NOW + dt.timedelta(days=1))
    )
    assert store.values(BinanceH5LaneState)[0]["day_entry_halted"] is False


def _evidence(**changes):
    values = {
        "client_order_id": "h5-one",
        "broker_order_id": "847",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "orig_qty": Decimal("1"),
        "executed_qty": Decimal("0.4"),
        "avg_price": Decimal("100"),
        "status": "PARTIALLY_FILLED",
        "reduce_only": False,
        "position_side": "BOTH",
        "order_created_at": NOW,
        "order_updated_at": NOW,
    }
    values.update(changes)
    return H5OrderEvidence(**values)


def test_partial_fill_and_restart_apply_only_incremental_evidence():
    store = FakeStore()
    store.put(
        BinanceH5Signal,
        signal_values(state="entry_reserved", entry_client_order_id="h5-one"),
    )
    store.put(BinanceH5Intent, intent_values())
    state = H5StateService(store.factory)
    signal, intent = asyncio.run(
        state.apply_order_evidence(
            _evidence(),
            broker_position_amt=Decimal("0.4"),
            exit_bar_close_ts=None,
            now=NOW,
        )
    )
    assert signal.entry_qty == Decimal("0.4") and intent.state == "acknowledged"
    restarted = H5StateService(store.factory)
    for _ in range(2):
        signal, intent = asyncio.run(
            restarted.apply_order_evidence(
                _evidence(status="FILLED", executed_qty=Decimal("1")),
                broker_position_amt=Decimal("1"),
                exit_bar_close_ts=None,
                now=NOW,
            )
        )
    assert signal.entry_qty == Decimal("1")
    assert signal.fees_usdt == Decimal("0.05")
    assert intent.state == "evidenced"  # not released before shared ledger commit
    assert (
        asyncio.run(restarted.settle_intent(intent.client_order_id, now=NOW)).state
        == "settled"
    )


def test_late_recovery_keeps_original_broker_holding_clock():
    store = FakeStore()
    store.put(BinanceH5Signal, signal_values(state="entry_reserved"))
    store.put(BinanceH5Intent, intent_values())
    state = H5StateService(store.factory)
    signal, _ = asyncio.run(
        state.apply_order_evidence(
            _evidence(status="FILLED", executed_qty=Decimal("1")),
            broker_position_amt=Decimal("1"),
            exit_bar_close_ts=None,
            now=NOW + dt.timedelta(days=2),
        )
    )
    assert signal.entered_at == NOW
    assert NOW + dt.timedelta(days=2) - signal.entered_at > dt.timedelta(hours=24)


def test_missing_execution_clock_keeps_intent_unresolved():
    store = FakeStore()
    store.put(BinanceH5Signal, signal_values(state="entry_reserved"))
    store.put(BinanceH5Intent, intent_values())
    with pytest.raises(H5StateBlocked, match="clock evidence"):
        asyncio.run(
            H5StateService(store.factory).apply_order_evidence(
                _evidence(order_created_at=None, order_updated_at=None),
                broker_position_amt=Decimal("0.4"),
                exit_bar_close_ts=None,
                now=NOW,
            )
        )
    assert store.values(BinanceH5Intent)[0]["state"] == "sending"
    assert store.values(BinanceH5Signal)[0]["entry_qty"] == 0


def test_partial_close_pnl_and_remaining_quantity_are_evidence_first():
    store = FakeStore()
    store.put(
        BinanceH5Signal,
        signal_values(
            state="holding",
            entry_qty=Decimal("1"),
            entry_price=Decimal("100"),
            entered_at=NOW,
        ),
    )
    store.put(
        BinanceH5Intent,
        intent_values(
            leg_key="tp1:0", side="SELL", qty=Decimal("0.5"), reduce_only=True
        ),
    )
    state = H5StateService(store.factory)
    ev = _evidence(
        side="SELL",
        orig_qty=Decimal("0.5"),
        executed_qty=Decimal("0.2"),
        avg_price=Decimal("103"),
        reduce_only=True,
    )
    signal, intent = asyncio.run(
        state.apply_order_evidence(
            ev, broker_position_amt=Decimal("0.8"), exit_bar_close_ts=None, now=NOW
        )
    )
    assert signal.remaining_qty == Decimal("0.8")
    assert signal.realized_pnl_usdt == Decimal("0.6")
    assert intent.state == "acknowledged"
    bad = _evidence(
        side="SELL",
        orig_qty=Decimal("0.5"),
        executed_qty=Decimal("0.5"),
        avg_price=Decimal("103"),
        status="FILLED",
        reduce_only=False,
    )
    with pytest.raises(H5StateBlocked, match="echo mismatch"):
        asyncio.run(
            state.apply_order_evidence(
                bad, broker_position_amt=Decimal("0.5"), exit_bar_close_ts=None, now=NOW
            )
        )
    assert store.values(BinanceH5Signal)[0]["closed_qty"] == Decimal("0.2")


def test_foreign_complete_account_exposure_blocks_entry():
    dirty = [
        SimpleNamespace(
            symbol="XRPUSDT",
            position_amt=Decimal("1"),
            position_side="BOTH",
            leverage=1,
        )
    ]
    orders = FuturesDemoOpenOrdersResult(orders=[])
    with pytest.raises(H5StateBlocked, match="foreign"):
        assert_account_exposure(
            signals=(), positions=dirty, open_orders=orders, for_entry=True
        )
    with pytest.raises(H5StateBlocked, match="complete broker"):
        assert_account_exposure(
            signals=(), positions=None, open_orders=orders, for_entry=True
        )
