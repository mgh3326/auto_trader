from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.binance_h5 import BinanceH5Intent, BinanceH5Signal
from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.h5.client import H5DemoClient
from app.services.brokers.binance.h5.executor import H5Executor
from app.services.brokers.binance.h5.history import (
    collect_holding_history,
    collect_signal_history,
)
from app.services.brokers.binance.h5.holding import Holding, choose_exit
from app.services.brokers.binance.h5.state import H5StateBlocked, H5StateService
from app.services.brokers.binance.h5.strategy import (
    DEMO_URL,
    H5Strategy,
    bar_price_text,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS, MINUTE_MS
from tests.services.brokers.binance.h5.fake_state_db import (
    NOW,
    FakeStore,
    intent_values,
    signal_values,
)


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class _MinuteTransport:
    base_url = DEMO_URL

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def get(self, path, *, params):
        self.calls += 1
        assert path == "/fapi/v1/klines"
        selected = [
            row
            for row in self.rows
            if params["startTime"] <= row[0] <= params["endTime"]
        ]
        return _Response(selected[: params["limit"]])


class _FakeH5Client(H5DemoClient):
    def __init__(self, transport, *, base_url=DEMO_URL):
        self._client = transport
        self._base_url = base_url


def _minute_rows(start_ms: int, count: int, *, adverse_index: int | None = None):
    return [
        [
            start_ms + i * MINUTE_MS,
            "100",
            "101",
            "94" if i == adverse_index else "99",
            "100",
            "1",
            start_ms + (i + 1) * MINUTE_MS - 1,
        ]
        for i in range(count)
    ]


def test_paged_history_yields_21_complete_bars() -> None:
    start = 1000 * FOUR_HOUR_MS
    transport = _MinuteTransport(_minute_rows(start, 21 * 240))
    client = _FakeH5Client(transport)
    bars = asyncio.run(
        collect_signal_history(client, "BTCUSDT", decision_ts=start + 21 * FOUR_HOUR_MS)
    )
    assert len(bars) == 21
    assert bars[-1].close_ts == start + 21 * FOUR_HOUR_MS
    assert transport.calls == 11  # 5,040 rows cannot come from the old 500-row default
    assert bar_price_text(bars[-1], "close") == "100"


def test_first_post_entry_close_is_complete_but_pre_entry_extrema_are_excluded():
    start = 1000 * FOUR_HOUR_MS
    client = _FakeH5Client(_MinuteTransport(_minute_rows(start, 240, adverse_index=0)))
    minutes, bars = asyncio.run(
        collect_holding_history(
            client,
            "BTCUSDT",
            entered_ms=start + 10 * MINUTE_MS + 500,
            now_ms=start + FOUR_HOUR_MS,
        )
    )
    assert len(bars) == 1
    assert bars[0].close_ts == start + FOUR_HOUR_MS
    assert min(row.low for row in minutes) == 99


def test_late_restart_history_remains_bounded_and_time_exit_eligible():
    start = 1000 * FOUR_HOUR_MS
    transport = _MinuteTransport(_minute_rows(start, 24 * 60))
    minutes, bars = asyncio.run(
        collect_holding_history(
            _FakeH5Client(transport),
            "BTCUSDT",
            entered_ms=start,
            now_ms=start + 100 * FOUR_HOUR_MS,
        )
    )
    assert len(minutes) == 1440 and len(bars) == 6
    assert transport.calls == 3


@pytest.mark.parametrize(
    "base_url",
    [
        "http://demo-fapi.binance.com",
        "https://demo-fapi.binance.com.evil.invalid",
        "https://testnet.binancefuture.com",
        "https://demo-fapi.binance.com/path",
    ],
)
def test_plugin_client_context_blocks_before_io(base_url):
    transport = _MinuteTransport([])
    blocked = False
    try:
        H5Strategy().validate_client(_FakeH5Client(transport, base_url=base_url))
    except ValueError:
        blocked = True
    assert blocked is True and transport.calls == 0


def test_intrabar_hard_stop_observed_before_four_hour_close() -> None:
    start = 1000 * FOUR_HOUR_MS
    client = _FakeH5Client(_MinuteTransport(_minute_rows(start, 10, adverse_index=4)))
    minutes, bars = asyncio.run(
        collect_holding_history(
            client, "BTCUSDT", entered_ms=start, now_ms=start + 10 * MINUTE_MS
        )
    )
    assert len(minutes) == 10 and bars == ()
    entered = dt.datetime.fromtimestamp(start / 1000, tz=dt.UTC)
    exit_decision = choose_exit(
        Holding(
            side="BUY",
            entry_price=Decimal("100"),
            entry_qty=Decimal("1"),
            broker_remaining_qty=Decimal("1"),
            closed_qty=Decimal("0"),
            entered_at=entered,
            completed_bars_held=0,
            step_size=Decimal("0.1"),
        ),
        quote_price=Decimal("100"),
        intrabar_low=min(Decimal(str(row.low)) for row in minutes),
        intrabar_high=max(Decimal(str(row.high)) for row in minutes),
        completed_bar_close=None,
        now=entered + dt.timedelta(minutes=10),
    )
    assert exit_decision is not None and exit_decision.reason == "hard_stop"


def test_non_demo_client_stops_before_any_fetch() -> None:
    transport = _MinuteTransport([])
    client = _FakeH5Client(transport, base_url="https://fapi.binance.com")
    blocked = False
    try:
        asyncio.run(
            collect_signal_history(client, "BTCUSDT", decision_ts=21 * FOUR_HOUR_MS)
        )
    except ValueError:
        blocked = True
    assert blocked is True
    assert transport.calls == 0


def test_executor_guard_precedes_network_and_db(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_H5_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    transport = _MinuteTransport([])
    client = _FakeH5Client(transport, base_url="https://fapi.binance.com")

    class _State:
        calls = 0

        async def record_nav(self, **kwargs):
            self.calls += 1

    state = _State()
    executor = H5Executor(
        client=client,
        strategy=H5Strategy(),
        state=state,
        demo_ledger=SimpleNamespace(),
        ledger_session=SimpleNamespace(),
    )
    blocked = False
    try:
        asyncio.run(executor.run_tick(now=dt.datetime.now(dt.UTC), confirm=True))
    except ValueError:
        blocked = True
    assert blocked is True
    assert transport.calls == 0 and state.calls == 0


def test_exception_after_recorded_send_never_resubmits() -> None:
    class _State:
        phase = "reserved"

        async def fence_send(self, client_order_id, *, now):
            if self.phase != "reserved":
                raise H5StateBlocked("already sent")
            self.phase = "sending"

        async def mark_uncertain(self, client_order_id, *, now):
            self.phase = "uncertain"

    class _Client:
        calls = 0

        async def submit_order(self, **kwargs):
            self.calls += 1
            raise RuntimeError("socket failed after broker recorded the order")

    state = _State()
    client = _Client()
    executor = H5Executor(
        client=client,
        strategy=H5Strategy(),
        state=state,
        demo_ledger=SimpleNamespace(),
        ledger_session=SimpleNamespace(),
    )
    intent = SimpleNamespace(
        client_order_id="h5-one", side="BUY", qty=Decimal("1"), reduce_only=False
    )
    signal = SimpleNamespace(symbol="BTCUSDT")
    now = dt.datetime.now(dt.UTC)
    for _ in range(2):
        try:
            asyncio.run(executor._send_reserved(intent=intent, signal=signal, now=now))
        except (RuntimeError, H5StateBlocked):
            pass
    assert client.calls == 1
    assert state.phase == "uncertain"


class _RecoveryLedger:
    def __init__(self):
        self.row = SimpleNamespace(lifecycle_state="validated")

    async def get_by_client_order_id(self, cid):
        return self.row

    async def record_submitted(self, **kwargs):
        self.row.lifecycle_state = "submitted"

    async def record_filled(self, **kwargs):
        self.row.lifecycle_state = "filled"


def test_restart_resolves_recorded_send_by_exact_client_id_without_submit(monkeypatch):
    import app.services.brokers.binance.h5.executor as executor_module

    monkeypatch.setattr(executor_module, "ensure_entry_forecast", AsyncMock())
    store = FakeStore()
    store.put(
        BinanceH5Signal,
        signal_values(state="entry_reserved", entry_client_order_id="h5-one"),
    )
    store.put(BinanceH5Intent, intent_values(state="reserved"))
    state = H5StateService(store.factory)

    class Client:
        sends = 0
        lookups = []

        async def submit_order(self, **kwargs):
            self.sends += 1
            raise RuntimeError("response lost after recorded send")

        async def get_order(self, *, symbol, client_order_id):
            self.lookups.append(client_order_id)
            return SimpleNamespace(
                client_order_id=client_order_id,
                broker_order_id="847",
                symbol=symbol,
                side="BUY",
                orig_qty=Decimal("1"),
                executed_qty=Decimal("1"),
                avg_price=Decimal("100"),
                status="FILLED",
                reduce_only=False,
                position_side="BOTH",
                raw_response_redacted={
                    "time": int(NOW.timestamp() * 1000),
                    "updateTime": int(NOW.timestamp() * 1000),
                },
            )

        async def get_all_positions(self):
            return [SimpleNamespace(symbol="BTCUSDT", position_amt=Decimal("1"))]

    client, ledger = Client(), _RecoveryLedger()
    session = SimpleNamespace(commit=AsyncMock())
    executor = H5Executor(
        client=client,
        strategy=H5Strategy(),
        state=state,
        demo_ledger=ledger,
        ledger_session=session,
    )
    intent = SimpleNamespace(
        client_order_id="h5-one", side="BUY", qty=Decimal("1"), reduce_only=False
    )
    signal = SimpleNamespace(symbol="BTCUSDT")
    with pytest.raises(RuntimeError):
        asyncio.run(executor._send_reserved(intent=intent, signal=signal, now=NOW))
    restarted = H5Executor(
        client=client,
        strategy=H5Strategy(),
        state=H5StateService(store.factory),
        demo_ledger=ledger,
        ledger_session=session,
    )
    recovery = SimpleNamespace(
        client_order_id="h5-one", signal_key="s1", reduce_only=False
    )
    result = asyncio.run(restarted._reconcile_intent(recovery, now=NOW))
    assert result.state == "holding" and result.entry_qty == Decimal("1")
    assert client.sends == 1 and client.lookups == ["h5-one"]
    assert store.values(BinanceH5Intent)[0]["state"] == "settled"
    assert ledger.row.lifecycle_state == "filled"


def test_order_not_found_does_not_release_uncertainty_or_resubmit():
    store = FakeStore()
    store.put(BinanceH5Signal, signal_values(state="entry_reserved"))
    store.put(BinanceH5Intent, intent_values())
    client = SimpleNamespace(
        get_order=AsyncMock(side_effect=BinanceDemoOrderNotFound("missing")),
        submit_order=AsyncMock(),
    )
    executor = H5Executor(
        client=client,
        strategy=H5Strategy(),
        state=H5StateService(store.factory),
        demo_ledger=SimpleNamespace(),
        ledger_session=SimpleNamespace(),
    )
    intent = SimpleNamespace(client_order_id="h5-one", signal_key="s1")
    result = asyncio.run(executor._reconcile_intent(intent, now=NOW))
    assert result is None
    assert store.values(BinanceH5Intent)[0]["state"] == "uncertain"
    assert store.values(BinanceH5Signal)[0]["state"] == "uncertain"
    assert client.submit_order.await_count == 0
