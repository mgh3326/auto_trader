"""Offline acceptance tests: the only broker here is an in-memory fake."""

from __future__ import annotations

from copy import copy
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.mock_strategy.crypto_brokers import FillEvidence
from app.services.mock_strategy.crypto_orders import MarketRules, binance_rules
from app.services.mock_strategy.crypto_runner import close_due, run_lists
from app.services.mock_strategy.crypto_signals import BASELINE

pytestmark = pytest.mark.unit
ASOF = date(2023, 12, 31)
DECISION = datetime(2024, 1, 1, 0, 5, tzinfo=UTC)


def signals():
    return {
        "model": BASELINE,
        "panel_venue": "binance_usdt_spot",
        "asof": ASOF.isoformat(),
        "input_prefix_sha256": "fixture-prefix",
        "horizons": {
            "3": {
                "up": [{"symbol": f"COIN{i}USDT"} for i in range(10)],
                "down": [{"symbol": f"COIN{i}USDT"} for i in range(10, 20)],
            }
        },
    }


class Recorder:
    def __init__(self):
        self.rows = {}
        self.writes = 0

    async def get(self, cid):
        return self.rows.get(cid)

    async def record_many(self, intents):
        result = []
        for intent in intents:
            if intent.client_order_id not in self.rows:
                self.rows[intent.client_order_id] = SimpleNamespace(
                    **asdict(intent), status="planned"
                )
                self.writes += 1
            result.append(self.rows[intent.client_order_id])
        return result

    async def record(self, intent):
        return (await self.record_many([intent]))[0]

    async def claim(self, cid):
        row = self.rows[cid]
        if row.status != "planned":
            return False
        row.status = "sending"
        self.writes += 1
        return True

    async def update(self, cid, **kwargs):
        row = self.rows[cid]
        meta = kwargs.pop("meta", {})
        row.meta.update(meta)
        for key, value in kwargs.items():
            if value is not None:
                setattr(row, key, value)
        self.writes += 1
        return row

    async def list_orders(self, *, venue, account_ref=None):
        return [
            r
            for r in self.rows.values()
            if r.venue == venue
            and (account_ref is None or r.account_ref == account_ref)
        ]

    async def list_due(self, *, venue, asof, account_ref):
        return [
            r
            for r in await self.list_orders(venue=venue, account_ref=account_ref)
            if r.side in {"buy", "short"}
            and r.status not in {"closed", "rejected", "canceled"}
            and r.exit_due_at <= asof
        ]


class Broker:
    account_ref = "fake-paper-account"

    def __init__(self, recorder):
        self.recorder = recorder
        self.orders = {}
        self.calls = []
        self.mark = Decimal("30")
        self.fail = False
        self.partial = False
        self.pending = False

    async def rules(self, symbol):
        return MarketRules(Decimal("0.1"), Decimal("0.1"), Decimal("5"))

    async def price(self, symbol):
        return self.mark

    async def closeable(self, order, qty):
        assert qty > 0

    async def submit(self, order, *, confirm):
        assert confirm is True
        assert self.recorder.rows[order.client_order_id].status == "sending"
        self.calls.append(order)
        if self.fail:
            raise TimeoutError("fake timeout after potential send")
        qty = order.qty
        status = "FILLED"
        if self.partial and order.side in {"sell", "cover"}:
            qty = order.qty / 2
            status = "CANCELED"
        if self.pending:
            qty, status = Decimal("0"), "NEW"
        proof = FillEvidence(
            "broker-" + order.client_order_id,
            status,
            qty,
            self.mark if qty else None,
            not self.pending,
        )
        self.orders[order.client_order_id] = proof
        return proof

    async def evidence(self, order):
        return self.orders[order.client_order_id]


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    for key in (
        "MOCK_STRATEGY_ENABLED",
        "BINANCE_SPOT_DEMO_ENABLED",
        "BINANCE_FUTURES_DEMO_ENABLED",
        "ALPACA_PAPER_CRYPTO_ENABLED",
    ):
        monkeypatch.setenv(key, "true")


@pytest.mark.parametrize(
    "venue", ["binance_spot_demo", "binance_futures_demo", "alpaca_paper_crypto"]
)
@pytest.mark.asyncio
async def test_open_rerun_price_move_and_close_due(venue):
    recorder = Recorder()
    broker = Broker(recorder)
    args = {
        "venue": venue,
        "asof": ASOF,
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
        "now": DECISION,
    }
    first = await run_lists(signals(), **args)
    assert len(first["orders"]) == 10
    assert all(
        c.side == ("short" if venue == "binance_futures_demo" else "buy")
        for c in broker.calls
    )
    broker.mark = Decimal("33")
    replay = await run_lists(signals(), **args)
    assert all(o["status"] == "replayed" for o in replay["orders"])
    assert len(broker.calls) == 10
    before = await close_due(
        venue=venue,
        asof=DECISION + timedelta(days=2),
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
    )
    assert before["orders"] == []
    closed = await close_due(
        venue=venue,
        asof=DECISION + timedelta(days=3),
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
    )
    assert len(closed["orders"]) == 10
    assert all(o["status"] == "closed" for o in closed["orders"])
    assert all(
        c.side == ("cover" if venue == "binance_futures_demo" else "sell")
        for c in broker.calls[10:]
    )
    pnl = [r.realized_pnl for r in recorder.rows.values() if r.side in {"buy", "short"}]
    assert all(p < 0 if venue == "binance_futures_demo" else p > 0 for p in pnl)
    await close_due(
        venue=venue,
        asof=DECISION + timedelta(days=4),
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
    )
    assert len(broker.calls) == 20


@pytest.mark.asyncio
async def test_dry_run_never_writes_or_submits():
    recorder = Recorder()
    broker = Broker(recorder)
    plan = await run_lists(
        signals(), venue="binance_spot_demo", asof=ASOF, broker=broker
    )
    assert len(plan["orders"]) == 10
    assert plan["orders"][0]["qty"] == "33.3"
    assert recorder.writes == 0 and broker.calls == []


@pytest.mark.asyncio
async def test_uncertain_claim_is_never_sent_again():
    recorder = Recorder()
    broker = Broker(recorder)
    broker.fail = True
    args = {
        "venue": "binance_spot_demo",
        "asof": ASOF,
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
        "now": DECISION,
    }
    with pytest.raises(TimeoutError):
        await run_lists(signals(), **args)
    uncertain = broker.calls[0].client_order_id
    assert recorder.rows[uncertain].status == "uncertain"
    broker.fail = False
    await run_lists(signals(), **args)
    assert sum(c.client_order_id == uncertain for c in broker.calls) == 1


@pytest.mark.asyncio
async def test_terminal_partial_close_gets_new_residual_leg():
    recorder = Recorder()
    broker = Broker(recorder)
    # Use an exactly even stepped quantity so half is executable.
    broker.mark = Decimal("25")
    await run_lists(
        signals(),
        venue="binance_futures_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    broker.partial = True
    args = {
        "venue": "binance_futures_demo",
        "asof": DECISION + timedelta(days=3),
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
    }
    first = await close_due(**args)
    assert all(o["status"] == "close_partial" for o in first["orders"])
    broker.partial = False
    final = await close_due(**args)
    assert all(o["status"] == "closed" for o in final["orders"])
    assert len(broker.calls) == 30
    assert all(c.qty == Decimal("20") for c in broker.calls[20:])


@pytest.mark.asyncio
async def test_accepted_entry_without_fill_cannot_close():
    recorder = Recorder()
    broker = Broker(recorder)
    broker.pending = True
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
    result = await close_due(
        venue="binance_spot_demo",
        asof=DECISION + timedelta(days=3),
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
    )
    assert all(r["status"] == "entry_fill_pending" for r in result["orders"])
    assert len(broker.calls) == 10


@pytest.mark.parametrize("confirm,offset", [(False, 0), (True, -1), (True, 1)])
@pytest.mark.asyncio
async def test_confirm_and_current_completed_day_required(confirm, offset):
    recorder = Recorder()
    broker = Broker(recorder)
    with pytest.raises(ValueError):
        await run_lists(
            signals(),
            venue="binance_spot_demo",
            asof=ASOF,
            broker=broker,
            recorder=recorder,
            dry_run=False,
            confirm=confirm,
            now=DECISION + timedelta(days=offset),
        )
    assert recorder.writes == 0 and broker.calls == []


def test_lot_and_market_lot_both_apply_without_strategy_cap():
    info = {
        "status": "TRADING",
        "quoteAsset": "USDT",
        "filters": [
            {
                "filterType": "LOT_SIZE",
                "stepSize": "0.03",
                "minQty": "0.03",
                "maxQty": "10000",
            },
            {
                "filterType": "MARKET_LOT_SIZE",
                "stepSize": "0.02",
                "minQty": "0.02",
                "maxQty": "10000",
            },
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
        ],
    }
    rules = binance_rules(info)
    assert rules.step == Decimal("0.06")
    assert rules.floor(Decimal("1000") / 30, Decimal("30")) == Decimal("33.30")
    with pytest.raises(ValueError, match="minimum"):
        rules.floor(Decimal("0.17"), Decimal("30"))


@pytest.mark.asyncio
async def test_disjoint_changed_list_cannot_open_second_dated_batch():
    recorder = Recorder()
    broker = Broker(recorder)
    args = {
        "venue": "binance_spot_demo",
        "asof": ASOF,
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
        "now": DECISION,
    }
    await run_lists(signals(), **args)
    changed = signals()
    changed["input_prefix_sha256"] = "revised-history"
    changed["horizons"]["3"]["up"] = [{"symbol": f"OTHER{i}USDT"} for i in range(10)]
    with pytest.raises(ValueError, match="batch conflict"):
        await run_lists(changed, **args)
    assert len(broker.calls) == 10


@pytest.mark.asyncio
async def test_dust_is_open_and_carries_only_realized_closed_quantity():
    recorder = Recorder()
    broker = Broker(recorder)
    broker.mark = Decimal("25")
    await run_lists(
        signals(),
        venue="binance_futures_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    # Terminal entry filled 39.95 units: market step only permits closing 39.9.
    for cid, proof in broker.orders.items():
        broker.orders[cid] = FillEvidence(
            proof.broker_order_id, "CANCELED", Decimal("39.95"), Decimal("25"), True
        )
    args = {
        "venue": "binance_futures_demo",
        "asof": DECISION + timedelta(days=3),
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
    }
    result = await close_due(**args)
    assert all(
        r["status"] == "dust_remaining"
        and Decimal(r["residual_qty"]) == Decimal("0.05")
        for r in result["orders"]
    )
    assert all(
        r.status == "filled" and r.realized_pnl == 0
        for r in recorder.rows.values()
        if r.side == "short"
    )
    await close_due(**args)
    assert len(broker.calls) == 20


@pytest.mark.asyncio
async def test_uncertain_close_without_evidence_never_creates_another_leg():
    recorder = Recorder()
    broker = Broker(recorder)
    await run_lists(
        signals(),
        venue="binance_futures_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    args = {
        "venue": "binance_futures_demo",
        "asof": DECISION + timedelta(days=3),
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
    }
    broker.fail = True
    with pytest.raises(TimeoutError):
        await close_due(**args)
    broker.fail = False
    with pytest.raises(KeyError):
        await close_due(**args)
    assert len(broker.calls) == 11


@pytest.mark.asyncio
async def test_later_planned_leg_cannot_hide_earlier_nonterminal_leg():
    recorder = Recorder()
    broker = Broker(recorder)
    await run_lists(
        signals(),
        venue="binance_futures_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    args = {
        "venue": "binance_futures_demo",
        "asof": DECISION + timedelta(days=3),
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
    }
    broker.pending = True
    await close_due(**args)
    prior = next(r for r in recorder.rows.values() if r.side == "cover")
    later = copy(prior)
    later.client_order_id, later.status = "r1474c-x-later-planned", "planned"
    recorder.rows[later.client_order_id] = later
    await close_due(**args)
    assert len(broker.calls) == 20
    another = copy(later)
    another.client_order_id = "r1474c-x-another-planned"
    recorder.rows[another.client_order_id] = another
    with pytest.raises(ValueError, match="multiple planned"):
        await close_due(**args)
    assert len(broker.calls) == 20
