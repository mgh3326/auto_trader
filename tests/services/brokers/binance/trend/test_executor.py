"""TrendExecutor on fakes: gates, fence, single send, uncertain, exits, conflicts.

Every refusal path asserts ``len(client.sends) == 0``; every send path asserts
the exact send count. No network, no database, no secret: ``FakeClient`` is a
scripted ``TrendDemoClient`` and ``FakeState``/``FakeLedger`` are in memory.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.binance.trend import executor as executor_module
from app.services.brokers.binance.trend.constants import (
    EXIT_HARD_STOP,
    EXIT_SIGNAL_FLIP,
    EXIT_TIME_EXIT,
    LOCKED_LIMITS,
)
from app.services.brokers.binance.trend.entry_gate import EntryRefusal
from app.services.brokers.binance.trend.executor import (
    TrendExecutor,
    affirmative_rejection,
)
from app.services.brokers.binance.trend.state import TrendStateBlocked
from app.services.brokers.binance.trend.strategy import (
    DAY_MS,
    TrendBar4h,
    TrendStrategy,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS
from tests.services.brokers.binance.trend.fakes import (
    NOW,
    FakeClient,
    FakeLedger,
    FakeSession,
    FakeState,
    open_order,
)

pytestmark = pytest.mark.unit

T = int(NOW.timestamp() * 1000)
STOP = D("0.05")


# --------------------------------------------------------------- scaffolding


def bar(close_ts: int, close: str) -> TrendBar4h:
    price = float(close)
    return TrendBar4h(
        ts=close_ts - FOUR_HOUR_MS,
        close_ts=close_ts,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1.0,
        is_segment_start=True,
        close_text=close,
    )


def trend_bars(
    side: str, *, last: str | None = None, past: str | None = None
) -> tuple[TrendBar4h, ...]:
    """21 daily closes + the L=20 past bar; ``side`` is the momentum sign."""
    now, old = ("101", "100") if side == "BUY" else ("99", "100")
    closes = dict.fromkeys(range(0, 21), "100")
    closes[0] = last or now
    closes[20] = past or old
    return tuple(
        bar(T - k * DAY_MS, c) for k, c in sorted(closes.items(), reverse=True)
    )


class Rig:
    def __init__(self, monkeypatch) -> None:
        monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
        monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
        self.client = FakeClient()
        self.state = FakeState()
        self.ledger = FakeLedger()
        self.sleeps: list[float] = []
        self.bars: dict[str, tuple] = {}
        self.minutes: dict[str, object] = {}
        self.minute_calls = 0
        self.bar_calls = 0

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.exe = TrendExecutor(
            client=self.client,
            strategy=TrendStrategy(lookback_days=20, hard_stop_fraction=STOP),
            state=self.state,  # type: ignore[arg-type]
            demo_ledger=self.ledger,  # type: ignore[arg-type]
            ledger_session=FakeSession(),  # type: ignore[arg-type]
            poll_attempts=5,
            poll_interval_s=0.25,
            sleep=sleep,
        )

        async def fake_signal_bars(transport, symbol, **kwargs):
            self.bar_calls += 1
            return self.bars.get(symbol, ())

        async def fake_minutes(transport, symbol, **kwargs):
            self.minute_calls += 1
            value = self.minutes.get(symbol, ())
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(executor_module, "collect_signal_bars", fake_signal_bars)
        monkeypatch.setattr(executor_module, "collect_hard_stop_minutes", fake_minutes)
        # Forecast writes are durable DB work covered by test_forecast.py;
        # here they are inert so executor tests never touch a session.
        monkeypatch.setattr(executor_module, "ensure_entry_forecast", AsyncMock())
        monkeypatch.setattr(executor_module, "repair_forecast", AsyncMock())

    async def tick(self, *, now=NOW, confirm=True):
        return await self.exe.run_tick(now=now, confirm=confirm)

    def events(self, result) -> list[tuple[str, str]]:
        return [(e.symbol, e.event) for e in result.events]


@pytest.fixture
def rig(monkeypatch) -> Rig:
    return Rig(monkeypatch)


def flat_bars() -> tuple[TrendBar4h, ...]:
    """Lookback return exactly 0: NO_SIGNAL, so an exit is not followed by an entry."""
    return trend_bars("BUY", last="100")


def minute_rows(low: str = "99", high: str = "101"):
    return (
        SimpleNamespace(low=float(low), high=float(high), low_text=low, high_text=high),
    )


def hold(rig: Rig, *, symbol="BTCUSDT", side="BUY", qty="0.6", **kwargs) -> str:
    key = rig.state.seed_holding(symbol=symbol, side=side, qty=qty, **kwargs)
    rig.ledger.rows[rig.state.signals[key].entry_client_order_id] = "filled"
    amount = D(qty) if side == "BUY" else -D(qty)
    rig.client.positions[symbol] = amount
    return key


# ---------------------------------------------------------------- the gates


@pytest.mark.parametrize(
    ("trend", "futures", "confirm"),
    [
        (None, "true", True),
        ("true", None, True),
        ("True", "true", True),
        ("true", "1", True),
        ("false", "true", True),
        ("true", "true", False),
        ("true", "true", None),
        ("true", "true", 1),
    ],
)
@pytest.mark.asyncio
async def test_the_triple_gate_refuses_before_any_network_or_db_use(
    rig: Rig, monkeypatch, trend, futures, confirm
) -> None:
    for name, value in (
        ("BINANCE_TREND_DEMO_ENABLED", trend),
        ("BINANCE_FUTURES_DEMO_ENABLED", futures),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    with pytest.raises(TrendStateBlocked):
        await rig.tick(confirm=confirm)  # type: ignore[arg-type]
    assert rig.client.sends == []
    assert rig.client.position_reads == 0 and rig.client.order_reads == 0
    assert (rig.bar_calls, rig.minute_calls) == (0, 0)
    assert rig.state.calls == {} and rig.state.signals == {}
    assert rig.ledger.calls == []


@pytest.mark.parametrize(
    "origin", ["https://fapi.binance.com", "https://demo-fapi.binance.com/", "x"]
)
@pytest.mark.asyncio
async def test_a_client_off_the_exact_demo_origin_is_refused_before_any_use(
    rig: Rig, origin
) -> None:
    rig.client._base_url = origin
    with pytest.raises(ValueError, match="exact Futures Demo origin"):
        await rig.tick()
    assert rig.client.sends == [] and rig.state.calls == {}


@pytest.mark.asyncio
async def test_a_foreign_client_or_plugin_identity_is_refused(
    rig: Rig, monkeypatch
) -> None:
    rig.exe.client = object()  # type: ignore[assignment]
    with pytest.raises(TrendStateBlocked, match="client identity"):
        await rig.tick()
    rig.exe.client = rig.client
    object.__setattr__(rig.exe.strategy, "strategy_id", "other")
    with pytest.raises(TrendStateBlocked, match="identity"):
        await rig.tick()
    assert rig.client.sends == [] and rig.state.calls == {}


@pytest.mark.asyncio
async def test_a_naive_clock_is_refused(rig: Rig) -> None:
    with pytest.raises(TrendStateBlocked, match="aware"):
        await rig.tick(now=NOW.replace(tzinfo=None))
    assert rig.client.sends == []


# -------------------------------------------------- entry: the round trip


@pytest.mark.asyncio
async def test_entry_round_trip_new_then_filled_by_bounded_poll(rig: Rig) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.submit_status = "NEW"
    rig.client.order_script = [{"status": "NEW"}, {"status": "FILLED"}]
    reads_at_send: list[int] = []

    def on_submit(kwargs):
        reads_at_send.append(rig.client.position_reads)
        rig.client.positions["BTCUSDT"] = kwargs["qty"]

    rig.client.on_submit = on_submit
    result = await rig.tick()

    assert len(rig.client.sends) == 1  # exactly one send
    sent = rig.client.sends[0]
    assert (sent["symbol"], sent["side"], sent["order_type"]) == (
        "BTCUSDT",
        "BUY",
        "MARKET",
    )
    assert sent["reduce_only"] is False and sent["position_side"] == "BOTH"
    assert sent["confirm"] is True and sent["client_order_id"].startswith("trend-")
    assert format(sent["qty"], "f") == "0.6"  # no storage-scale zeros on the wire
    # Gate order: reservation, intent, then the fence; the two account-wide
    # reads both happened before the send.
    assert rig.state.order == ["reserve_entry", "reserve_intent", "mark_sending"]
    assert reads_at_send == [2]
    assert rig.client.order_reads == 2 and rig.sleeps == [0.25]  # bounded poll
    assert rig.events(result) == [("BTCUSDT", "entry_filled")]
    signal = next(iter(rig.state.signals.values()))
    assert (signal.state, signal.entry_qty, signal.entry_price) == (
        "holding",
        D("0.6"),
        D("100"),
    )
    assert signal.entry_notional_usdt == D("60.000000000000")
    intent = rig.state.intents[signal.entry_client_order_id]
    assert intent.state == "settled" and intent.broker_order_id == "42"
    cid = intent.client_order_id
    assert rig.ledger.rows[cid] == "filled"  # evidence-first, ledger service only
    assert [c for c in rig.ledger.calls if c[1] == cid] == [
        ("reserve_root_planned", cid),
        ("record_previewed", cid),
        ("record_validated", cid),
        ("record_submitted", cid),
        ("record_filled", cid),
    ]


@pytest.mark.asyncio
async def test_a_second_tick_with_the_same_signal_sends_nothing(rig: Rig) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.auto_fill = True
    await rig.tick()
    assert len(rig.client.sends) == 1
    again = await rig.tick()
    assert len(rig.client.sends) == 1
    assert rig.events(again) == []
    assert len(rig.state.signals) == 1  # same decision -> same signal key


@pytest.mark.asyncio
async def test_status_new_that_never_fills_stays_pending_and_is_never_resent(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.order_script = [{"status": "NEW"}]
    first = await rig.tick()
    assert rig.client.order_reads == 5 and len(rig.sleeps) == 4  # bounded
    assert rig.events(first) == [("BTCUSDT", "entry_pending")]
    signal = next(iter(rig.state.signals.values()))
    intent = rig.state.intents[signal.entry_client_order_id]
    assert (signal.state, intent.state) == ("entry_reserved", "acknowledged")
    second = await rig.tick()
    assert len(rig.client.sends) == 1  # still pending: nothing new on the symbol
    assert rig.client.order_reads == 6  # one read per restart tick, no poll
    assert ("BTCUSDT", "reconciled_pending") in rig.events(second)


@pytest.mark.asyncio
async def test_a_fill_the_position_read_does_not_confirm_stays_uncertain_then_resolves(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.order_script = [{"status": "FILLED"}]  # position read stays flat
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "entry_uncertain")]
    assert len(rig.client.sends) == 1
    signal = next(iter(rig.state.signals.values()))
    cid = signal.entry_client_order_id
    assert (signal.state, rig.state.intents[cid].state) == ("uncertain", "uncertain")
    # The next tick must not resend and the symbol stays blocked.
    await rig.tick()
    assert len(rig.client.sends) == 1
    assert rig.state.intents[cid].state == "uncertain"
    # Only broker evidence resolves it: order read by id + a position that agrees.
    rig.client.positions["BTCUSDT"] = D("0.6")
    third = await rig.tick()
    assert len(rig.client.sends) == 1
    assert ("BTCUSDT", "reconciled_filled") in rig.events(third)
    assert rig.state.signals[signal.signal_key].state == "holding"
    assert rig.state.intents[cid].state == "settled"


@pytest.mark.parametrize(
    "echo",
    [
        {"client_order_id": "trend-someone-else"},
        {"symbol": "ETHUSDT"},
        {"side": "SELL"},
        {"qty": D("0.5")},
        {"reduce_only": True},
        {"position_side": "LONG"},
        {"position_side": None},
        {"broker_order_id": ""},
        {"status": "WEIRD"},
    ],
)
@pytest.mark.asyncio
async def test_an_echo_mismatch_is_uncertain_and_never_resent(rig: Rig, echo) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.submit_echo = echo
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "entry_uncertain")]
    assert len(rig.client.sends) == 1
    assert rig.client.order_reads == 0  # no evidence is trusted off a bad echo
    assert rig.state.calls["acknowledge"] == 0
    rig.client.order_not_found = True
    rig.client.known_orders = {s["client_order_id"]: s for s in rig.client.sends}
    await rig.tick()
    assert len(rig.client.sends) == 1  # not-found never proves "no order"
    signal = next(iter(rig.state.signals.values()))
    assert signal.state == "uncertain"


class _Boom(Exception):
    pass


class _HttpError(Exception):
    def __init__(self, status: int, body) -> None:
        self.response = SimpleNamespace(status_code=status, json=lambda: body)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("read timeout"),
        _Boom("transport"),
        _HttpError(503, {"code": -1007, "msg": "send status unknown"}),
        _HttpError(500, {"code": -1000, "msg": "unknown"}),
        _HttpError(429, {"code": -1003, "msg": "rate"}),
        _HttpError(418, {"code": -1003, "msg": "banned"}),
        _HttpError(400, ["not", "an", "object"]),
        _HttpError(400, {"msg": "no code"}),
        _HttpError(400, {"code": 5}),
    ],
)
@pytest.mark.asyncio
async def test_any_error_after_the_fence_is_uncertain_and_never_resent(
    rig: Rig, error
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.submit_error = error
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "entry_uncertain")]
    assert len(rig.client.sends) == 1
    signal = next(iter(rig.state.signals.values()))
    intent = rig.state.intents[signal.entry_client_order_id]
    assert (signal.state, intent.state) == ("uncertain", "uncertain")
    rig.client.submit_error = None
    for _ in range(3):
        await rig.tick()
    assert len(rig.client.sends) == 1


@pytest.mark.asyncio
async def test_an_uncertain_symbol_blocks_only_itself(rig: Rig) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.bars["ETHUSDT"] = trend_bars("BUY")
    rig.client.submit_error = TimeoutError("x")
    await rig.tick()  # BTC goes uncertain; the loop stops sending new entries
    assert [s["symbol"] for s in rig.client.sends] == ["BTCUSDT"]
    rig.client.submit_error = None
    rig.client.auto_fill = True
    rig.client.order_not_found = True
    rig.client.known_orders = {s["client_order_id"]: s for s in rig.client.sends}
    await rig.tick()
    symbols = [s["symbol"] for s in rig.client.sends]
    assert symbols == ["BTCUSDT", "ETHUSDT"]  # BTC never resent; ETH proceeds


@pytest.mark.parametrize(
    ("error", "conflicting"),
    [
        (_HttpError(400, {"code": -2019, "msg": "margin"}), False),
        (_HttpError(400, {"code": -4061, "msg": "side"}), True),
    ],
)
@pytest.mark.asyncio
async def test_an_affirmative_rejection_is_recorded_not_uncertain(
    rig: Rig, error, conflicting
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.submit_error = error
    if conflicting:
        # Another strategy's open order appears on the symbol during the send.
        rig.client.on_submit = lambda kwargs: rig.client.orders.append(
            open_order("BTCUSDT")
        )
    result = await rig.tick()
    assert len(rig.client.sends) == 1
    signal = next(iter(rig.state.signals.values()))
    intent = rig.state.intents[signal.entry_client_order_id]
    assert intent.state == "rejected" and intent.broker_status.startswith("http_400:")
    assert signal.state == ("conflict_rejected" if conflicting else "blocked")
    assert rig.state.calls["mark_uncertain"] == 0
    assert rig.ledger.rows[intent.client_order_id] == "reconciled"  # no order existed
    expected = "entry_conflict_rejected" if conflicting else "entry_rejected"
    assert rig.events(result) == [("BTCUSDT", expected)]


@pytest.mark.asyncio
async def test_three_conflicts_in_a_week_stop_new_entries(rig: Rig) -> None:
    for i in range(3):
        key = rig.state.seed_holding(
            symbol="ETHUSDT", key=f"c{i}", state="conflict_blocked"
        )
        rig.state.signals[key].conflict_at = NOW - dt.timedelta(hours=i + 1)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    result = await rig.tick()
    assert rig.client.sends == []
    event = result.events[0].event
    assert EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED in event
    assert (
        rig.state.signals[
            next(k for k, v in rig.state.signals.items() if v.symbol == "BTCUSDT")
        ].state
        == "blocked"
    )


# -------------------------------------- refusals send nothing (zero sends)


async def _only_btc(rig: Rig):
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    result = await rig.tick()
    assert rig.client.sends == [], "a refusal path must not send"
    return result


@pytest.mark.asyncio
async def test_hedge_mode_refuses_the_entry(rig: Rig) -> None:
    rig.client.hedge = True
    result = await _only_btc(rig)
    assert result.events[0].event.startswith("entry_entry_refused")
    assert rig.state.calls["reserve_entry"] == 0


@pytest.mark.asyncio
async def test_isolated_1x_readback_failure_refuses_without_mutating_settings(
    rig: Rig,
) -> None:
    rig.client.isolated_ok = False
    result = await _only_btc(rig)
    assert result.events[0].event.startswith("entry_entry_aborted")
    assert rig.state.calls["reserve_entry"] == 0


@pytest.mark.asyncio
async def test_sizing_that_cannot_meet_the_exchange_minimum_refuses(rig: Rig) -> None:
    rig.client.filters = executor_module.TrendFilters(
        D("0.001"), D("61"), D("0.001"), D("1000"), 3
    )
    result = await _only_btc(rig)
    assert result.events[0].event.startswith("entry_entry_refused")
    assert rig.state.calls["reserve_entry"] == 0


@pytest.mark.asyncio
async def test_the_state_entry_gate_refuses_after_two_hard_stops_today(
    rig: Rig,
) -> None:
    for i in range(2):
        rig.state.seed_holding(
            symbol="ETHUSDT",
            key=f"h{i}",
            state="closed",
            exit_reason=EXIT_HARD_STOP,
            exit_at=NOW - dt.timedelta(hours=2 - i),
        )
    result = await _only_btc(rig)
    assert EntryRefusal.CONSECUTIVE_HARD_STOP_LIMIT_REACHED in result.events[0].event
    assert rig.ledger.calls == []


@pytest.mark.asyncio
async def test_gate_one_foreign_position_is_conflict_blocked_before_the_ledger(
    rig: Rig,
) -> None:
    rig.client.positions["BTCUSDT"] = D("-2")  # ROB-1474 short on the symbol
    result = await _only_btc(rig)
    assert rig.events(result) == [("BTCUSDT", "entry_conflict_blocked")]
    signal = next(iter(rig.state.signals.values()))
    assert signal.state == "conflict_blocked" and signal.conflict_at is not None
    assert "foreign_position" in signal.conflict_detail
    assert rig.ledger.calls == [] and rig.client.position_reads == 1


@pytest.mark.asyncio
async def test_gate_one_foreign_open_order_is_conflict_blocked(rig: Rig) -> None:
    rig.client.orders = [open_order("BTCUSDT")]
    result = await _only_btc(rig)
    assert rig.events(result) == [("BTCUSDT", "entry_conflict_blocked")]


@pytest.mark.asyncio
async def test_gate_two_runs_again_right_before_the_fence(rig: Rig) -> None:
    # Clear at gate #1 (first position read), foreign by gate #2 (second read).
    rig.client.foreign_after_reads = 2
    result = await _only_btc(rig)
    assert rig.events(result) == [("BTCUSDT", "entry_conflict_blocked")]
    assert rig.client.position_reads == 2  # the account was read exactly twice
    signal = next(iter(rig.state.signals.values()))
    assert signal.state == "conflict_blocked"
    cid = next(iter(rig.state.intents))
    assert rig.state.intents[cid].state == "rejected"  # reservation released
    assert rig.state.calls["mark_sending"] == 0  # never fenced
    assert rig.ledger.rows[cid] == "reconciled"  # the planned root was released


@pytest.mark.asyncio
async def test_a_fresh_price_above_the_reservation_refuses_the_send(rig: Rig) -> None:
    rig.client.price_script = [D("100"), D("101")]
    result = await _only_btc(rig)
    assert "price_moved_beyond_reservation" in result.events[0].event
    assert rig.state.calls["mark_sending"] == 0
    assert next(iter(rig.state.signals.values())).state == "blocked"


@pytest.mark.asyncio
async def test_a_refused_ledger_reservation_aborts_before_the_send(rig: Rig) -> None:
    rig.ledger.root_status = "cap_reached"
    result = await _only_btc(rig)
    assert result.events[0].event.startswith("entry_entry_aborted")
    assert rig.state.calls["mark_sending"] == 0
    assert next(iter(rig.state.signals.values())).state == "blocked"


@pytest.mark.asyncio
async def test_the_fence_rejudges_the_entry_rules_and_a_refusal_sends_nothing(
    rig: Rig,
) -> None:
    original = rig.state.mark_sending

    async def tripped(cid, *, now):
        for i in range(2):  # two hard stops land between reservation and fence
            rig.state.seed_holding(
                symbol="ETHUSDT",
                key=f"late{i}",
                state="closed",
                exit_reason=EXIT_HARD_STOP,
                exit_at=NOW - dt.timedelta(minutes=10 - i),
            )
        return await original(cid, now=now)

    rig.state.mark_sending = tripped  # type: ignore[method-assign]
    result = await _only_btc(rig)
    assert rig.events(result) == [("BTCUSDT", "entry_refused_at_fence")]
    cid = next(iter(rig.state.intents))
    assert rig.state.intents[cid].state == "rejected"
    assert rig.ledger.rows[cid] == "reconciled"


@pytest.mark.asyncio
async def test_unknown_bars_are_no_signal_and_nothing_is_even_observed(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")[:-1]  # decision bar missing
    result = await rig.tick()
    assert rig.client.sends == [] and rig.state.signals == {}
    assert result.events == ()


# ------------------------------------------------- restart / unresolved


@pytest.mark.asyncio
async def test_a_reserved_but_never_fenced_entry_is_released_on_restart(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.price_script = [D("100"), D("100"), D("101")]
    original = rig.state.mark_sending

    async def crash(cid, *, now):
        raise RuntimeError("process died before the fence committed")

    rig.state.mark_sending = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await rig.tick()
    rig.state.mark_sending = original  # type: ignore[method-assign]
    assert rig.client.sends == []
    cid = next(iter(rig.state.intents))
    assert rig.state.intents[cid].state == "reserved"
    restart = await rig.tick()
    assert rig.client.sends == []  # a reservation without a fence is not a send
    assert ("BTCUSDT", "unsent_released") in rig.events(restart)
    assert rig.state.intents[cid].state == "rejected"
    assert rig.ledger.rows[cid] == "reconciled"


@pytest.mark.asyncio
async def test_a_fenced_intent_found_after_a_crash_is_resolved_by_evidence_only(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.price_script = [D("100")]
    orig_submit = rig.client.submit_order

    async def die(**kwargs):
        rig.client.sends.append(kwargs)
        raise KeyboardInterrupt  # the process dies mid-send

    rig.client.submit_order = die  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await rig.tick()
    rig.client.submit_order = orig_submit  # type: ignore[method-assign]
    assert len(rig.client.sends) == 1
    cid = next(iter(rig.state.intents))
    assert rig.state.intents[cid].state == "sending"
    # Broker never saw it: not-found is not proof, so it stays uncertain.
    rig.client.order_not_found = True
    rig.client.known_orders = {cid: rig.client.sends[0]}
    await rig.tick()
    assert rig.state.intents[cid].state == "uncertain"
    # Broker did fill it: evidence + a confirming position resolves it.
    rig.client.order_not_found = False
    rig.client.order_script = [{"status": "FILLED"}]
    rig.client.positions["BTCUSDT"] = rig.client.sends[0]["qty"]
    await rig.tick()
    assert len(rig.client.sends) == 1
    assert rig.state.intents[cid].state == "settled"
    assert next(iter(rig.state.signals.values())).state == "holding"


@pytest.mark.asyncio
async def test_an_evidenced_intent_whose_ledger_sync_failed_is_replayed(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.auto_fill = True
    real = rig.ledger.record_filled

    async def broken(**kwargs):
        raise RuntimeError("ledger unavailable")

    rig.ledger.record_filled = broken  # type: ignore[method-assign]
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "entry_ledger_incomplete")]
    cid = next(iter(rig.state.intents))
    assert rig.state.intents[cid].state == "evidenced"
    assert next(iter(rig.state.signals.values())).state == "holding"
    rig.ledger.record_filled = real  # type: ignore[method-assign]
    second = await rig.tick()
    assert len(rig.client.sends) == 1  # replay never resends
    assert rig.state.intents[cid].state == "settled"
    assert rig.ledger.rows[cid] == "filled"
    assert ("BTCUSDT", "reconciled_filled") in rig.events(second)


# ------------------------------------------------------------------ exits


@pytest.mark.asyncio
async def test_hard_stop_on_a_1m_extreme_sends_one_reduce_only_close(rig: Rig) -> None:
    key = hold(rig)
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    rig.client.auto_fill = True
    result = await rig.tick()
    assert len(rig.client.sends) == 1
    sent = rig.client.sends[0]
    assert (sent["side"], sent["reduce_only"], sent["position_side"]) == (
        "SELL",
        True,
        "BOTH",
    )
    assert sent["qty"] == D("0.6") and sent["order_type"] == "MARKET"
    assert rig.events(result) == [("BTCUSDT", "exit_closed")]
    signal = rig.state.signals[key]
    assert (signal.state, signal.exit_reason, signal.closed_qty) == (
        "closed",
        EXIT_HARD_STOP,
        D("0.6"),
    )
    cid = sent["client_order_id"]
    assert rig.state.intents[cid].state == "settled"
    # Exit child closed+reconciled; the root is closed only on flat evidence.
    assert rig.ledger.rows[cid] == "reconciled"
    assert rig.ledger.rows[signal.entry_client_order_id] == "reconciled"


@pytest.mark.asyncio
async def test_exit_order_hard_stop_then_signal_flip_then_time_exit(rig: Rig) -> None:
    old = NOW - dt.timedelta(days=31)
    hold(rig, entered_at=old)
    rig.bars["BTCUSDT"] = trend_bars("SELL")  # flip AND time exit are both due
    rig.minutes["BTCUSDT"] = minute_rows(low="94")  # and the stop is touched
    rig.client.auto_fill = True
    await rig.tick()
    assert rig.state.intents[rig.client.sends[0]["client_order_id"]].leg_key == (
        f"{EXIT_HARD_STOP}:0"
    )


@pytest.mark.asyncio
async def test_signal_flip_closes_then_reenters_the_new_side_in_the_same_tick(
    rig: Rig,
) -> None:
    hold(rig)
    rig.bars["BTCUSDT"] = trend_bars("SELL")
    rig.minutes["BTCUSDT"] = minute_rows()
    rig.client.auto_fill = True
    result = await rig.tick()
    assert [(s["side"], s["reduce_only"]) for s in rig.client.sends] == [
        ("SELL", True),  # reduceOnly close first
        ("SELL", False),  # then the new short entry
    ]
    assert rig.events(result) == [
        ("BTCUSDT", "exit_closed"),
        ("BTCUSDT", "entry_filled"),
    ]
    closed = next(s for s in rig.state.signals.values() if s.state == "closed")
    assert closed.exit_reason == EXIT_SIGNAL_FLIP
    held = [s for s in rig.state.signals.values() if s.state == "holding"]
    assert [(s.side, s.entry_qty) for s in held] == [("SELL", D("0.603"))]


@pytest.mark.asyncio
async def test_time_exit_after_thirty_days(rig: Rig) -> None:
    key = hold(rig, entered_at=NOW - dt.timedelta(days=30, minutes=1))
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows()
    rig.client.auto_fill = True
    await rig.tick()
    assert rig.state.signals[key].exit_reason == EXIT_TIME_EXIT
    assert len(rig.client.sends) == 1


@pytest.mark.asyncio
async def test_a_calm_aligned_holding_sends_nothing(rig: Rig) -> None:
    hold(rig)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.minutes["BTCUSDT"] = minute_rows()
    result = await rig.tick()
    assert rig.client.sends == [] and result.events == ()


@pytest.mark.asyncio
async def test_a_failed_1m_fetch_falls_back_to_the_4h_decision_only(rig: Rig) -> None:
    hold(rig)
    rig.bars["BTCUSDT"] = trend_bars("BUY", last="100")
    rig.minutes["BTCUSDT"] = RuntimeError("klines down")
    rig.client.price = D("90")  # a stopped-out quote is ignored in fallback mode
    result = await rig.tick()
    assert rig.minute_calls == 1
    assert rig.client.sends == [] and result.events == ()


@pytest.mark.asyncio
async def test_a_failed_1m_fetch_still_exits_on_a_stopped_out_4h_close(
    rig: Rig,
) -> None:
    key = hold(rig)
    rig.bars["BTCUSDT"] = trend_bars("BUY", last="94")
    rig.minutes["BTCUSDT"] = RuntimeError("klines down")
    rig.client.auto_fill = True
    await rig.tick()
    assert rig.state.signals[key].exit_reason == EXIT_HARD_STOP


@pytest.mark.asyncio
async def test_an_old_bar_before_the_fill_is_not_a_4h_decision(rig: Rig) -> None:
    hold(rig, entered_at=NOW + dt.timedelta(hours=1))  # filled after the bar closed
    rig.bars["BTCUSDT"] = trend_bars("BUY", last="94", past="90")
    rig.minutes["BTCUSDT"] = RuntimeError("klines down")
    await rig.tick()
    assert rig.client.sends == []


@pytest.mark.asyncio
async def test_an_exit_is_refused_when_the_broker_differs_from_the_lane_remainder(
    rig: Rig,
) -> None:
    key = hold(rig, qty="0.6")
    rig.client.positions["BTCUSDT"] = D("1.0")  # a manual addition on top of ours
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    rig.client.auto_fill = True
    result = await rig.tick()
    assert rig.client.sends == []  # the state could not reconcile the result
    assert rig.events(result) == [("BTCUSDT", "exposure_mismatch")]
    assert rig.state.signals[key].state == "holding"


@pytest.mark.parametrize("setup", ["flat", "wrong_side", "open_order", "not_1x"])
@pytest.mark.asyncio
async def test_an_exit_the_account_cannot_support_sends_nothing(
    rig: Rig, setup
) -> None:
    hold(rig)
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    if setup == "flat":
        rig.client.positions["BTCUSDT"] = D(0)
    elif setup == "wrong_side":
        rig.client.positions["BTCUSDT"] = D("-0.6")
    elif setup == "open_order":
        rig.client.orders = [open_order("BTCUSDT")]
    else:
        original = rig.client.get_all_positions

        async def leveraged():
            rows = await original()
            return [
                row.__class__(**{**row.__dict__, "leverage": 3})
                if row.symbol == "BTCUSDT"
                else row
                for row in rows
            ]

        rig.client.get_all_positions = leveraged  # type: ignore[method-assign]
    result = await rig.tick()
    assert rig.client.sends == []
    assert rig.events(result) == [("BTCUSDT", "exposure_mismatch")]


@pytest.mark.asyncio
async def test_a_rejected_exit_keeps_the_position_and_retries_with_a_new_leg(
    rig: Rig,
) -> None:
    key = hold(rig)
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    rig.client.submit_error = _HttpError(400, {"code": -2022, "msg": "reduceOnly"})
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "exit_rejected")]
    assert rig.state.signals[key].state == "holding"
    rig.client.submit_error = None
    rig.client.auto_fill = True
    await rig.tick()
    ids = [s["client_order_id"] for s in rig.client.sends]
    assert len(ids) == 2 and len(set(ids)) == 2  # the rejected key is never reused
    assert rig.state.signals[key].state == "closed"


@pytest.mark.asyncio
async def test_an_uncertain_exit_is_never_resent(rig: Rig) -> None:
    key = hold(rig)
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    rig.client.submit_error = TimeoutError("exit timeout")
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "exit_uncertain")]
    assert rig.state.signals[key].state == "uncertain"
    rig.client.submit_error = None
    for _ in range(3):
        await rig.tick()
    assert len(rig.client.sends) == 1


@pytest.mark.asyncio
async def test_a_partial_exit_fill_keeps_the_remainder_held(rig: Rig) -> None:
    key = hold(rig, qty="0.6")
    rig.client.filters = executor_module.TrendFilters(
        D("0.5"), D("5"), D("0.5"), D("1000"), 1
    )
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    rig.client.auto_fill = True
    await rig.tick()
    signal = rig.state.signals[key]
    assert (signal.state, signal.closed_qty) == ("holding", D("0.5"))
    assert rig.client.positions["BTCUSDT"] == D("0.1")
    # The decision (and its day) is recorded when it is taken, so the daily kill
    # switch can count the stop although the position is still partly open.
    assert signal.exit_reason == EXIT_HARD_STOP
    assert signal.exit_at == NOW


@pytest.mark.asyncio
async def test_an_unknown_exit_reason_is_refused_before_any_write(rig: Rig) -> None:
    key = hold(rig)
    signal = await rig.state.get_signal(key)
    with pytest.raises(TrendStateBlocked, match="exit reason"):
        await rig.exe._exit(signal, reason="tp1", qty=D("0.1"), now=NOW)
    assert rig.state.intents == {} and rig.client.sends == []
    assert rig.ledger.calls == []


# ------------------------------------------- no settings mutation / identity


@pytest.mark.asyncio
async def test_the_client_cannot_mutate_settings_or_use_the_smoke_order_test() -> None:
    client = FakeClient()
    for call in (
        client.set_leverage(symbol="BTCUSDT", leverage=1),
        client.set_margin_type(symbol="BTCUSDT", margin_type="ISOLATED"),
        client.order_test(symbol="BTCUSDT"),
    ):
        with pytest.raises(ValueError):
            await call


@pytest.mark.asyncio
async def test_the_trend_client_refuses_a_cancel_without_its_gates(
    monkeypatch,
) -> None:
    """A cancel carries the same gates as a submit: both env flags and confirm."""
    client = FakeClient()

    async def call(**kwargs):
        return await executor_module.TrendDemoClient.cancel_order(client, **kwargs)

    ok = {"symbol": "BTCUSDT", "client_order_id": "trend-x", "confirm": True}
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    for changed in (
        {"confirm": False},
        {"client_order_id": "foreign-1"},
        {"symbol": "DOGEUSDT"},
    ):
        with pytest.raises(ValueError):
            await call(**{**ok, **changed})
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "True")
    with pytest.raises(ValueError):
        await call(**ok)
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_ENABLED", raising=False)
    with pytest.raises(ValueError):
        await call(**ok)


def test_only_the_cancel_serialization_step_calls_cancel_order() -> None:
    import ast
    from pathlib import Path

    package = Path(executor_module.__file__).parent
    banned = {"set_leverage", "set_margin_type", "order_test"}
    cancels: list[tuple[str, int]] = []
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in banned, (
                    f"{path.name} calls {node.func.attr}"
                )
                if node.func.attr == "cancel_order":
                    cancels.append((path.name, node.lineno))
    # The gated client method (``super().cancel_order``) and its one caller.
    assert [name for name, _ in cancels] == ["executor.py", "executor.py"]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"code": -2019}, "http_400:-2019"),
        (401, {"code": -2015}, "http_401:-2015"),
        (400, {"code": 0}, None),
        (400, {"code": "x"}, None),
        (400, {"code": True}, None),
        (408, {"code": -1001}, None),
        (418, {"code": -1003}, None),
        (429, {"code": -1003}, None),
        (500, {"code": -1000}, None),
        (503, {"code": -1007}, None),
        (200, {"code": -1}, None),
    ],
)
def test_only_a_4xx_with_a_negative_code_proves_no_order_exists(
    status, body, expected
) -> None:
    assert affirmative_rejection(_HttpError(status, body)) == expected


def test_the_broker_rejection_helper_is_safe_on_unreadable_responses() -> None:
    assert affirmative_rejection(TimeoutError()) is None

    class Unreadable:
        status_code = 400

        def json(self):
            raise ValueError("not json")

    error = Exception()
    error.response = Unreadable()  # type: ignore[attr-defined]
    assert affirmative_rejection(error) is None


def test_the_executor_uses_only_the_locked_universe_constants() -> None:
    assert LOCKED_LIMITS.universe == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")


# ----------------------------------------- ownership, orphans, partial fills


@pytest.mark.asyncio
async def test_a_second_tick_cannot_run_while_one_owns_the_lane(rig: Rig) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.state.lease_held = True  # another tick is between reservation and fence
    with pytest.raises(TrendStateBlocked, match="owns the lane"):
        await rig.tick()
    assert rig.client.sends == [] and rig.client.position_reads == 0
    assert rig.state.calls == {} and rig.ledger.calls == []
    # the lease is held for the whole tick and released afterwards
    rig.state.lease_held = False
    await rig.tick()
    assert rig.state.lease_entries == 1 and rig.state.lease_held is False


@pytest.mark.asyncio
async def test_the_lease_is_released_when_a_tick_dies(rig: Rig) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")

    async def die(**kwargs):
        raise KeyboardInterrupt

    rig.state.reserve_entry = die  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await rig.tick()
    assert rig.state.lease_held is False


@pytest.mark.asyncio
async def test_a_reservation_orphaned_before_its_intent_is_released_on_restart(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")

    async def crash(**kwargs):
        raise KeyboardInterrupt  # dies after the root commit, before reserve_intent

    original = rig.state.reserve_intent
    rig.state.reserve_intent = crash  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await rig.tick()
    rig.state.reserve_intent = original  # type: ignore[method-assign]
    signal = next(iter(rig.state.signals.values()))
    cid = executor_module.trend_client_order_id(signal.signal_key, "entry")
    assert signal.state == "entry_reserved" and rig.state.intents == {}
    assert rig.ledger.rows[cid] == "validated"  # the stranded root
    restart = await rig.tick()
    assert rig.client.sends == []
    assert ("BTCUSDT", "orphan_reservation_released") in rig.events(restart)
    assert signal.state == "blocked"  # slot and symbol are free again
    assert rig.ledger.rows[cid] == "reconciled"  # root no longer consumes a slot
    assert await rig.state.list_active_signals() == ()


@pytest.mark.asyncio
async def test_a_sent_entry_is_never_treated_as_an_orphan(rig: Rig) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.order_script = [{"status": "NEW"}]
    await rig.tick()  # pending: entry_reserved with an acknowledged intent
    again = await rig.tick()
    assert "orphan_reservation_released" not in [e for _, e in rig.events(again)]
    assert len(rig.client.sends) == 1
    assert next(iter(rig.state.signals.values())).state == "entry_reserved"


@pytest.mark.asyncio
async def test_a_partial_terminal_entry_fill_stays_on_a_closable_ledger_lifecycle(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.order_script = [{"status": "CANCELED", "executed": D("0.3")}]
    rig.client.on_submit = lambda kwargs: rig.client.positions.update(
        {"BTCUSDT": D("0.3")}
    )
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "entry_filled")]
    signal = next(iter(rig.state.signals.values()))
    assert (signal.state, signal.entry_qty) == ("holding", D("0.3"))
    root = signal.entry_client_order_id
    assert rig.ledger.rows[root] == "filled"  # never "anomaly"
    assert ("record_anomaly", root) not in rig.ledger.calls
    # the remainder can still be closed and the root released
    rig.client.order_script = []
    rig.client.auto_fill = True
    rig.client.on_submit = None
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows(low="60")
    await rig.tick()
    assert signal.state == "closed"
    assert rig.ledger.rows[root] == "reconciled"
    assert rig.state.intents[rig.client.sends[-1]["client_order_id"]].state == "settled"


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["entry", "exit"])
async def test_a_failed_ledger_release_after_a_rejection_is_retried_by_recovery(
    rig: Rig, side
) -> None:
    broken = {"on": True}
    real = rig.ledger.record_cancelled

    async def flaky(**kwargs):
        if broken["on"]:
            raise RuntimeError("ledger unavailable")
        await real(**kwargs)

    rig.ledger.record_cancelled = flaky  # type: ignore[method-assign]
    rig.client.submit_error = _HttpError(400, {"code": -2019, "msg": "x"})
    if side == "entry":
        rig.bars["BTCUSDT"] = trend_bars("BUY")
    else:
        hold(rig)
        rig.bars["BTCUSDT"] = flat_bars()
        rig.minutes["BTCUSDT"] = minute_rows(low="94")
    await rig.tick()
    rejected = [i for i in rig.state.intents.values() if i.state == "rejected"]
    assert len(rejected) == 1 and len(rig.client.sends) == 1
    assert rig.ledger.rows[rejected[0].client_order_id] == "validated"  # stranded
    broken["on"] = False
    rig.client.submit_error = None
    await rig.tick()  # recovery sweeps the pre-send row of the rejected order
    assert rig.ledger.rows[rejected[0].client_order_id] == "reconciled"


@pytest.mark.asyncio
async def test_recovery_claims_the_state_before_it_touches_the_ledger(rig: Rig) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    original = rig.state.mark_sending

    async def crash(cid, *, now):
        raise RuntimeError("died before the fence")

    rig.state.mark_sending = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await rig.tick()
    rig.state.mark_sending = original  # type: ignore[method-assign]
    cid = next(iter(rig.state.intents))
    seen = []
    real = rig.ledger.record_cancelled

    async def spy(**kwargs):
        seen.append(rig.state.intents[kwargs["client_order_id"]].state)
        await real(**kwargs)

    rig.ledger.record_cancelled = spy  # type: ignore[method-assign]
    await rig.tick()
    # the intent was already released (fence impossible) when the row was cancelled
    assert seen == ["rejected"]
    assert rig.ledger.rows[cid] == "reconciled"


@pytest.mark.asyncio
async def test_a_sender_that_fenced_first_wins_the_claim_and_nothing_is_released(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    original = rig.state.mark_sending

    async def crash(cid, *, now):
        raise RuntimeError("died before the fence")

    rig.state.mark_sending = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await rig.tick()
    rig.state.mark_sending = original  # type: ignore[method-assign]
    cid = next(iter(rig.state.intents))
    real_list = rig.state.list_unresolved_intents

    async def stale_snapshot():
        snapshot = await real_list()
        # the other tick fences between our read and our claim
        rig.state.intents[cid].state = "sending"
        return snapshot

    rig.state.list_unresolved_intents = stale_snapshot  # type: ignore[method-assign]
    result = await rig.tick()
    assert ("BTCUSDT", "unsent_released") not in rig.events(result)
    assert rig.ledger.rows[cid] == "validated"  # a possibly sent order's root stays
    assert rig.client.sends == []


@pytest.mark.asyncio
async def test_a_lost_lease_stops_the_tick_before_the_fence(rig: Rig) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.state.lease_lost = True
    with pytest.raises(Exception, match="lease lost"):
        await rig.tick()
    assert rig.client.sends == [] and rig.state.calls["mark_sending"] == 0
    assert rig.state.lease_checks >= 1 and rig.state.lease_held is False


# ------------------- review round 3: quote outage, partial entries, ledger


@pytest.mark.asyncio
async def test_a_book_quote_outage_does_not_stop_an_exit_the_candles_prove(
    rig: Rig,
) -> None:
    key = hold(rig)
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    rig.client.book_down = True
    rig.client.auto_fill = True
    await rig.tick()
    assert len(rig.client.sends) == 1 and rig.client.sends[0]["reduce_only"] is True
    assert rig.state.signals[key].exit_reason == EXIT_HARD_STOP


@pytest.mark.asyncio
async def test_a_book_quote_outage_still_exits_a_flip_and_an_overdue_holding(
    rig: Rig,
) -> None:
    hold(rig, entered_at=NOW - dt.timedelta(days=31))
    rig.bars["BTCUSDT"] = flat_bars()
    rig.minutes["BTCUSDT"] = minute_rows()
    rig.client.book_down = True
    rig.client.auto_fill = True
    await rig.tick()
    assert [s["reduce_only"] for s in rig.client.sends] == [True]


@pytest.mark.asyncio
async def test_an_entry_is_exposed_from_its_reservation_not_its_last_order_update(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.auto_fill = True  # the fake's order updateTime is NOW
    await rig.tick()
    signal = next(iter(rig.state.signals.values()))
    reserved_at = rig.state.intents[signal.entry_client_order_id].created_at
    assert reserved_at == NOW - dt.timedelta(minutes=10)
    assert signal.entered_at == reserved_at  # not the later updateTime


@pytest.mark.asyncio
async def test_a_ledger_error_rolls_the_shared_session_back(rig: Rig) -> None:
    FakeSession.rollbacks = 0
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.ledger.root_status = "cap_reached"
    await rig.tick()
    assert FakeSession.rollbacks >= 1  # the aborted ledger transaction is cleared


@pytest.mark.asyncio
async def test_the_release_sweep_has_no_age_window(rig: Rig) -> None:
    # 300 newer terminal records must not hide an older stranded root.
    for i in range(300):
        rig.state.seed_holding(symbol="ETHUSDT", key=f"b{i}", state="blocked")
    stranded = rig.state.seed_holding(symbol="SOLUSDT", key="old", state="blocked")
    cid = executor_module.trend_client_order_id(stranded, "entry")
    rig.ledger.rows[cid] = "validated"
    await rig.tick()
    assert rig.ledger.rows[cid] == "reconciled"


# -------- managed holding of a partially filled entry (serialization, hk 4752)


def _partial_rig(
    rig: Rig,
    *,
    executed: str = "0.3",
    avg: str | None = None,
    side: str = "BUY",
) -> None:
    rig.bars["BTCUSDT"] = trend_bars(side)
    script: dict = {"status": "PARTIALLY_FILLED", "executed": D(executed)}
    if avg is not None:
        script["avg"] = D(avg)
    rig.client.order_script = [script]

    def fill(kwargs):
        if kwargs["reduce_only"]:
            return  # the exit's effect comes from auto_fill / exit_script
        amount = D(executed) if side == "BUY" else -D(executed)
        rig.client.positions["BTCUSDT"] = amount

    rig.client.on_submit = fill


def _terminal_entry(
    rig: Rig,
    *,
    executed: str,
    avg: str,
    status: str = "CANCELED",
    side: str = "BUY",
):
    """The entry order is terminal with its final executed quantity."""
    rig.client.order_script = [
        {"status": status, "executed": D(executed), "avg": D(avg)}
    ]
    amount = D(executed) if side == "BUY" else -D(executed)
    rig.client.positions["BTCUSDT"] = amount


def _ready_exit(rig: Rig, *, price: str = "110") -> None:
    rig.client.auto_fill = True
    rig.client.exit_script = {"status": "FILLED", "avg": D(price)}


def _entry_cid(rig: Rig) -> str:
    return next(iter(rig.state.signals.values())).entry_client_order_id


@pytest.mark.asyncio
async def test_a_partially_filled_entry_stays_open_and_is_not_a_holding(
    rig: Rig,
) -> None:
    _partial_rig(rig)
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "entry_pending_partial")]
    signal = next(iter(rig.state.signals.values()))
    intent = rig.state.intents[signal.entry_client_order_id]
    assert (signal.state, intent.state) == ("entry_reserved", "acknowledged")
    assert intent.executed_qty == D("0.3")  # progress recorded, no exposure record
    assert signal.entry_qty == D(0)
    assert len(rig.client.sends) == 1


@pytest.mark.asyncio
async def test_a_partial_entry_is_managed_not_alarmed(rig: Rig) -> None:
    """The breached stop cancels the working remainder; no exit is sent yet."""
    _partial_rig(rig)
    await rig.tick()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")  # below the 95 stop of avg 100
    result = await rig.tick()
    names = [e.event for e in result.events]
    assert ("BTCUSDT", "partial_entry_exposure_open") in rig.events(result)
    assert ("BTCUSDT", "partial_entry_exit_decided") in rig.events(result)
    assert ("BTCUSDT", "partial_entry_cancel_sent") in rig.events(result)
    assert "partial_entry_stop_breached" not in names  # replaced by the managed step
    assert len(rig.client.sends) == 1  # the entry only: never an exit while it fills
    assert rig.client.cancels == [
        {"symbol": "BTCUSDT", "client_order_id": _entry_cid(rig), "confirm": True}
    ]
    signal = next(iter(rig.state.signals.values()))
    assert (signal.state, signal.exit_reason) == ("entry_reserved", EXIT_HARD_STOP)
    assert signal.exit_at == NOW  # recorded when the stop was proven
    assert rig.state.calls["reserve_exit_intent"] == 0


@pytest.mark.asyncio
async def test_a_terminal_fill_after_the_cancel_is_exited_in_full(rig: Rig) -> None:
    """Round-7 scenario: entry 0.3, stop, cancel — and one more fill lands.

    The exit waits for the terminal evidence, so it sends the broker's final
    position (0.6), not the 0.3 visible at decision time, and no exit fill can
    interleave with a later entry fill.
    """
    _partial_rig(rig)
    await rig.tick()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    await rig.tick()
    assert len(rig.client.sends) == 1 and len(rig.client.cancels) == 1

    _terminal_entry(rig, executed="0.6", avg="120")
    _ready_exit(rig)
    result = await rig.tick()
    assert ("BTCUSDT", "reconciled_filled") in rig.events(result)
    assert [s["reduce_only"] for s in rig.client.sends] == [False, True]
    assert rig.client.sends[1]["qty"] == D("0.6")
    signal = next(iter(rig.state.signals.values()))
    assert (signal.state, signal.entry_qty, signal.closed_qty) == (
        "closed",
        D("0.6"),
        D("0.6"),
    )
    assert signal.entry_price == D("120")  # the broker's cumulative entry average
    assert signal.exit_reason == EXIT_HARD_STOP


@pytest.mark.asyncio
async def test_a_recorded_stop_is_latched_after_the_price_recovers(rig: Rig) -> None:
    _partial_rig(rig)
    await rig.tick()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    await rig.tick()  # the stop is proven while the entry is still working
    rig.minutes["BTCUSDT"] = minute_rows(low="99")  # back above the stop
    _terminal_entry(rig, executed="0.3", avg="100")
    _ready_exit(rig)
    result = await rig.tick()
    exits = [e for e in result.events if e.event.startswith("exit_")]
    assert len(exits) == 1 and exits[0].detail == EXIT_HARD_STOP
    assert [s["reduce_only"] for s in rig.client.sends] == [False, True]


@pytest.mark.asyncio
async def test_a_short_partial_entry_is_managed_too(rig: Rig) -> None:
    """The broker amount is signed: a short partial entry is managed as well."""
    _partial_rig(rig, side="SELL")
    await rig.tick()
    rig.minutes["BTCUSDT"] = minute_rows(low="99", high="106")  # 105 stop of avg 100
    result = await rig.tick()
    assert ("BTCUSDT", "partial_entry_exit_decided") in rig.events(result)
    assert len(rig.client.cancels) == 1
    assert len(rig.client.sends) == 1  # the entry only
    signal = next(iter(rig.state.signals.values()))
    assert (signal.side, signal.exit_reason) == ("SELL", EXIT_HARD_STOP)

    _terminal_entry(rig, executed="0.3", avg="100", side="SELL")
    _ready_exit(rig, price="90")
    await rig.tick()
    assert [s["reduce_only"] for s in rig.client.sends] == [False, True]
    assert rig.client.sends[1]["side"] == "BUY"  # the close of a short is a buy
    assert rig.client.sends[1]["qty"] == D("0.3")
    assert next(iter(rig.state.signals.values())).state == "closed"


@pytest.mark.asyncio
async def test_a_failed_cancel_is_retried_after_the_price_recovers(rig: Rig) -> None:
    _partial_rig(rig)
    await rig.tick()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    rig.client.cancel_error = TimeoutError("read timeout")
    first = await rig.tick()
    assert ("BTCUSDT", "partial_entry_cancel_unconfirmed") in rig.events(first)
    rig.minutes["BTCUSDT"] = minute_rows(low="99")  # back above the stop
    rig.client.cancel_error = None
    second = await rig.tick()
    # The recorded stop is a latch: the recovery does not stop the retry.
    assert ("BTCUSDT", "partial_entry_cancel_sent") in rig.events(second)
    assert len(rig.client.cancels) == 2


@pytest.mark.asyncio
async def test_a_decision_record_failure_does_not_abort_the_tick(rig: Rig) -> None:
    _partial_rig(rig)
    await rig.tick()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")

    async def boom(*args, **kwargs):
        raise TrendStateBlocked("state unavailable")

    rig.state.record_exit_decision = boom  # type: ignore[method-assign]
    result = await rig.tick()  # must not raise out of the tick
    assert ("BTCUSDT", "blocked") in rig.events(result)
    # The action the just-computed decision calls for still runs.
    assert ("BTCUSDT", "partial_entry_cancel_sent") in rig.events(result)


@pytest.mark.asyncio
async def test_a_failed_1m_fetch_still_manages_a_stopped_out_4h_close(
    rig: Rig,
) -> None:
    _partial_rig(rig)
    await rig.tick()
    rig.minutes["BTCUSDT"] = RuntimeError("klines down")
    rig.bars["BTCUSDT"] = trend_bars("BUY", last="94", past="90")
    result = await rig.tick()
    assert ("BTCUSDT", "partial_entry_exit_decided") in rig.events(result)
    assert len(rig.client.cancels) == 1
    assert len(rig.client.sends) == 1


@pytest.mark.asyncio
async def test_an_unconfirmed_cancel_still_sends_no_exit(rig: Rig) -> None:
    _partial_rig(rig)
    await rig.tick()
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    rig.client.cancel_error = TimeoutError("read timeout")
    result = await rig.tick()
    assert ("BTCUSDT", "partial_entry_cancel_unconfirmed") in rig.events(result)
    assert len(rig.client.sends) == 1  # the entry only
    assert rig.state.calls["reserve_exit_intent"] == 0
    signal = next(iter(rig.state.signals.values()))
    assert signal.exit_reason == EXIT_HARD_STOP  # the decision is still durable


@pytest.mark.asyncio
async def test_the_kill_switch_counts_a_stop_on_the_day_it_fired(rig: Rig) -> None:
    """A stop proven while the entry works is counted though nothing closed."""
    for symbol in ("ETHUSDT", "SOLUSDT"):
        rig.state.seed_holding(
            symbol=symbol,
            state="blocked",
            exit_reason=EXIT_HARD_STOP,
            exit_at=NOW - dt.timedelta(hours=1),
        )
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    result = await rig.tick()
    assert rig.client.sends == []
    assert any("consecutive_hard_stop_limit_reached" in e.event for e in result.events)


@pytest.mark.asyncio
async def test_a_stop_from_another_utc_day_does_not_count(rig: Rig) -> None:
    for symbol in ("ETHUSDT", "SOLUSDT"):
        rig.state.seed_holding(
            symbol=symbol,
            state="blocked",
            exit_reason=EXIT_HARD_STOP,
            exit_at=NOW - dt.timedelta(days=1, hours=1),
        )
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.auto_fill = True
    result = await rig.tick()
    assert not any("entry_refused" in e.event for e in result.events)
    assert rig.client.sends[0]["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_no_alarm_while_the_partial_entry_is_above_its_stop(rig: Rig) -> None:
    _partial_rig(rig)
    await rig.tick()
    rig.minutes["BTCUSDT"] = minute_rows(low="99")
    result = await rig.tick()
    assert "partial_entry_stop_breached" not in [e.event for e in result.events]
    assert rig.client.cancels == []
    assert len(rig.client.sends) == 1


@pytest.mark.asyncio
async def test_a_partial_entry_that_ends_is_a_normal_holding_with_the_executed_qty(
    rig: Rig,
) -> None:
    _partial_rig(rig)
    await rig.tick()
    rig.client.order_script = [{"status": "CANCELED", "executed": D("0.3")}]
    result = await rig.tick()  # terminal evidence: the executed 0.3 is the holding
    assert ("BTCUSDT", "reconciled_filled") in rig.events(result)
    signal = next(iter(rig.state.signals.values()))
    assert (signal.state, signal.entry_qty) == ("holding", D("0.3"))
    assert rig.ledger.rows[signal.entry_client_order_id] == "filled"  # closable root


@pytest.mark.asyncio
async def test_an_uncertain_entry_found_partially_filled_still_alarms_and_blocks(
    rig: Rig,
) -> None:
    rig.bars["BTCUSDT"] = trend_bars("BUY")
    rig.client.submit_error = TimeoutError("read timeout")  # sent, outcome unknown
    first = await rig.tick()
    assert rig.events(first) == [("BTCUSDT", "entry_uncertain")]
    rig.client.submit_error = None
    rig.client.order_script = [{"status": "PARTIALLY_FILLED", "executed": D("0.3")}]
    rig.client.positions["BTCUSDT"] = D("0.3")
    rig.minutes["BTCUSDT"] = minute_rows(low="94")
    second = await rig.tick()
    names = [e.event for e in second.events]
    assert "partial_entry_exposure_open" in names  # the intent is still uncertain
    assert names.count("partial_entry_stop_breached") == 1
    assert ("BTCUSDT", "reconciled_pending_partial") in rig.events(second)
    assert "reconciled_no_fill" not in names
    signal = next(iter(rig.state.signals.values()))
    assert rig.state.intents[signal.entry_client_order_id].state == "uncertain"
    assert len(rig.client.sends) == 1  # no resend, no cancel, no exit
    assert rig.client.cancels == []
    assert rig.state.calls["reserve_exit_intent"] == 0
