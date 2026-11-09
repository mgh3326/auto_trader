"""Offline acceptance tests: the only broker here is an in-memory fake."""

from __future__ import annotations

from asyncio import CancelledError
from copy import copy
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.services.mock_strategy.crypto_brokers import FillEvidence, PreDispatchRefusal
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
        self.claims = 0

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

    async def record_settings_event(self, cid, event):
        self.rows[cid].meta.setdefault("settings_events", []).append(event)

    async def record(self, intent):
        return (await self.record_many([intent]))[0]

    async def claim(self, cid):
        self.claims += 1
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

    async def entry_rules(self, symbol):
        return await self.rules(symbol)

    async def inspect_entry_settings(self, symbol):
        return None

    async def prepare_entry_settings(self, symbol, *, confirm, audit):
        return None

    async def price(self, symbol):
        return self.mark

    async def closeable(self, order, qty):
        assert qty > 0

    async def preflight(self, order, *, confirm):
        assert confirm is True

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
    result = await close_due(**args)
    assert all(row["status"] == "unresolved" for row in result["orders"])
    broker.fail = False
    result = await close_due(**args)
    assert all(row["status"] == "unresolved" for row in result["orders"])
    assert len(broker.calls) == 20


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
    result = await close_due(**args)
    assert result["orders"][0]["status"] == "unresolved"
    assert len(broker.calls) == 20


@pytest.mark.asyncio
async def test_preflight_refusal_leaves_entry_planned_and_retryable():
    recorder = Recorder()

    class Guarded(Broker):
        blocked = True

        async def preflight(self, order, *, confirm):
            if self.blocked and order.symbol == "COIN0USDT":
                raise PreDispatchRefusal("ValueError")

    broker = Guarded(recorder)
    args = {
        "venue": "binance_spot_demo",
        "asof": ASOF,
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
        "now": DECISION,
    }
    first = await run_lists(signals(), **args)
    assert first["orders"][0]["status"] == "preflight_refused"
    assert (
        next(r for r in recorder.rows.values() if r.symbol == "COIN0USDT").status
        == "planned"
    )
    assert len(broker.calls) == 9
    broker.blocked = False
    await run_lists(signals(), **args)
    assert len(broker.calls) == 10


@pytest.mark.asyncio
async def test_late_proven_no_post_refusal_allows_new_close_leg():
    recorder = Recorder()

    class Guarded(Broker):
        blocked = True

        async def submit(self, order, *, confirm):
            if self.blocked and order.symbol == "COIN10USDT" and order.side == "cover":
                raise PreDispatchRefusal("ValueError")
            return await super().submit(order, confirm=confirm)

    broker = Guarded(recorder)
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
    first = await close_due(**args)
    assert first["orders"][0]["status"] == "rejected"
    refused = next(
        r
        for r in recorder.rows.values()
        if r.side == "cover" and r.symbol == "COIN10USDT"
    )
    assert refused.status == "rejected" and refused.meta["no_order_sent"] is True
    assert len(broker.calls) == 19
    broker.blocked = False
    second = await close_due(**args)
    assert second["orders"][0]["status"] == "closed"
    assert len(broker.calls) == 20
    assert refused.client_order_id not in broker.orders


@pytest.mark.asyncio
async def test_one_missing_entry_does_not_block_other_due_exits_or_reset_open_time():
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
    opened = {cid: r.opened_at for cid, r in recorder.rows.items()}
    missing = broker.calls[0].client_order_id
    del broker.orders[missing]
    result = await close_due(
        venue="binance_spot_demo",
        asof=DECISION + timedelta(days=3),
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
    )
    assert result["orders"][0]["status"] == "unresolved"
    assert sum(r["status"] == "closed" for r in result["orders"]) == 9
    assert len(broker.calls) == 19
    assert all(recorder.rows[cid].opened_at == stamp for cid, stamp in opened.items())


@pytest.mark.asyncio
async def test_cancellation_survives_uncertain_update_failure():
    class BrokenOutcomeRecorder(Recorder):
        async def update(self, cid, **kwargs):
            if kwargs["status"] == "uncertain":
                raise RuntimeError("synthetic outcome-store unavailable")
            return await super().update(cid, **kwargs)

    class CanceledBroker(Broker):
        async def submit(self, order, *, confirm):
            self.calls.append(order)
            raise CancelledError

    recorder = BrokenOutcomeRecorder()
    broker = CanceledBroker(recorder)
    with pytest.raises(CancelledError):
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
    assert len(broker.calls) == 1
    assert recorder.rows[broker.calls[0].client_order_id].status == "sending"


class FuturesSettingsClient:
    credential_fingerprint = "fixture"

    def __init__(self):
        self.settings = {}
        self.position = Decimal("0")
        self.hedge = False
        self.open_orders = False
        self.mismatch = False
        self.calls = []

    async def get_symbol_info(self, *, symbol):
        return {
            "status": "TRADING",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": "0.1",
                    "minQty": "0.1",
                    "maxQty": "10000",
                },
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }

    async def get_ticker_price(self, *, symbol):
        return Decimal("30")

    async def get_preparation_state(self, *, symbol):
        if self.hedge:
            raise ValueError("hedge")
        return SimpleNamespace(
            symbol=symbol,
            margin_type=self.settings.get(symbol, ("cross", 20))[0],
            leverage=self.settings.get(symbol, ("cross", 20))[1],
            position_amt=self.position,
            require_position_side=lambda: "BOTH",
        )

    async def get_position_mode(self):
        return SimpleNamespace(is_hedge_mode=self.hedge)

    async def get_position(self, *, symbol):
        return await self.get_preparation_state(symbol=symbol)

    async def has_open_orders_for_preparation(self, *, symbol):
        return self.open_orders

    async def set_margin_type(self, *, symbol, margin_type, confirm):
        assert margin_type == "ISOLATED" and confirm is True
        self.calls.append((symbol, "margin"))
        _, leverage = self.settings.get(symbol, ("cross", 20))
        self.settings[symbol] = ("isolated", leverage)

    async def set_leverage(self, *, symbol, leverage, require_symbol_echo):
        assert leverage == 1 and require_symbol_echo is True
        self.calls.append((symbol, "leverage"))
        if not self.mismatch:
            margin, _ = self.settings.get(symbol, ("cross", 20))
            self.settings[symbol] = (margin, leverage)

    async def submit_order(self, **kwargs):
        self.calls.append((kwargs["symbol"], "order"))
        return SimpleNamespace(
            raw_response_redacted={
                "symbol": kwargs["symbol"],
                "side": "SELL",
                "type": "MARKET",
                "clientOrderId": kwargs["client_order_id"],
                "origQty": str(kwargs["qty"]),
                "executedQty": str(kwargs["qty"]),
                "avgPrice": "30",
                "orderId": "42",
                "status": "FILLED",
                "positionSide": "BOTH",
                "reduceOnly": False,
            }
        )


@pytest.mark.asyncio
async def test_futures_settings_prepare_and_audit_before_entry():
    from app.services.mock_strategy.crypto_brokers import BinanceCryptoDemo

    client = FuturesSettingsClient()
    recorder = Recorder()
    broker = BinanceCryptoDemo(client, futures=True, expected_fingerprint="fixture")
    result = await run_lists(
        signals(),
        venue="binance_futures_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    assert client.calls[:3] == [
        ("COIN10USDT", "margin"),
        ("COIN10USDT", "leverage"),
        ("COIN10USDT", "order"),
    ]
    assert result["settings_changes"][0]["before"] == {
        "margin_type": "cross",
        "leverage": 20,
    }
    assert (
        next(iter(recorder.rows.values())).meta["settings_events"][-1]["state"]
        == "verified"
    )
    assert len([c for c in client.calls if c[1] == "margin"]) == 10
    assert {s for s, _ in client.calls} == {f"COIN{i}USDT" for i in range(10, 20)}


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["position", "open_orders", "hedge", "mismatch"])
async def test_futures_unsafe_settings_refuse_before_order(condition):
    from app.services.mock_strategy.crypto_brokers import BinanceCryptoDemo

    client = FuturesSettingsClient()
    setattr(client, condition, Decimal("1") if condition == "position" else True)
    recorder = Recorder()
    broker = BinanceCryptoDemo(client, futures=True, expected_fingerprint="fixture")
    result = await run_lists(
        signals(),
        venue="binance_futures_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    assert all(o["status"] == "preflight_refused" for o in result["orders"])
    assert not any(call[1] == "order" for call in client.calls)
    assert recorder.claims == 0
    if condition == "mismatch":
        assert all(
            o["reason_class"] == "settings_preparation_failed" for o in result["orders"]
        )
        assert result["settings_events"][-1]["after"] == {
            "margin_type": "isolated",
            "leverage": 20,
        }
    if condition != "mismatch":
        assert client.calls == []
    if condition == "open_orders":
        assert all(
            o["reason_class"] == "settings_open_orders" for o in result["orders"]
        )


@pytest.mark.asyncio
async def test_futures_dry_run_reports_changes_without_setters():
    from app.services.mock_strategy.crypto_brokers import BinanceCryptoDemo

    client = FuturesSettingsClient()
    broker = BinanceCryptoDemo(client, futures=True, expected_fingerprint="fixture")
    result = await run_lists(
        signals(), venue="binance_futures_demo", asof=ASOF, broker=broker
    )
    assert len(result["would_set"]) == 10
    assert client.calls == []


@pytest.mark.asyncio
async def test_futures_already_isolated_1x_does_not_call_setters():
    from app.services.mock_strategy.crypto_brokers import BinanceCryptoDemo

    client = FuturesSettingsClient()
    client.settings = {f"COIN{i}USDT": ("isolated", 1) for i in range(10, 20)}
    recorder = Recorder()
    broker = BinanceCryptoDemo(client, futures=True, expected_fingerprint="fixture")
    result = await run_lists(
        signals(),
        venue="binance_futures_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    assert all(kind == "order" for _, kind in client.calls)
    assert len(client.calls) == 10
    assert result["settings_changes"] == []


@pytest.mark.asyncio
async def test_futures_one_refusal_does_not_block_other_planned_symbols():
    from app.services.mock_strategy.crypto_brokers import BinanceCryptoDemo

    class OneOpenOrder(FuturesSettingsClient):
        async def has_open_orders_for_preparation(self, *, symbol):
            return symbol == "COIN10USDT"

    client = OneOpenOrder()
    recorder = Recorder()
    broker = BinanceCryptoDemo(client, futures=True, expected_fingerprint="fixture")
    result = await run_lists(
        signals(),
        venue="binance_futures_demo",
        asof=ASOF,
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
        now=DECISION,
    )
    assert result["orders"][0]["reason_class"] == "settings_open_orders"
    assert all(o["submitted"] for o in result["orders"][1:])
    assert not any(symbol == "COIN10USDT" for symbol, _ in client.calls)


class ThreeEntryBroker(Broker):
    def __init__(self, recorder, error):
        super().__init__(recorder)
        self.error = error

    async def rules(self, symbol):
        if symbol not in {"COIN0USDT", "COIN1USDT", "COIN2USDT"}:
            raise ValueError("fixture excludes symbol")
        return await super().rules(symbol)

    async def submit(self, order, *, confirm):
        if order.symbol == "COIN0USDT":
            self.calls.append(order)
            raise self.error
        return await super().submit(order, confirm=confirm)


def entry_args(recorder, broker):
    return {
        "venue": "binance_spot_demo",
        "asof": ASOF,
        "broker": broker,
        "recorder": recorder,
        "dry_run": False,
        "confirm": True,
        "now": DECISION,
    }


def http_status_error():
    request = httpx.Request("POST", "https://example.invalid/order")
    return httpx.HTTPStatusError(
        "sensitive message",
        request=request,
        response=httpx.Response(400, request=request, text="secret body"),
    )


@pytest.mark.asyncio
async def test_http_status_continues_and_replay_does_not_resubmit():
    recorder = Recorder()
    broker = ThreeEntryBroker(recorder, http_status_error())
    try:
        result = await run_lists(signals(), **entry_args(recorder, broker))
    except httpx.HTTPStatusError:
        result = None
    assert result is not None, "HTTP status error aborted the batch"
    assert [o["status"] for o in result["orders"]] == ["uncertain", "filled", "filled"]
    assert result["orders"][0] == {
        "client_order_id": broker.calls[0].client_order_id,
        "status": "uncertain",
        "submitted": None,
        "error_class": "HTTPStatusError",
    }
    assert [r.status for r in recorder.rows.values()] == [
        "uncertain",
        "filled",
        "filled",
    ]
    replay = await run_lists(signals(), **entry_args(recorder, broker))
    assert [o["status"] for o in replay["orders"]] == ["replayed"] * 3
    assert len(broker.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError("transport"), CancelledError()])
async def test_transport_or_cancel_aborts_remaining_entries(error):
    recorder = Recorder()
    broker = ThreeEntryBroker(recorder, error)
    with pytest.raises(type(error)):
        await run_lists(signals(), **entry_args(recorder, broker))
    assert [r.status for r in recorder.rows.values()] == [
        "uncertain",
        "planned",
        "planned",
    ]
    assert [c.symbol for c in broker.calls] == ["COIN0USDT"]


@pytest.mark.asyncio
async def test_http_status_update_fail_preserves_original_error_and_stops():
    class BrokenRecorder(Recorder):
        async def update(self, cid, **kwargs):
            if kwargs["status"] == "uncertain":
                raise RuntimeError("store unavailable")
            return await super().update(cid, **kwargs)

    recorder = BrokenRecorder()
    error = http_status_error()
    broker = ThreeEntryBroker(recorder, error)
    caught = None
    try:
        await run_lists(signals(), **entry_args(recorder, broker))
    except Exception as exc:
        caught = exc
    assert caught is error
    assert [r.status for r in recorder.rows.values()] == [
        "sending",
        "planned",
        "planned",
    ]
    assert [c.symbol for c in broker.calls] == ["COIN0USDT"]


def test_http_status_cli_uncertain_returns_nonzero(monkeypatch, capsys):
    from scripts import mock_strategy_crypto

    async def execute(_args):
        return {
            "orders": [
                {
                    "status": "uncertain",
                    "submitted": None,
                    "error_class": "HTTPStatusError",
                }
            ]
        }

    monkeypatch.setattr(mock_strategy_crypto, "execute", execute)
    assert (
        mock_strategy_crypto.main(
            ["run", "--venue", "binance_spot_demo", "--asof", ASOF.isoformat()]
        )
        == 1
    )
    assert '"status": "uncertain"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_http_status_close_remains_unresolved():
    class CloseErrorBroker(Broker):
        async def submit(self, order, *, confirm):
            if order.side == "sell":
                self.calls.append(order)
                raise http_status_error()
            return await super().submit(order, confirm=confirm)

        async def evidence(self, order):
            if order.side == "sell":
                raise AssertionError(
                    "close evidence must not be queried after submit failure"
                )
            return await super().evidence(order)

    recorder = Recorder()
    broker = CloseErrorBroker(recorder)
    await run_lists(signals(), **entry_args(recorder, broker))
    result = await close_due(
        venue="binance_spot_demo",
        asof=DECISION + timedelta(days=3),
        broker=broker,
        recorder=recorder,
        dry_run=False,
        confirm=True,
    )
    assert all(
        row["status"] == "unresolved" and row["error_class"] == "HTTPStatusError"
        for row in result["orders"]
    )
    assert all(
        row.status == "uncertain"
        for row in recorder.rows.values()
        if row.side == "sell"
    )
