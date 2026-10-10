"""The watcher's heartbeat is the stamp ``record_nav`` already writes every tick."""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.binance_h5 import BinanceH5LaneState
from app.services.brokers.binance.h5.state import H5StateService
from tests.services.brokers.binance.h5.fake_state_db import (
    NOW,
    FakeStore,
    lane_values,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]


def test_no_lane_row_means_no_runner_has_ticked():
    state = H5StateService(FakeStore().factory)
    assert asyncio.run(state.last_tick_at()) is None


def test_last_tick_is_the_lane_row_stamp():
    store = FakeStore()
    store.put(BinanceH5LaneState, lane_values(updated_at=NOW))
    assert asyncio.run(H5StateService(store.factory).last_tick_at()) == NOW


def test_every_record_nav_call_advances_the_stamp_the_watcher_reads():
    store = FakeStore()
    store.put(BinanceH5LaneState, lane_values(updated_at=NOW))
    state = H5StateService(store.factory)
    later = NOW + dt.timedelta(minutes=1)
    asyncio.run(state.record_nav(nav_usdt=Decimal("1000"), now=later))
    assert asyncio.run(state.last_tick_at()) == later
    latest = later + dt.timedelta(minutes=1)
    asyncio.run(state.record_nav(nav_usdt=Decimal("1001"), now=latest))
    assert asyncio.run(state.last_tick_at()) == latest


def test_last_tick_at_is_read_only():
    source = (REPO_ROOT / "app/services/brokers/binance/h5/state.py").read_text()
    tree = ast.parse(source)
    (method,) = [
        n
        for c in tree.body
        if isinstance(c, ast.ClassDef) and c.name == "H5StateService"
        for n in c.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "last_tick_at"
    ]
    attrs = {
        n.attr
        for statement in method.body
        for n in ast.walk(statement)
        if isinstance(n, ast.Attribute)
    }
    assert attrs <= {"_factory", "get", "updated_at"}, attrs


def test_run_tick_stamps_before_it_can_return_or_raise_past_the_account_read():
    """A tick that returns early still stamped: record_nav precedes every return."""
    tree = ast.parse(
        (REPO_ROOT / "app/services/brokers/binance/h5/executor.py").read_text()
    )
    (run_tick,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_tick"
    ]

    def calls(statement: ast.stmt) -> list[str]:
        return [
            n.func.attr
            for n in ast.walk(statement)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]

    stamp_index = next(
        i for i, s in enumerate(run_tick.body) if "record_nav" in calls(s)
    )
    before = run_tick.body[:stamp_index]
    assert not any(isinstance(n, ast.Return) for s in before for n in ast.walk(s))
    assert [c for s in before for c in calls(s)] == ["_guard", "read_account"]
