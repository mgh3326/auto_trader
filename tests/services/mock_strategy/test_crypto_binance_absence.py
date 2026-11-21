"""hk 5910: Binance spot demo -2013 + empty myTrades closes an unresolved leg."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.mock_strategy.crypto_brokers import (
    MY_TRADES_LIMIT,
    BinanceCryptoDemo,
)
from app.services.mock_strategy.crypto_runner import close_due, run_lists
from app.services.mock_strategy.recorder import (
    record_alpaca_absence,
    record_binance_spot_absence,
)
from tests.services.mock_strategy import test_crypto as base

pytestmark = pytest.mark.unit
ASOF, DECISION, signals = base.ASOF, base.DECISION, base.signals
enabled = base.enabled  # autouse env fixture


class Recorder(base.Recorder):
    def __init__(self):
        super().__init__()
        self.absence_calls = []

    async def record_many(self, intents):
        rows = await super().record_many(intents)
        for row in rows:
            for column in ("broker_order_id", "opened_at", "entry_price", "exit_price"):
                row.__dict__.setdefault(column, None)
        return rows

    async def update(self, cid, **kwargs):
        broker_order_id = kwargs.get("broker_order_id")
        row = await super().update(cid, **kwargs)
        if broker_order_id is not None:
            row.broker_order_id = broker_order_id
        return row

    async def confirm_binance_spot_absence(self, cid, *, account_ref):
        self.absence_calls.append(cid)
        row = self.rows[cid]
        assert row.account_ref == account_ref
        if record_binance_spot_absence(row):
            self.writes += 1
        return row


class Broker(base.Broker):
    """Spot-style fake: lookups of an unbound leg raise not-found on demand."""

    def __init__(self, recorder):
        super().__init__(recorder)
        self.lookup_error: BaseException | None = None
        self.trade_verdict = True
        self.absence_checks = []
        self.lost = set()  # client ids the exchange never received

    async def evidence(self, order):
        if order.client_order_id in self.lost:
            raise self.lookup_error or BinanceDemoOrderNotFound(order.client_order_id)
        return await super().evidence(order)

    async def submit(self, order, *, confirm):
        if order.client_order_id in self.lost:
            self.calls.append(order)
            raise TimeoutError("fake timeout after potential send")
        return await super().submit(order, confirm=confirm)

    async def confirm_absence(self, order, *, window_start, other_order_ids):
        self.absence_checks.append((order.client_order_id, window_start))
        if isinstance(self.trade_verdict, BaseException):
            raise self.trade_verdict
        return self.trade_verdict


def args(recorder, broker, venue="binance_spot_demo"):
    return {
        "venue": venue,
        "asof": DECISION + timedelta(days=3),
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
    }


async def open_book(recorder, broker, venue="binance_spot_demo", lose=1):
    """Open ten entries; the first ``lose`` end uncertain (POST timed out)."""
    sig = signals()
    key = "down" if venue == "binance_futures_demo" else "up"
    first = [n["symbol"] for n in sig["horizons"]["3"][key]][:lose]
    # Mark the first entries' client ids as lost before sending.
    from app.services.mock_strategy.crypto_orders import client_id, strategy_id

    for symbol in first:
        broker.lost.add(
            client_id(
                strategy_id(3), venue, broker.account_ref, ASOF.isoformat(), symbol
            )
        )
    try:
        await run_lists(
            sig,
            venue=venue,
            asof=ASOF,
            broker=broker,
            recorder=recorder,
            dry_run=False,
            confirm=True,
            now=DECISION,
        )
    except TimeoutError:
        pass
    return sorted(broker.lost)


@pytest.mark.asyncio
async def test_not_found_and_no_trades_rejects_with_zero_fill_and_no_resend():
    recorder, venue = Recorder(), "binance_spot_demo"
    broker = Broker(recorder)
    (lost,) = await open_book(recorder, broker)
    assert recorder.rows[lost].status == "uncertain"
    result = await close_due(**args(recorder, broker, venue))
    row = recorder.rows[lost]
    assert row.status == "rejected"
    assert row.meta["filled_qty"] == "0"
    assert row.meta["broker_status"] == "broker_confirmed_absent"
    assert row.meta["no_order_proven"] is True
    assert recorder.absence_calls == [lost]
    assert {"client_order_id": lost, "status": "entry_rejected_absent"} in result[
        "orders"
    ]
    # The window starts at the decision time the batch was cut from.
    assert broker.absence_checks[0][1] == row.exit_due_at - timedelta(days=3)
    assert sum(c.client_order_id == lost for c in broker.calls) == 1


@pytest.mark.asyncio
async def test_rejected_entry_is_not_closed_and_not_sent_again():
    recorder = Recorder()
    broker = Broker(recorder)
    (lost,) = await open_book(recorder, broker)
    await close_due(**args(recorder, broker))
    before = len(broker.calls)
    await close_due(**args(recorder, broker))
    # Nine real entries closed once; the rejected one produced no order.
    assert not any(
        c.client_order_id == lost
        or c.side == "sell"
        and c.symbol == recorder.rows[lost].symbol
        for c in broker.calls[before:]
    )
    assert recorder.rows[lost].status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [False, ValueError("myTrades malformed")])
async def test_trade_found_or_unreadable_trades_keep_the_leg(verdict):
    recorder = Recorder()
    broker = Broker(recorder)
    (lost,) = await open_book(recorder, broker)
    broker.trade_verdict = verdict
    result = await close_due(**args(recorder, broker))
    assert recorder.rows[lost].status == "uncertain"
    assert recorder.absence_calls == []
    entry = next(o for o in result["orders"] if o["client_order_id"] == lost)
    assert entry["status"] == "unresolved"
    assert entry["error_class"] == "BinanceDemoOrderNotFound" or isinstance(
        verdict, ValueError
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("lookup timeout"),
        RuntimeError("anything else"),
        httpx.HTTPStatusError(
            "5xx",
            request=httpx.Request("GET", "https://demo-api.binance.com"),
            response=httpx.Response(503),
        ),
        httpx.HTTPStatusError(
            "empty 400",
            request=httpx.Request("GET", "https://demo-api.binance.com"),
            response=httpx.Response(400),
        ),
    ],
)
async def test_other_lookup_outcomes_never_close_and_skip_the_trade_read(error):
    recorder = Recorder()
    broker = Broker(recorder)
    (lost,) = await open_book(recorder, broker)
    broker.lookup_error = error
    result = await close_due(**args(recorder, broker))
    assert recorder.rows[lost].status == "uncertain"
    assert broker.absence_checks == []
    assert recorder.absence_calls == []
    entry = next(o for o in result["orders"] if o["client_order_id"] == lost)
    assert entry["status"] == "unresolved"


@pytest.mark.asyncio
async def test_dry_run_reports_but_never_writes():
    recorder = Recorder()
    broker = Broker(recorder)
    (lost,) = await open_book(recorder, broker)
    writes = recorder.writes
    result = await close_due(**{**args(recorder, broker), "dry_run": True})
    assert recorder.rows[lost].status == "uncertain"
    assert recorder.writes == writes
    assert {"client_order_id": lost, "status": "would_reject_absent"} in result[
        "orders"
    ]


@pytest.mark.asyncio
async def test_unbound_close_leg_proven_absent_is_replaced_by_a_new_indexed_leg():
    recorder = Recorder()
    broker = Broker(recorder)
    await run_lists(
        signals(),
        venue="binance_spot_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    # First close attempt times out for exactly one entry's sell leg.
    entry = next(iter(recorder.rows.values()))
    close_ids = set()
    original_submit = broker.submit

    async def submit(order, *, confirm):
        if order.side == "sell" and order.symbol == entry.symbol and not close_ids:
            close_ids.add(order.client_order_id)
            broker.lost.add(order.client_order_id)
        return await original_submit(order, confirm=confirm)

    broker.submit = submit
    first = await close_due(**args(recorder, broker))
    (lost_leg,) = close_ids
    assert recorder.rows[lost_leg].status == "uncertain"
    assert (
        next(
            o for o in first["orders"] if o["client_order_id"] == entry.client_order_id
        )["status"]
        == "unresolved"
    )
    sells_before = [c for c in broker.calls if c.side == "sell"]
    second = await close_due(**args(recorder, broker))
    assert recorder.rows[lost_leg].status == "rejected"
    assert recorder.rows[lost_leg].meta["broker_status"] == "broker_confirmed_absent"
    assert (
        next(
            o for o in second["orders"] if o["client_order_id"] == entry.client_order_id
        )["status"]
        == "closed"
    )
    resent = [
        c
        for c in broker.calls
        if c.side == "sell" and c.symbol == entry.symbol and c not in sells_before
    ]
    assert len(resent) == 1
    assert resent[0].client_order_id != lost_leg
    assert resent[0].qty == entry.qty  # whole proven residual, nothing was filled
    assert broker.absence_checks[-1][1] == entry.exit_due_at


@pytest.mark.asyncio
async def test_futures_demo_not_found_is_unchanged():
    recorder = Recorder()
    broker = Broker(recorder)
    (lost,) = await open_book(recorder, broker, venue="binance_futures_demo")
    result = await close_due(**args(recorder, broker, "binance_futures_demo"))
    assert recorder.rows[lost].status == "uncertain"
    assert broker.absence_checks == [] and recorder.absence_calls == []
    entry = next(o for o in result["orders"] if o["client_order_id"] == lost)
    assert entry["status"] == "unresolved"
    assert entry["error_class"] == "BinanceDemoOrderNotFound"


def row(**changes):
    values = {
        "venue": "binance_spot_demo",
        "status": "uncertain",
        "broker_order_id": None,
        "opened_at": None,
        "entry_price": None,
        "exit_price": None,
        "meta": {},
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"venue": "binance_futures_demo"},
        {"venue": "alpaca_paper_crypto"},
        {"status": "accepted"},
        {"broker_order_id": "42"},
        {"meta": {"filled_qty": "0.1"}},
        {"meta": {"filled_qty": "nan-ish"}},
    ],
)
def test_pure_transition_refuses_everything_but_unbound_unfilled_spot(changes):
    target = row(**changes)
    assert record_binance_spot_absence(target) is False
    assert target.status == changes.get("status", "uncertain")


def test_pure_transition_is_per_venue_and_closes_with_zero_fill():
    target = row(status="sending")
    assert record_alpaca_absence(target) is False
    assert record_binance_spot_absence(target) is True
    assert target.status == "rejected"
    assert target.meta["filled_qty"] == "0"
    assert target.meta["broker_status"] == "broker_confirmed_absent"
    assert target.meta["no_order_sent"] is True
    assert record_binance_spot_absence(target) is False
    assert record_alpaca_absence(row(venue="alpaca_paper", status="sending")) is True


class SpotClient:
    credential_fingerprint = "fixture-fingerprint"

    def __init__(self, trades):
        self.trades, self.calls = trades, []

    async def get_symbol_trades_since(self, **kwargs):
        self.calls.append(kwargs)
        return self.trades


def trade(order_id, buyer=True, symbol="BTCUSDT"):
    return {"symbol": symbol, "orderId": order_id, "isBuyer": buyer}


def spot_order(side="buy"):
    return SimpleNamespace(symbol="BTCUSDT", side=side, client_order_id="r1474c-x")


START = datetime(2026, 11, 9, 0, 5, tzinfo=UTC)


async def verdict(trades, side="buy", others=frozenset(), futures=False):
    client = SpotClient(trades)
    broker = BinanceCryptoDemo(
        client, futures=futures, expected_fingerprint="fixture-fingerprint"
    )
    result = await broker.confirm_absence(
        spot_order(side), window_start=START, other_order_ids=others
    )
    return result, client


@pytest.mark.asyncio
async def test_confirm_absence_reads_symbol_trades_from_window_start():
    result, client = await verdict([])
    assert result is True
    assert client.calls == [
        {
            "symbol": "BTCUSDT",
            "start_time_ms": int(START.timestamp() * 1000),
            "limit": MY_TRADES_LIMIT,
        }
    ]


@pytest.mark.asyncio
async def test_confirm_absence_trade_rules():
    assert (await verdict([trade(7)]))[0] is False  # unattributed same side
    assert (await verdict([trade(7, buyer=False)]))[0] is True  # opposite side
    assert (await verdict([trade(7)], others=frozenset({"7"})))[0] is True
    assert (await verdict([trade(7, buyer=False)], side="sell"))[0] is False
    full = [trade(i, buyer=False) for i in range(MY_TRADES_LIMIT)]
    assert (await verdict(full))[0] is False  # possibly truncated
    with pytest.raises(ValueError):
        await verdict([{"symbol": "BTCUSDT", "orderId": 1}])
    # Every row needs a valid orderId, whatever its side or attribution.
    for bad in (None, True, "", "4x", 1.5):
        with pytest.raises(ValueError):
            await verdict([trade(bad, buyer=False)])
        with pytest.raises(ValueError):
            await verdict([trade(bad)], others=frozenset({"None"}))
    with pytest.raises(ValueError):
        await verdict([trade(1, symbol="ETHUSDT")])


@pytest.mark.asyncio
async def test_confirm_absence_never_reads_for_futures():
    result, client = await verdict([], futures=True)
    assert result is False and client.calls == []


@pytest.fixture
def spot(monkeypatch):
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_SPOT_DEMO_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SPOT_DEMO_SECRET")
    return BinanceSpotDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_symbol_trades_read_is_a_signed_mytrades_get(spot, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"^https://demo-api\.binance\.com/api/v3/myTrades\?"
            r"(?=.*symbol=BTCUSDT)(?=.*startTime=1000)(?=.*limit=1000)"
            r"(?=.*signature=).*$"
        ),
        json=[{"symbol": "BTCUSDT", "orderId": 1, "isBuyer": True, "qty": "1"}],
    )
    rows = await spot.get_symbol_trades_since(symbol="BTCUSDT", start_time_ms=1000)
    assert rows[0]["orderId"] == 1


@pytest.mark.asyncio
async def test_not_found_requires_the_code_not_the_message(spot, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order\?.*$"),
        status_code=400,
        json={"msg": "Order does not exist."},  # same text, no -2013 code
    )
    with pytest.raises(httpx.HTTPStatusError):
        await spot.get_order_status(symbol="XRPUSDT", client_order_id="r1474c-x")


@pytest.mark.asyncio
async def test_other_order_exclusion_is_limited_to_the_target_symbol():
    """Binance order IDs repeat across symbols: ETH's 42 must not hide BTC's 42."""
    recorder = Recorder()
    broker = Broker(recorder)
    (lost,) = await open_book(recorder, broker)
    entry = recorder.rows[lost]
    for symbol, cid in (("ETHUSDT", "eth-row"), (entry.symbol, "same-symbol-row")):
        recorder.rows[cid] = SimpleNamespace(
            venue=entry.venue,
            account_ref=entry.account_ref,
            client_order_id=cid,
            symbol=symbol,
            side="sell",
            episode_id="other",
            status="filled",
            broker_order_id="42",
        )
    seen = {}

    async def confirm(order, *, window_start, other_order_ids):
        seen["ids"] = other_order_ids
        return False

    broker.confirm_absence = confirm
    await close_due(**args(recorder, broker))
    assert seen["ids"] == frozenset({"42"})  # same-symbol row only
    del recorder.rows["same-symbol-row"]
    await close_due(**args(recorder, broker))
    assert seen["ids"] == frozenset()  # ETH's 42 alone is never excluded


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, "", None, {"code": 1}])
async def test_symbol_trades_require_a_json_array(spot, httpx_mock, body):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/myTrades\?.*$"),
        json=body,
    )
    with pytest.raises(ValueError):
        await spot.get_symbol_trades_since(symbol="BTCUSDT", start_time_ms=1000)
