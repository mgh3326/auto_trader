"""Trend forecast lifecycle: payload, idempotency, executor wiring, fail-open.

Unit tests only — ``forecast_service`` calls, the forecast session and the
state service are faked; no engine, socket, secret or database. Send-count
assertions prove forecast work never causes or repeats a broker order.
"""

from __future__ import annotations

import uuid
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.binance.trend import executor as executor_module
from app.services.brokers.binance.trend import forecast as forecast_module
from app.services.brokers.binance.trend.forecast import (
    ensure_entry_forecast,
    repair_forecast,
    repair_forecasts,
    resolve_held_outcome,
    trend_forecast_id,
)
from app.services.brokers.binance.trend.keys import trend_client_order_id
from tests.services.brokers.binance.trend.fakes import NOW, FakeState
from tests.services.brokers.binance.trend.test_executor import (
    Rig,
    hold,
    trend_bars,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- forecast fakes


class _Session:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _signal(**over) -> SimpleNamespace:
    base = {
        "signal_key": "BTCUSDT|2026-11-14 12:00|BUY|100",
        "correlation_id": "binance-trend:" + "a" * 24,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "state": "holding",
        "entry_client_order_id": "trend-entry-1",
        "entry_qty": D("0.6"),
        "entry_price": D("100"),
        "entered_at": NOW,
        "closed_qty": D("0"),
        "realized_pnl_usdt": D("0"),
        "fees_usdt": D("0"),
        "exit_reason": None,
        "exit_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _forecast_patches(monkeypatch, *, forecast_row=None):
    saved, resolved = AsyncMock(), AsyncMock(return_value={"status": "resolved"})
    getter = AsyncMock(return_value=forecast_row)
    monkeypatch.setattr(forecast_module, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(forecast_module, "get_forecast", getter)
    monkeypatch.setattr(forecast_module, "save_forecast", saved)
    monkeypatch.setattr(forecast_module, "resolve_forecast", resolved)
    return saved, resolved, getter


def _batch_patches(monkeypatch, *, forecast_rows=()):
    """Patch the batch repair path: one session whose SELECT returns rows."""
    saved, resolved = AsyncMock(), AsyncMock(return_value={"status": "resolved"})
    session = _Session()
    session.scalars = AsyncMock(
        return_value=SimpleNamespace(all=lambda: list(forecast_rows))
    )
    monkeypatch.setattr(forecast_module, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(forecast_module, "save_forecast", saved)
    monkeypatch.setattr(forecast_module, "resolve_forecast", resolved)
    return saved, resolved, session


# ------------------------------------------------------- forecast.py contract


@pytest.mark.asyncio
async def test_entry_forecast_payload_and_held_resolution(monkeypatch):
    saved, resolved, getter = _forecast_patches(monkeypatch)
    signal = _signal()
    fid = await ensure_entry_forecast(signal, now=NOW)
    assert fid == str(trend_forecast_id(signal))
    kwargs = saved.call_args.kwargs
    assert kwargs["created_by"] == "trend-tsmom-v1"
    assert kwargs["symbol"] == "USDT-BTC"
    assert kwargs["instrument_type"] == "crypto"
    assert kwargs["forecast_id"] == uuid.UUID(fid)
    target = kwargs["forecast_target"]
    assert target["kind"] == "trend_held_outcome"
    assert target["strategy_id"] == "TREND-TSMOM-v1"
    assert target["side"] == "BUY" and target["signal_key"] == signal.signal_key
    assert target["entry_qty"] == "0.6" and target["entry_price"] == "100"
    assert target["outcome"] == "pending_actual_held_trade"
    assert kwargs["correlation_id"] == signal.correlation_id
    assert kwargs["probability"] == 0.5

    getter.return_value = SimpleNamespace(status="open")
    closed = _signal(
        state="closed",
        closed_qty=D("0.6"),
        realized_pnl_usdt=D("4.9"),
        fees_usdt=D("0.1"),
        exit_reason="time_exit",
        exit_at=NOW,
    )
    result = await resolve_held_outcome(closed, now=NOW)
    assert result["status"] == "resolved"
    rkw = resolved.call_args.kwargs
    assert rkw["forecast_id"] == uuid.UUID(fid)
    assert rkw["persist"] is True
    assert rkw["manual_outcome"] is True  # 4.9 - 0.1 = 4.8 > 0
    assert rkw["manual_observed_value"] == 4.8
    assert rkw["backfill_missing"] is False
    evidence = rkw["manual_evidence"]
    assert evidence["signal_key"] == closed.signal_key
    assert evidence["correlation_id"] == closed.correlation_id
    assert evidence["exit_reason"] == "time_exit"


@pytest.mark.asyncio
async def test_ensure_entry_forecast_is_idempotent(monkeypatch):
    for row_status in ("open", "closed"):
        saved, _, _ = _forecast_patches(
            monkeypatch, forecast_row=SimpleNamespace(status=row_status)
        )
        fid = await ensure_entry_forecast(_signal(), now=NOW)
        assert fid == str(trend_forecast_id(_signal()))
        saved.assert_not_called()  # existing row = save already happened


@pytest.mark.asyncio
async def test_resolve_requires_closed_signal_and_existing_forecast(monkeypatch):
    _, resolved, _ = _forecast_patches(monkeypatch)
    with pytest.raises(ValueError):
        await resolve_held_outcome(_signal(state="holding"), now=NOW)
    monkeypatch.setattr(forecast_module, "get_forecast", AsyncMock(return_value=None))
    with pytest.raises(ValueError):
        await resolve_held_outcome(_signal(state="closed"), now=NOW)
    resolved.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_is_idempotent_when_forecast_already_closed(monkeypatch):
    _, resolved, _ = _forecast_patches(
        monkeypatch,
        forecast_row=SimpleNamespace(status="closed"),
    )
    resolved.return_value = {"status": "already_closed", "changed": False}
    result = await resolve_held_outcome(_signal(state="closed"), now=NOW)
    assert result["status"] == "already_closed"


@pytest.mark.asyncio
async def test_repair_covers_missed_save_and_resolve(monkeypatch):
    saved, resolved, _ = _forecast_patches(monkeypatch)
    # holding + no forecast row -> save only
    assert await repair_forecast(_signal(state="holding"), now=NOW) == "saved"
    saved.assert_called_once()
    resolved.assert_not_called()
    # closed + no forecast row -> save then resolve in the same repair
    saved.reset_mock()
    assert (
        await repair_forecast(
            _signal(
                state="closed",
                closed_qty=D("0.6"),
                realized_pnl_usdt=D("-2"),
                exit_reason="hard_stop",
                exit_at=NOW,
            ),
            now=NOW,
        )
        == "resolved"
    )
    saved.assert_called_once()
    resolved.assert_called_once()
    assert resolved.call_args.kwargs["manual_outcome"] is False


@pytest.mark.asyncio
async def test_repair_skips_unproven_or_irrelevant_signals(monkeypatch):
    saved, resolved, _ = _forecast_patches(monkeypatch)
    for signal in (
        _signal(entry_qty=D("0"), entry_price=None, state="entry_reserved"),
        _signal(state="observed", entry_qty=D("0"), entry_price=None),
        _signal(state="uncertain"),
        _signal(state="blocked", entry_qty=D("0"), entry_price=None),
    ):
        assert await repair_forecast(signal, now=NOW) is None
    saved.assert_not_called()
    resolved.assert_not_called()


@pytest.mark.asyncio
async def test_repair_leaves_an_open_forecast_on_a_held_signal(monkeypatch):
    saved, resolved, _ = _forecast_patches(
        monkeypatch, forecast_row=SimpleNamespace(status="open")
    )
    assert await repair_forecast(_signal(state="holding"), now=NOW) == "open"
    saved.assert_not_called()
    resolved.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_scores_net_of_fees_not_gross(monkeypatch):
    _, resolved, getter = _forecast_patches(monkeypatch)
    getter.return_value = SimpleNamespace(status="open")
    # Gross +0.05 but accrued fees 0.10 -> net -0.05 -> a loss, not a win.
    await resolve_held_outcome(
        _signal(
            state="closed",
            closed_qty=D("0.6"),
            realized_pnl_usdt=D("0.05"),
            fees_usdt=D("0.10"),
            exit_reason="time_exit",
            exit_at=NOW,
        ),
        now=NOW,
    )
    kwargs = resolved.call_args.kwargs
    assert kwargs["manual_outcome"] is False
    assert kwargs["manual_observed_value"] == pytest.approx(-0.05)


@pytest.mark.asyncio
async def test_repair_never_resolves_an_already_closed_forecast(monkeypatch):
    saved, resolved, _ = _forecast_patches(
        monkeypatch,
        forecast_row=SimpleNamespace(status="closed"),
    )
    resolved.return_value = {"status": "already_closed", "changed": False}
    assert (
        await repair_forecast(_signal(state="closed", closed_qty=D("0.6")), now=NOW)
        == "already_closed"
    )
    saved.assert_not_called()
    resolved.assert_not_called()


# ---------------------------------------------------- batched recovery scan


@pytest.mark.asyncio
async def test_repair_forecasts_uses_one_lookup_and_writes_only_misses(
    monkeypatch,
):
    missing_hold = _signal(signal_key="k1", correlation_id="binance-trend:" + "1" * 24)
    missing_closed = _signal(
        signal_key="k2",
        correlation_id="binance-trend:" + "2" * 24,
        state="closed",
        closed_qty=D("0.6"),
        realized_pnl_usdt=D("1"),
        exit_reason="time_exit",
        exit_at=NOW,
    )
    open_hold = _signal(signal_key="k3", correlation_id="binance-trend:" + "3" * 24)
    done_closed = _signal(
        signal_key="k4",
        correlation_id="binance-trend:" + "4" * 24,
        state="closed",
        closed_qty=D("0.6"),
    )
    rows = [
        SimpleNamespace(forecast_id=trend_forecast_id(open_hold), status="open"),
        SimpleNamespace(forecast_id=trend_forecast_id(done_closed), status="closed"),
    ]
    saved, resolved, session = _batch_patches(monkeypatch, forecast_rows=rows)

    results, failures = await repair_forecasts(
        [missing_hold, missing_closed, open_hold, done_closed], now=NOW
    )

    assert failures == []
    # One session, one SELECT — resolved history costs a lookup, not writes.
    assert session.scalars.await_count == 1
    assert saved.await_count == 2  # the two missing forecasts only
    assert resolved.await_count == 1  # the closed missing one, after its save
    assert results == {
        "k1": "saved",
        "k2": "resolved",
        "k3": "open",
        "k4": "already_closed",
    }


@pytest.mark.asyncio
async def test_repair_forecasts_isolates_a_per_signal_failure(monkeypatch):
    good = _signal(signal_key="k1", correlation_id="binance-trend:" + "1" * 24)
    bad = _signal(signal_key="k2", correlation_id="binance-trend:" + "2" * 24)
    _batch_patches(monkeypatch)
    original_save = forecast_module._save

    async def flaky_save(db, signal, fid, *, now):
        if signal.signal_key == "k2":
            raise RuntimeError("forecast store down")
        await original_save(db, signal, fid, now=now)

    monkeypatch.setattr(forecast_module, "_save", flaky_save)

    results, failures = await repair_forecasts([good, bad], now=NOW)

    assert results == {"k1": "saved"}
    assert [s.signal_key for s, _ in failures] == ["k2"]
    assert isinstance(failures[0][1], RuntimeError)


@pytest.mark.asyncio
async def test_repair_forecasts_never_raises_on_a_lookup_failure(monkeypatch):
    signal = _signal()
    _, _, session = _batch_patches(monkeypatch)
    session.scalars = AsyncMock(side_effect=RuntimeError("db down"))

    results, failures = await repair_forecasts([signal], now=NOW)

    assert results == {}
    assert [s.signal_key for s, _ in failures] == [signal.signal_key]


# ------------------------------------------------------- executor wiring


def _record_forecast(monkeypatch):
    ensures, repairs = [], []

    async def ensure(signal, *, now):
        ensures.append((signal.signal_key, signal.state, signal.entry_qty))
        return "fid"

    async def repair(signal, *, now):
        repairs.append((signal.signal_key, signal.state))
        return "open"

    async def repair_batch(signals, *, now):
        repairs.extend((s.signal_key, s.state) for s in signals)
        return {}, []

    monkeypatch.setattr(executor_module, "ensure_entry_forecast", ensure)
    monkeypatch.setattr(executor_module, "repair_forecast", repair)
    monkeypatch.setattr(executor_module, "repair_forecasts", repair_batch)
    return ensures, repairs


@pytest.mark.asyncio
async def test_entry_fill_saves_forecast_only_after_proven_fill(monkeypatch):
    rig = Rig(monkeypatch)
    ensures, repairs = _record_forecast(monkeypatch)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.submit_status = "NEW"
    rig.client.order_script = [{"status": "NEW"}, {"status": "FILLED"}]

    def on_submit(kwargs):
        rig.client.positions["BTCUSDT"] = kwargs["qty"]

    rig.client.on_submit = on_submit
    result = await rig.tick()

    assert len(rig.client.sends) == 1
    signal = next(iter(rig.state.signals.values()))
    assert signal.state == "holding"
    # Saved exactly once, on the broker-proven holding snapshot — the recovery
    # scan ran earlier in the same tick, before the signal existed.
    assert ensures == [(signal.signal_key, "holding", D("0.6"))]
    assert repairs == []
    assert rig.events(result) == [("BTCUSDT", "entry_filled")]


@pytest.mark.asyncio
async def test_unproven_entry_sends_but_saves_no_forecast(monkeypatch):
    rig = Rig(monkeypatch)
    ensures, repairs = _record_forecast(monkeypatch)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.order_script = [{"status": "NEW"}]
    result = await rig.tick()

    assert len(rig.client.sends) == 1  # order sent, still working
    signal = next(iter(rig.state.signals.values()))
    assert signal.state == "entry_reserved"
    assert ensures == [] and repairs == []
    assert rig.events(result) == [("BTCUSDT", "entry_pending")]


@pytest.mark.asyncio
async def test_held_close_repairs_the_forecast_resolution(monkeypatch):
    rig = Rig(monkeypatch)
    ensures, repairs = _record_forecast(monkeypatch)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.auto_fill = True
    await rig.tick()
    assert len(rig.client.sends) == 1 and len(ensures) == 1

    # Flip the signal: the holding is closed reduce-only and a fresh SELL
    # entry follows in the same tick (signal_flip re-entry).
    rig.bars["BTCUSDT"] = trend_bars("SELL")
    rig.minutes["BTCUSDT"] = (
        SimpleNamespace(low=99.0, high=101.0, low_text="99", high_text="101"),
    )
    await rig.tick()

    assert len(rig.client.sends) == 3  # entry + one reduce-only close + re-entry
    closes = [s for s in rig.client.sends if s.get("reduce_only")]
    assert len(closes) == 1
    closed = next(s for s in rig.state.signals.values() if s.state == "closed")
    assert closed.exit_reason == "signal_flip"
    # The close evidence repaired the forecast inline; the recovery scan may
    # call it again — both are idempotent inside repair_forecast.
    assert (closed.signal_key, "closed") in repairs
    assert any(state == "closed" for _, state in repairs)


@pytest.mark.asyncio
async def test_forecast_failure_is_fail_open_and_never_resends(monkeypatch):
    rig = Rig(monkeypatch)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.auto_fill = True

    async def boom(signal, *, now):
        raise RuntimeError("forecast store down")

    async def failing_batch(signals, *, now):
        return {}, [(s, RuntimeError("forecast store down")) for s in signals]

    monkeypatch.setattr(executor_module, "ensure_entry_forecast", boom)
    monkeypatch.setattr(executor_module, "repair_forecast", boom)
    monkeypatch.setattr(executor_module, "repair_forecasts", failing_batch)
    first = await rig.tick()
    # The fill still books; the forecast failure changes nothing observable.
    assert len(rig.client.sends) == 1
    assert rig.events(first) == [("BTCUSDT", "entry_filled")]
    signal = next(iter(rig.state.signals.values()))
    assert signal.state == "holding"

    # A later recovery pass retries the missed save — still no resend.
    second = await rig.tick()
    assert len(rig.client.sends) == 1
    assert ("BTCUSDT", "forecast_recovery_failed", "RuntimeError") in [
        (e.symbol, e.event, e.detail) for e in second.events
    ]


@pytest.mark.asyncio
async def test_recovery_tick_repairs_a_missed_save_without_sending(monkeypatch):
    rig = Rig(monkeypatch)
    repairs = []

    async def repair(signals, *, now):
        repairs.extend((s.signal_key, s.state) for s in signals)
        return {}, []

    monkeypatch.setattr(executor_module, "repair_forecasts", repair)
    key = hold(rig)  # proven holding, calm market -> no exit decision
    # The durable entry intent is settled but its forecast was never saved.
    cid = trend_client_order_id(key, "entry")
    intent = FakeState._new_intent(cid, key, "entry", "BUY", D("0.6"), False)
    intent.state = "settled"
    intent.executed_qty = D("0.6")
    intent.broker_order_id = "1"
    intent.avg_price = D("100")
    rig.state.intents[cid] = intent
    rig.state.signals[key].entry_client_order_id = cid
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.minutes["BTCUSDT"] = (
        SimpleNamespace(low=99.0, high=101.0, low_text="99", high_text="101"),
    )

    result = await rig.tick()

    assert repairs == [(key, "holding")]
    assert rig.client.sends == []  # repair never touches the broker
    assert result.events == ()


@pytest.mark.asyncio
async def test_partial_working_entry_never_reaches_the_forecast(monkeypatch):
    rig = Rig(monkeypatch)
    ensures, repairs = _record_forecast(monkeypatch)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.order_script = [
        {"status": "PARTIALLY_FILLED", "executed": D("0.3")},
    ]

    def on_submit(kwargs):
        if not kwargs["reduce_only"]:
            rig.client.positions["BTCUSDT"] = D("0.3")

    rig.client.on_submit = on_submit
    first = await rig.tick()
    assert len(rig.client.sends) == 1
    assert rig.events(first) == [("BTCUSDT", "entry_pending_partial")]
    assert ensures == [] and repairs == []
    signal = next(iter(rig.state.signals.values()))
    assert signal.state == "entry_reserved"  # no proven fill yet
