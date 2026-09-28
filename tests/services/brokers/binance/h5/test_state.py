"""Exercise the production state service against an in-memory async DB fake."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
from decimal import Decimal as D

import pytest
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Null,
)

from app.models.binance_h5 import BinanceH5Intent as Intent
from app.models.binance_h5 import BinanceH5LaneState as Lane
from app.models.binance_h5 import BinanceH5Signal as Row
from app.services.brokers.binance.h5.state import (
    H5OrderEvidence,
    H5StateBlocked,
    H5StateService,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS as BAR

NOW = dt.datetime(2026, 9, 28, 8, tzinfo=dt.UTC)


def signal(key="BTC", **changes):
    values = {
        "signal_key": key,
        "correlation_id": "binance-h5:" + key,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "decision_ts": 6 * BAR,
        "signal_price_text": "100.00",
        "state": "observed",
        "entry_client_order_id": None,
        "entry_nav_usdt": None,
        "entry_qty": D(0),
        "entry_price": None,
        "entered_at": None,
        "closed_qty": D(0),
        "realized_pnl_usdt": D(0),
        "fees_usdt": D(0),
        "exit_reason": None,
        "exit_at": None,
        "exit_bar_close_ts": None,
        "forecast_id": None,
        "forecast_resolved_at": None,
        "updated_at": NOW,
    }
    values.update(changes)
    return Row(**values)


def value(expr, row):
    if isinstance(expr, BindParameter):
        return expr.value
    if isinstance(expr, Null):
        return None
    if isinstance(expr, BooleanClauseList):
        vals = [value(c, row) for c in expr.clauses]
        return all(vals) if expr.operator is operators.and_ else any(vals)
    if isinstance(expr, BinaryExpression):
        left, right = value(expr.left, row), value(expr.right, row)
        op = expr.operator
        if op is operators.in_op:
            return left in right
        if op is operators.is_:
            return left is right
        if op is operators.eq:
            return left == right
        if left is None or right is None:
            return False
        return op(left, right)
    return getattr(row, expr.key)


class FakeDB:
    def __init__(self, rows):
        self.rows = {
            Row: {r.signal_key: r for r in rows},
            Intent: {},
            Lane: {
                1: Lane(
                    id=1,
                    day_kst=NOW.astimezone(dt.timezone(dt.timedelta(hours=9))).date(),
                    day_start_nav_usdt=D(1000),
                    peak_nav_usdt=D(1000),
                    last_nav_usdt=D(1000),
                    halt_reason=None,
                    last_decision_ts=None,
                )
            },
        }
        self.lock = asyncio.Lock()
        self.transaction = False

    async def __aenter__(self):
        if self.transaction:
            await self.lock.acquire()
            self.before = copy.deepcopy(self.rows)
        return self

    async def __aexit__(self, kind, *_):
        if self.transaction:
            if kind:
                self.rows = self.before
            self.transaction = False
            self.lock.release()

    def begin(self):
        self.transaction = True
        return self

    async def execute(self, stmt, params=None):
        assert "pg_advisory_xact_lock" in str(stmt)

    async def get(self, model, key, **kwargs):
        return self.rows[model].get(key)

    async def scalar(self, stmt):
        model = Intent if "FROM review.binance_h5_intents" in str(stmt) else Row
        candidates = [
            r
            for r in self.rows[model].values()
            if all(value(c, r) for c in stmt._where_criteria)
        ]
        text = str(stmt)
        if "count(" in text:
            return len(candidates)
        if "max(" in text:
            return max(
                (
                    r.exit_bar_close_ts
                    for r in candidates
                    if r.exit_bar_close_ts is not None
                ),
                default=None,
            )
        raise AssertionError(text)

    def add(self, row):
        if isinstance(row, Intent):
            row.broker_order_id = None
            row.broker_status = None
            row.avg_price = None
            self.rows[Intent][row.client_order_id] = row
        else:
            raise AssertionError(type(row))

    async def flush(self):
        pass


class Factory:
    def __init__(self, rows):
        self.db = FakeDB(rows)

    def __call__(self):
        # Separate context wrappers share state, with a transaction lock.
        return Session(self.db)


class Session:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def __getattr__(self, name):
        return getattr(self.db, name)


def reserve(service, key="BTC", count=6):
    return service.reserve_entry(
        signal_key=key,
        completed_bar_closes=tuple(i * BAR for i in range(1, count + 1)),
        entry_nav_usdt=D(1000),
        now=NOW,
    )


def test_five_bar_ban_then_sixth_permits():
    for count in (4, 5, 6):
        factory = Factory(
            [
                signal(decision_ts=count * BAR),
                signal("old", state="closed", exit_bar_close_ts=0),
            ]
        )
        service = H5StateService(factory)
        if count <= 5:
            with pytest.raises(H5StateBlocked, match="five"):
                asyncio.run(reserve(service, count=count))
        else:
            assert asyncio.run(reserve(service, count=count)).state == "entry_reserved"


@pytest.mark.parametrize("kind", ["third", "symbol", "sl", "daily", "mdd"])
def test_state_reservation_enforces_capacity_and_kills(kind):
    rows = [signal()]
    factory = Factory(rows)
    if kind in ("third", "symbol"):
        for i in range(2 if kind == "third" else 1):
            r = signal(
                str(i),
                state="holding",
                symbol="BTCUSDT" if kind == "symbol" else ("ETHUSDT", "SOLUSDT")[i],
            )
            factory.db.rows[Row][r.signal_key] = r
    if kind == "sl":
        for i in range(3):
            r = signal(str(i), state="closed", exit_reason="hard_stop", exit_at=NOW)
            factory.db.rows[Row][r.signal_key] = r
    if kind == "daily":
        factory.db.rows[Lane][1].last_nav_usdt = D(970)
    if kind == "mdd":
        factory.db.rows[Lane][1].last_nav_usdt = D(850)
    with pytest.raises(H5StateBlocked):
        asyncio.run(reserve(H5StateService(factory)))
    assert factory.db.rows[Row]["BTC"].state == "observed"


def test_concurrent_symbol_reservation_one_winner():
    factory = Factory([signal("a"), signal("b")])
    service = H5StateService(factory)

    async def both():
        return await asyncio.gather(
            reserve(service, "a"), reserve(service, "b"), return_exceptions=True
        )

    results = asyncio.run(both())
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, H5StateBlocked) for r in results) == 1


def test_restart_unknown_fence_and_partial_fill_evidence():
    factory = Factory([signal(state="entry_reserved")])
    service = H5StateService(factory)

    async def scenario():
        intent = await service.reserve_intent(
            signal_key="BTC",
            leg_key="entry",
            side="BUY",
            qty=D(1),
            reduce_only=False,
            now=NOW,
        )
        await service.fence_send(intent.client_order_id, now=NOW)
        restarted = H5StateService(factory)
        with pytest.raises(H5StateBlocked):
            await restarted.fence_send(intent.client_order_id, now=NOW)
        await restarted.mark_uncertain(intent.client_order_id, now=NOW)
        ev = H5OrderEvidence(
            intent.client_order_id,
            "broker-1",
            "BTCUSDT",
            "BUY",
            D(1),
            D(1),
            D(100),
            "FILLED",
            False,
            "BOTH",
        )
        held, settled = await restarted.apply_order_evidence(
            ev, broker_position_amt=D(1), exit_bar_close_ts=None, now=NOW
        )
        assert (
            held.state == "holding"
            and held.entry_qty == 1
            and settled.state == "evidenced"
        )
        assert (
            await restarted.settle_intent(intent.client_order_id, now=NOW)
        ).state == "settled"
        closing = await restarted.reserve_intent(
            signal_key="BTC",
            leg_key="tp1:0",
            side="SELL",
            qty=D(".5"),
            reduce_only=True,
            now=NOW,
        )
        await restarted.fence_send(closing.client_order_id, now=NOW)
        partial = H5OrderEvidence(
            closing.client_order_id,
            "broker-2",
            "BTCUSDT",
            "SELL",
            D(".5"),
            D(".2"),
            D(103),
            "PARTIALLY_FILLED",
            True,
            "BOTH",
        )
        held, pending = await restarted.apply_order_evidence(
            partial, broker_position_amt=D(".8"), exit_bar_close_ts=None, now=NOW
        )
        assert held.closed_qty == D(".2") and pending.state == "acknowledged"
        with pytest.raises(H5StateBlocked):
            await restarted.reserve_intent(
                signal_key="BTC",
                leg_key="hard_stop:.2",
                side="SELL",
                qty=D(".8"),
                reduce_only=True,
                now=NOW,
            )
        final = H5OrderEvidence(
            closing.client_order_id,
            "broker-2",
            "BTCUSDT",
            "SELL",
            D(".5"),
            D(".5"),
            D(103),
            "FILLED",
            True,
            "BOTH",
        )
        held, settled = await restarted.apply_order_evidence(
            final, broker_position_amt=D(".5"), exit_bar_close_ts=None, now=NOW
        )
        assert (
            held.closed_qty == D(".5")
            and held.realized_pnl_usdt == D("1.5")
            and settled.state == "evidenced"
        )
        assert (
            await restarted.settle_intent(closing.client_order_id, now=NOW)
        ).state == "settled"

    asyncio.run(scenario())
