"""Fake transactional table store; no engine, socket, or SQL execution."""

from __future__ import annotations

import copy
import datetime as dt
import operator
import threading
from decimal import Decimal

from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList, Null

from app.models.binance_h5 import (
    BinanceH5NavSample,
    BinanceH5Signal,
)

NOW = dt.datetime(2026, 9, 28, 4, tzinfo=dt.UTC)


def signal_values(key="s1", symbol="BTCUSDT", **changes):
    values = {
        "signal_key": key,
        "correlation_id": f"binance-h5:{key}",
        "symbol": symbol,
        "side": "BUY",
        "decision_ts": int(NOW.timestamp() * 1000),
        "signal_price_text": "100.00",
        "state": "observed",
        "entry_client_order_id": None,
        "entry_nav_usdt": None,
        "entry_qty": Decimal(0),
        "entry_price": None,
        "entered_at": None,
        "closed_qty": Decimal(0),
        "realized_pnl_usdt": Decimal(0),
        "fees_usdt": Decimal(0),
        "exit_reason": None,
        "exit_at": None,
        "exit_bar_close_ts": None,
        "forecast_id": None,
        "forecast_resolved_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return values


def lane_values(**changes):
    values = {
        "id": 1,
        "day_kst": NOW.date(),
        "day_start_nav_usdt": Decimal("1000"),
        "peak_nav_usdt": Decimal("1000"),
        "last_nav_usdt": Decimal("1000"),
        "day_entry_halted": False,
        "halt_reason": None,
        "last_decision_ts": None,
        "updated_at": NOW,
    }
    values.update(changes)
    return values


def intent_values(cid="h5-one", key="s1", **changes):
    values = {
        "client_order_id": cid,
        "signal_key": key,
        "leg_key": "entry",
        "side": "BUY",
        "qty": Decimal("1"),
        "reduce_only": False,
        "state": "sending",
        "broker_order_id": None,
        "broker_status": None,
        "executed_qty": Decimal(0),
        "avg_price": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return values


def _value(expr, row):
    if isinstance(expr, BindParameter):
        return expr.value
    if isinstance(expr, Null):
        return None
    if isinstance(expr, BooleanClauseList):
        values = [_value(item, row) for item in expr.clauses]
        return all(values) if expr.operator is operators.and_ else any(values)
    if hasattr(expr, "left") and hasattr(expr, "right"):
        left, right = _value(expr.left, row), _value(expr.right, row)
        op = expr.operator
        if op is operators.in_op:
            return left in right
        if op is operators.is_:
            return left is right
        if op in {operator.ge, operator.le, operator.gt, operator.lt} and (
            left is None or right is None
        ):
            return False
        return op(left, right)
    if hasattr(expr, "key"):
        return row.get(expr.key)
    return expr


class _Transaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, value, tb):
        try:
            if kind is None:
                self.session.commit_rows()
        finally:
            if self.session.locked:
                self.session.store.lock.release()
                self.session.locked = False


class FakeStore:
    def __init__(self, data=None, lock=None):
        self.data = data if data is not None else {}
        self.lock = lock if lock is not None else threading.RLock()

    def put(self, model, values):
        key = tuple(values[col.key] for col in model.__table__.primary_key.columns)
        self.data[(model.__tablename__, key)] = copy.deepcopy(values)

    def values(self, model):
        return [
            dict(value)
            for (table, _), value in self.data.items()
            if table == model.__tablename__
        ]

    def factory(self):
        return FakeSession(self)


class FakeSession:
    def __init__(self, store):
        self.store = store
        self.local = {}
        self.locked = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def begin(self):
        return _Transaction(self)

    async def execute(self, statement, params=None):
        if str(statement).startswith("SELECT pg_advisory_xact_lock"):
            assert params == {"key": 847005}
            self.store.lock.acquire()
            self.locked = True
            return None
        # The service's PostgreSQL insert is interpreted, never executed.
        model = {
            "binance_h5_signals": BinanceH5Signal,
            "binance_h5_nav_samples": BinanceH5NavSample,
        }[statement.table.name]
        values = statement.compile().params
        key = tuple(values[col.key] for col in model.__table__.primary_key.columns)
        if (model.__tablename__, key) not in self.store.data:
            self.add(
                model(
                    **(signal_values(**values) if model is BinanceH5Signal else values)
                )
            )
        return None

    async def get(self, model, key, **kwargs):
        keys = key if isinstance(key, tuple) else (key,)
        locator = (model.__tablename__, keys)
        if locator not in self.local:
            values = self.store.data.get(locator)
            if values is None:
                return None
            self.local[locator] = model(**dict(values))
        return self.local[locator]

    def add(self, row):
        key = tuple(getattr(row, col.key) for col in row.__table__.primary_key.columns)
        self.local[(row.__tablename__, key)] = row

    async def flush(self):
        return None

    async def scalar(self, query):
        table = query.get_final_froms()[0].name
        rows = [
            dict(value) for (name, _), value in self.store.data.items() if name == table
        ]
        rows = [
            row
            for row in rows
            if all(_value(expr, row) for expr in query._where_criteria)
        ]
        expr = next(iter(query.selected_columns))
        if expr.name == "count":
            return len(rows)
        if expr.name == "max":
            column = next(iter(expr.clauses)).key
            return max(
                (row[column] for row in rows if row[column] is not None), default=None
            )
        raise AssertionError(f"unhandled fake query {expr.name}")

    def commit_rows(self):
        for locator, row in self.local.items():
            self.store.data[locator] = {
                column.key: getattr(row, column.key) for column in row.__table__.columns
            }
