"""hk 6066 (6033 K2): the read-only ``exposure`` block of ``GET /intents/<id>``.

The block reports what the cap rules already count. These tests pin that it never
disagrees with them, that a confirmed cancel is evidence and not a release (D-4),
and that every field the route answered before is still answered unchanged.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.execution_intent_gateway_server import create_app
from app.routers import execution_intent_gateway as route
from app.schemas.execution_intent import SubmitChild
from app.services.execution_intent_gateway.decisions import (
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    Refusal,
    _exposure_by_order,
    decide,
)
from app.services.execution_intent_gateway.exposure import (
    RELEASE,
    build_kis_exposure,
    build_upbit_exposure,
)
from app.services.execution_intent_gateway.query import (
    LedgerEvidence,
    LedgerOrder,
    build_reconciliation,
    reconciliation_json,
)
from app.services.execution_intent_gateway.upbit_decisions import sent_exposure
from app.services.execution_intent_gateway.upbit_query import (
    build_upbit_reconciliation,
    filled_by_uuid,
    upbit_reconciliation_json,
)
from app.services.kis_mock_runner.control import KillMode
from app.services.strategy_sleeves.intent_orders import (
    ChildOrderEvidence,
    IntentChildOrder,
)
from tests.services.execution_intent_gateway.support import (
    MemoryGatewayStore,
    intent_snapshot,
)
from tests.services.execution_intent_gateway.test_intent_query import (
    SENT_AT,
    SENT_DAY,
    _child,
    _ledger,
)
from tests.services.execution_intent_gateway.upbit_support import (
    NOW,
    child_row,
    upbit_intent,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "exposure-test-token"
HEADERS = {"X-Execution-Intent-Gateway-Token": TOKEN}
SUBMIT = _child(1, "key-00001", ChildState.ACCEPTED, order_id="1001")


def _evidence(
    ledger: LedgerOrder | None, *, filled: int = 0, root: str = "key-00001"
) -> LedgerEvidence:
    return LedgerEvidence(
        orders_by_root={} if ledger is None else {root: (ledger,)},
        filled_by_order_day={("1001", SENT_DAY): filled} if filled else {},
    )


# kis_mock: counts


def test_counts_are_the_cap_functions_own_high_water_figures() -> None:
    children = [
        SUBMIT,
        _child(
            2,
            "key-00002",
            ChildState.ACCEPTED,
            action="amend",
            target="key-00001",
            quantity=4,
            price=71_000,
            order_id="1001",
        ),
        _child(3, "key-00003", ChildState.REFUSED, quantity=9, reason="x"),
        _child(4, "key-00004", ChildState.UNCERTAIN, quantity=2, price=60_000),
    ]
    intent = intent_snapshot()
    block = build_kis_exposure(intent, children, LedgerEvidence())
    by_order = _exposure_by_order(children)
    assert block["counted"] == {
        "quantity": sum(q for q, _ in by_order.values()),
        "notional_krw": sum(n for _, n in by_order.values()),
    }
    assert [
        (
            o["root_idempotency_key"],
            o["high_water_quantity"],
            o["high_water_notional_krw"],
        )
        for o in block["orders"]
    ] == [(root, q, n) for root, (q, n) in by_order.items()]
    # The amend kept the higher quantity and the higher notional of the two.
    assert by_order["key-00001"] == (10, 700_000)
    assert block["cap"] == {
        "max_quantity": 10,
        "max_notional_krw": 1_000_000,
        "max_single_notional_krw": block["cap"]["max_single_notional_krw"],
    }
    assert block["remaining"] == {
        "quantity": max(0, 10 - block["counted"]["quantity"]),
        "notional_krw": max(0, 1_000_000 - block["counted"]["notional_krw"]),
    }
    assert block["release"] == RELEASE == "none"


def test_no_children_means_nothing_counted_and_the_full_cap_remains() -> None:
    block = build_kis_exposure(intent_snapshot(), [], LedgerEvidence())
    assert block["counted"] == {"quantity": 0, "notional_krw": 0}
    assert block["remaining"] == {"quantity": 10, "notional_krw": 1_000_000}
    assert block["orders"] == []


def test_remaining_never_goes_below_zero() -> None:
    block = build_kis_exposure(
        intent_snapshot(max_quantity=3, max_notional_krw=100_000),
        [SUBMIT],
        LedgerEvidence(),
    )
    assert block["remaining"] == {"quantity": 0, "notional_krw": 0}


# kis_mock: confirmed cancel is evidence only (D-4)


def _cancel_of_submit() -> list[ChildSnapshot]:
    return [
        SUBMIT,
        _child(
            2,
            "key-00002",
            ChildState.ACCEPTED,
            action="cancel",
            target="key-00001",
            order_id="1001",
        ),
    ]


def test_a_confirmed_cancel_with_no_fill_is_reported_and_frees_nothing() -> None:
    children = _cancel_of_submit()
    ledger = _ledger(lifecycle="cancelled", status="cancelled", confirmed=True)
    block = build_kis_exposure(intent_snapshot(), children, _evidence(ledger))
    [order] = block["orders"]
    assert order["cancel_evidence"] == {
        "broker_terminal": "cancelled",
        "ledger_fill_qty": "0",
        "confirmed_cancel_no_fill": True,
        "reason": None,
    }
    # D-4: the cancelled order still counts in full, and the remaining is unchanged.
    assert block["counted"]["quantity"] == 10
    assert block["remaining"]["quantity"] == 0
    assert block["release"] == "none"


@pytest.mark.parametrize(
    ("ledger", "filled", "reason"),
    [
        (
            _ledger(lifecycle="cancelled", status="cancelled", confirmed=False),
            0,
            "broker_terminal_unproven",
        ),
        (
            _ledger(lifecycle="cancelled", status="cancelled", confirmed=None),
            0,
            "broker_terminal_unproven",
        ),
        (_ledger(lifecycle="accepted"), 0, "broker_terminal_unproven"),
        (
            _ledger(
                lifecycle="cancelled", status="cancelled", confirmed=True, filled=3
            ),
            3,
            "filled",
        ),
        (
            _ledger(
                lifecycle="cancelled", status="cancelled", confirmed=True, filled=5
            ),
            3,
            "fill_evidence_conflict",
        ),
        (_ledger(lifecycle="expired", status="accepted"), 0, "expired_not_cancel"),
    ],
)
def test_unproven_or_filled_cancels_are_not_confirmed(
    ledger: LedgerOrder, filled: int, reason: str
) -> None:
    block = build_kis_exposure(
        intent_snapshot(), _cancel_of_submit(), _evidence(ledger, filled=filled)
    )
    [order] = block["orders"]
    assert order["cancel_evidence"]["confirmed_cancel_no_fill"] is False
    assert order["cancel_evidence"]["reason"] == reason


def test_no_ledger_row_is_not_evidence_of_a_cancel() -> None:
    block = build_kis_exposure(intent_snapshot(), _cancel_of_submit(), LedgerEvidence())
    [order] = block["orders"]
    assert order["cancel_evidence"]["reason"] == "no_ledger_evidence"
    assert order["cancel_evidence"]["confirmed_cancel_no_fill"] is False


def test_two_ledger_rows_for_one_order_confirm_nothing() -> None:
    ledger = _ledger(lifecycle="cancelled", status="cancelled", confirmed=True)
    evidence = LedgerEvidence(orders_by_root={"key-00001": (ledger, ledger)})
    block = build_kis_exposure(intent_snapshot(), _cancel_of_submit(), evidence)
    assert block["orders"][0]["cancel_evidence"]["reason"] == "ledger_row_ambiguous"


# Cap decisions are the same with or without the block (acceptance 3)


def _decide(intent: IntentSnapshot, children: Sequence[ChildSnapshot], quantity: int):
    request = SubmitChild(
        action="submit",
        idempotency_key="key-new-01",
        intent_id=intent.intent_id,
        quantity=quantity,
        limit_price_krw=70_000,
    )
    return decide(
        request,
        intent=intent,
        children=children,
        now=SENT_AT,
        kill_mode=KillMode.ACTIVE,
        forwarder_accounts=frozenset({"kis_mock"}),
    )


def test_building_the_block_changes_no_cap_decision() -> None:
    intent = intent_snapshot(max_quantity=14, max_position_after=14)
    children = _cancel_of_submit()
    evidence = _evidence(
        _ledger(lifecycle="cancelled", status="cancelled", confirmed=True)
    )
    before = {q: _decide(intent, children, q) for q in range(1, 8)}
    snapshot = list(children)
    block = build_kis_exposure(intent, children, evidence)
    after = {q: _decide(intent, children, q) for q in range(1, 8)}
    assert before == after
    assert children == snapshot
    # remaining names exactly the boundary the cap check enforces, with the
    # confirmed-cancel order still counted: 4 more fit, 5 do not.
    assert block["remaining"]["quantity"] == 4
    assert before[4].refusal is None
    assert before[5].refusal is Refusal.QUANTITY_CAP_EXCEEDED


# kis_mock: existing fields are byte-identical


def test_kis_route_keeps_every_existing_field_and_adds_exposure() -> None:
    intent = intent_snapshot()
    children = _cancel_of_submit()
    evidence = _evidence(
        _ledger(lifecycle="cancelled", status="cancelled", confirmed=True)
    )
    store = MemoryGatewayStore(intent)
    for child in children:
        store.children.append(child)
        store._intent_of_child[child.child_id] = intent.intent_id

    class _Reader:
        async def read(self, *_: Any) -> LedgerEvidence:
            return evidence

    client = _client(store, evidence_reader=_Reader())
    answer = client.get(
        f"/execution-intents/intents/{intent.intent_id}", headers=HEADERS
    )
    assert answer.status_code == 200
    body = answer.json()
    expected = reconciliation_json(
        intent.intent_id, children, build_reconciliation(children, evidence)
    )
    assert {k: v for k, v in body.items() if k != "exposure"} == expected
    assert list(body)[: len(expected)] == list(expected)
    assert body["exposure"] == build_kis_exposure(intent, children, evidence)


# upbit_live


def _order(
    key: str,
    *,
    child_id: int = 1,
    uuid: str | None = "uuid-1",
    quantity: str = "10",
    evidence: tuple[str, str] | None = None,
    mode: str = "live",
) -> IntentChildOrder:
    return IntentChildOrder(
        child_id=child_id,
        intent_id=upbit_intent().intent_id,
        idempotency_key=key,
        symbol="KRW-XRP",
        side="buy",
        execution_mode=mode,
        decision_ref="d",
        state="accepted",
        quantity=Decimal(quantity),
        limit_price=Decimal("3000"),
        broker_order_id=uuid,
        intent_created_at=NOW,
        created_at=NOW,
        finished_at=NOW,
        evidence=None
        if evidence is None
        else ChildOrderEvidence(
            order_uuid=uuid or "uuid-found",
            broker_state=evidence[0],
            executed_volume=Decimal(evidence[1]),
            remaining_volume=None,
            observed_at=NOW,
        ),
    )


def test_upbit_counts_are_sent_exposure_with_exact_decimals() -> None:
    children = [
        child_row("k1", quantity="10.5", price=3_000, child_id=1),
        child_row(
            "k2", quantity="1", price=3_000, child_id=2, broker_order_id="uuid-2"
        ),
        child_row(
            "k3",
            state=ChildState.NOT_SENT,
            quantity="9",
            child_id=3,
            broker_order_id=None,
        ),
        child_row("k4", state=ChildState.REFUSED, quantity="9", child_id=4),
        child_row(
            "k5", action="cancel", quantity=None, price=None, child_id=5, target="k1"
        ),
    ]
    intent = upbit_intent()
    block = build_upbit_exposure(intent, children, [], {})
    quantity, notional = sent_exposure(children)
    assert (quantity, notional) == (Decimal("11.5"), Decimal("34500"))
    assert block["counted"] == {"quantity": "11.5", "notional_krw": "34500"}
    assert [o["root_idempotency_key"] for o in block["orders"]] == ["k1", "k2"]
    assert block["orders"][0]["high_water_quantity"] == "10.5"
    assert block["orders"][0]["high_water_notional_krw"] == "31500"
    assert block["cap"] == {"max_quantity": "160", "max_notional_krw": "480000"}
    assert block["remaining"] == {"quantity": "148.5", "notional_krw": "445500"}
    assert block["release"] == "none"


def test_an_upbit_sell_has_a_quantity_cap_only() -> None:
    block = build_upbit_exposure(upbit_intent(side="sell"), [child_row("k1")], [], {})
    assert block["cap"]["max_notional_krw"] is None
    assert block["remaining"]["notional_krw"] is None
    assert block["remaining"]["quantity"] == "150"


def test_an_upbit_cancelled_order_with_no_fill_is_evidence_and_frees_nothing() -> None:
    children = [child_row("k1")]
    block = build_upbit_exposure(
        upbit_intent(),
        children,
        [_order("k1", evidence=("cancel", "0"))],
        {"uuid-1": Decimal("0")},
    )
    [order] = block["orders"]
    assert order["cancel_evidence"] == {
        "broker_terminal": "cancel",
        "ledger_fill_qty": "0",
        "confirmed_cancel_no_fill": True,
        "reason": None,
    }
    assert block["counted"]["quantity"] == "10"
    assert block["remaining"]["quantity"] == "150"


@pytest.mark.parametrize(
    ("order", "ledger", "reason"),
    [
        (_order("k1", evidence=("cancel", "2")), "2", "filled"),
        (_order("k1", evidence=("cancel", "0")), "2", "filled"),
        (_order("k1", evidence=("done", "10")), "10", "filled"),
        (_order("k1", evidence=("done", "0")), "0", "ended_not_cancel"),
        (_order("k1", evidence=None), "0", "no_order_evidence"),
        (_order("k1", mode="shadow"), "0", "shadow_order"),
    ],
)
def test_upbit_unconfirmed_cancels(
    order: IntentChildOrder, ledger: str, reason: str
) -> None:
    block = build_upbit_exposure(
        upbit_intent(), [child_row("k1")], [order], {"uuid-1": Decimal(ledger)}
    )
    evidence = block["orders"][0]["cancel_evidence"]
    assert evidence["confirmed_cancel_no_fill"] is False
    assert evidence["reason"] == reason


def test_upbit_open_order_has_no_terminal_proof() -> None:
    block = build_upbit_exposure(upbit_intent(), [child_row("k1")], [_order("k1")], {})
    evidence = block["orders"][0]["cancel_evidence"]
    assert evidence["reason"] == "no_order_evidence"
    assert evidence["broker_terminal"] is None


def test_upbit_route_keeps_every_existing_field_and_adds_exposure() -> None:
    intent = upbit_intent()
    children = [child_row("k1")]
    orders = [_order("k1", evidence=("cancel", "0"))]

    class _Store(MemoryGatewayStore):
        async def read_intent_orders(
            self, _intent: IntentSnapshot
        ) -> list[IntentChildOrder]:
            return orders

    class _Fills:
        async def read_fills(self, *_: Any) -> list[dict[str, Any]]:
            return [{"broker_order_id": "uuid-1", "cumulative_qty": "0", "fills": []}]

    store = _Store(intent)
    store.children.extend(children)
    for child in children:
        store._intent_of_child[child.child_id] = intent.intent_id
    client = _client(store, fills_reader=_Fills(), upbit_store=store)
    answer = client.get(
        f"/execution-intents/intents/{intent.intent_id}", headers=HEADERS
    )
    assert answer.status_code == 200
    body = answer.json()
    fills = [{"broker_order_id": "uuid-1", "cumulative_qty": "0", "fills": []}]
    expected = upbit_reconciliation_json(
        intent.intent_id,
        children,
        build_upbit_reconciliation(children, orders, filled_by_uuid(fills)),
    )
    assert {k: v for k, v in body.items() if k != "exposure"} == expected
    assert list(body)[: len(expected)] == list(expected)
    assert body["exposure"] == build_upbit_exposure(
        intent, children, orders, filled_by_uuid(fills)
    )


# Boundaries


def test_the_block_is_pure_and_the_decision_modules_never_import_it() -> None:
    base = ROOT / "app/services/execution_intent_gateway"
    tree = ast.parse((base / "exposure.py").read_text("utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    for name in imported:
        assert not any(
            part in name for part in ("sqlalchemy", "httpx", "app.core.db")
        ), name
        assert not name.endswith(
            ("forwarder", "_store", ".store", ".gateway", "upbit_gateway")
        ), name
    for decision_module in (
        "decisions.py",
        "upbit_decisions.py",
        "gateway.py",
        "upbit_gateway.py",
    ):
        source = (base / decision_module).read_text("utf-8")
        assert "execution_intent_gateway.exposure" not in source, decision_module


def _client(
    store: MemoryGatewayStore,
    *,
    evidence_reader: Any = None,
    fills_reader: Any = None,
    upbit_store: Any = None,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[route.get_gateway_reader] = lambda: store
    if evidence_reader is not None:
        app.dependency_overrides[route.get_evidence_reader] = lambda: evidence_reader
    if fills_reader is not None:
        app.dependency_overrides[route.get_fills_reader] = lambda: fills_reader
    if upbit_store is not None:
        app.dependency_overrides[route.get_upbit_store] = lambda: upbit_store
    return TestClient(app)


@pytest.fixture(autouse=True)
def _route_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN", TOKEN)
