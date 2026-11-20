"""hk 5441 — ``scripts/execution_intent_upbit_resolve.py`` (design B4, B30).

What the script treats as evidence, and what it refuses to treat as evidence.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.execution_intent_gateway.order_evidence import parse_order_reading
from scripts import execution_intent_upbit_resolve as resolve

pytestmark = pytest.mark.unit

SOURCE = Path(resolve.__file__).read_text("utf-8")


def _reader(result: Any):
    calls: list[tuple[str | None, str | None]] = []

    async def read(order_uuid: str | None, identifier: str | None) -> Any:
        calls.append((order_uuid, identifier))
        if isinstance(result, Exception):
            raise result
        return result

    return read, calls


ORDER = {
    "uuid": "uuid-1",
    "state": "cancel",
    "executed_volume": "4.5",
    "remaining_volume": "5.5",
}


@pytest.mark.asyncio
async def test_a_returned_order_is_a_reading() -> None:
    read, calls = _reader(ORDER)
    reading, problem = await resolve._reading(read, "uuid-1", "child-0001")
    assert problem is None and reading is not None
    assert (reading.order_uuid, reading.state, reading.final) == (
        "uuid-1",
        "cancel",
        True,
    )
    assert reading.executed_volume == Decimal("4.5")
    assert calls == [("uuid-1", "child-0001")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "problem"),
    [
        (None, "not_found_or_unreadable"),
        ({}, "not_found_or_unreadable"),
        ({"error": {"name": "order_not_found"}}, "not_found_or_unreadable"),
        (RuntimeError("timeout"), "read_failed:RuntimeError"),
        ({**ORDER, "uuid": "uuid-other"}, "uuid_mismatch"),
    ],
)
async def test_b4_not_found_and_a_failed_read_are_not_evidence(result, problem) -> None:
    read, _calls = _reader(result)
    reading, found = await resolve._reading(read, "uuid-1", "child-0001")
    assert reading is None and found == problem


@pytest.mark.parametrize(
    "body",
    [
        {**ORDER, "state": "unknown"},
        {**ORDER, "executed_volume": None},
        {**ORDER, "executed_volume": "-1"},
        {**ORDER, "executed_volume": "NaN"},
        {**ORDER, "uuid": ""},
        "text",
        [ORDER],
    ],
)
def test_an_incomplete_order_body_proves_nothing(body: Any) -> None:
    assert parse_order_reading(body) is None


def test_an_open_order_is_read_but_is_not_final() -> None:
    reading = parse_order_reading({**ORDER, "state": "wait", "executed_volume": "0"})
    assert reading is not None and reading.final is False


def test_the_script_previews_by_default() -> None:
    assert resolve._parser().parse_args([]).commit is False
    assert resolve._parser().parse_args(["--commit"]).commit is True


def test_the_script_calls_no_order_writing_function() -> None:
    called = {
        node.func.attr
        if isinstance(node.func, ast.Attribute)
        else getattr(node.func, "id", "")
        for node in ast.walk(ast.parse(SOURCE))
        if isinstance(node, ast.Call)
    }
    forbidden = {
        "place_buy_order",
        "place_sell_order",
        "place_market_buy_order",
        "place_market_sell_order",
        "cancel_orders",
        "cancel_and_reorder",
        "_place_order_impl",
        "cancel_order_impl",
        "modify_order_impl",
    }
    assert not called & forbidden
    # The journal is never updated from here: evidence sits beside it.
    assert "ExecutionChildRequest" not in SOURCE


def test_only_the_two_order_reads_are_imported_from_the_broker() -> None:
    imported = {
        alias.name
        for node in ast.walk(ast.parse(SOURCE))
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.services.brokers.upbit.orders"
        for alias in node.names
    }
    assert imported == {"fetch_order_by_identifier", "fetch_order_detail"}
