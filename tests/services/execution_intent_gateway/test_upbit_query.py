"""hk 5441 — the ``upbit_live`` query answers (design §1.5, S4, B20, B30, B34)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.execution_intent_gateway.decisions import ChildState
from app.services.execution_intent_gateway.upbit_query import (
    build_upbit_reconciliation,
    exact,
    filled_by_uuid,
    upbit_child_json,
    upbit_fill_orders,
    upbit_reconciliation_json,
)
from app.services.strategy_sleeves.intent_orders import (
    ChildOrderEvidence,
    IntentChildOrder,
)
from tests.services.execution_intent_gateway.upbit_support import NOW, child_row

pytestmark = pytest.mark.unit

INTENT = uuid4()


def order(
    key: str,
    *,
    child_id: int = 1,
    state: str = "accepted",
    uuid: str | None = "uuid-1",
    quantity: str = "10",
    evidence: tuple[str, str] | None = None,
) -> IntentChildOrder:
    return IntentChildOrder(
        child_id=child_id,
        intent_id=INTENT,
        idempotency_key=key,
        symbol="KRW-XRP",
        side="buy",
        execution_mode="shadow" if state == "shadowed" else "live",
        decision_ref="d",
        state=state,
        quantity=Decimal(quantity),
        limit_price=Decimal("3000"),
        broker_order_id=uuid,
        intent_created_at=NOW,
        created_at=NOW,
        finished_at=NOW,
        evidence=(
            None
            if evidence is None
            else ChildOrderEvidence(
                order_uuid=uuid or "uuid-found",
                broker_state=evidence[0],
                executed_volume=Decimal(evidence[1]),
                remaining_volume=None,
                observed_at=NOW,
            )
        ),
    )


def test_an_accepted_order_with_no_end_is_the_working_order() -> None:
    children = [child_row("k1")]
    answer = build_upbit_reconciliation(
        children, [order("k1")], {"uuid-1": Decimal("2.5")}
    )
    assert answer.reconciled is True
    assert answer.order is not None and answer.order.key == "k1"
    assert answer.filled_qty == Decimal("2.5")


def test_s4_a_not_sent_submit_is_never_a_working_order() -> None:
    lost = child_row(
        "k1",
        state=ChildState.NOT_SENT,
        broker_order_id=None,
        refusal_reason="account_mismatch",
    )
    refused = child_row(
        "k2",
        state=ChildState.REFUSED,
        broker_order_id=None,
        child_id=2,
        refusal_reason="price_off_tick",
    )
    answer = build_upbit_reconciliation([lost, refused], [], {})
    assert (answer.reconciled, answer.order, answer.filled_qty) == (
        True,
        None,
        Decimal("0"),
    )
    body = upbit_reconciliation_json(INTENT, [lost, refused], answer)
    assert body["view"] == {"filled_qty": "0"}
    assert [row["state"] for row in body["children"]] == ["not_sent", "refused"]


@pytest.mark.parametrize("state", [ChildState.SENDING, ChildState.UNCERTAIN])
def test_an_unknown_send_stays_unknown_without_evidence(state) -> None:
    child = child_row("k1", state=state, broker_order_id=None)
    answer = build_upbit_reconciliation(
        [child], [order("k1", state=state.value, uuid=None)], {}
    )
    assert answer.reconciled is False and answer.filled_qty is None
    assert upbit_reconciliation_json(INTENT, [child], answer)["view"] is None


def test_b3_evidence_found_by_identifier_resolves_an_unrecorded_send() -> None:
    child = child_row("k1", state=ChildState.SENDING, broker_order_id=None)
    found = order("k1", state="sending", uuid=None, evidence=("cancel", "4"))
    answer = build_upbit_reconciliation([child], [found], {"uuid-found": Decimal("4")})
    assert (answer.reconciled, answer.order, answer.filled_qty) == (
        True,
        None,
        Decimal("4"),
    )


def test_an_ended_order_is_not_working_and_keeps_its_ledger_fills() -> None:
    child = child_row("k1")
    ended = order("k1", evidence=("cancel", "3"))
    answer = build_upbit_reconciliation([child], [ended], {"uuid-1": Decimal("3")})
    assert answer.order is None and answer.filled_qty == Decimal("3")


def test_b30_an_unknown_cancel_is_settled_only_by_the_orders_own_evidence() -> None:
    sent = child_row("k1")
    unknown_cancel = child_row(
        "k2",
        state=ChildState.UNCERTAIN,
        action="cancel",
        quantity=None,
        price=None,
        target="k1",
        child_id=2,
    )
    children = [sent, unknown_cancel]

    open_answer = build_upbit_reconciliation(children, [order("k1")], {})
    assert open_answer.reconciled is False
    assert [item.idempotency_key for item in open_answer.unresolved] == ["k2"]

    ended = order("k1", evidence=("cancel", "0"))
    assert build_upbit_reconciliation(children, [ended], {}).reconciled is True


def test_an_accepted_cancel_closes_its_order() -> None:
    sent = child_row("k1")
    accepted_cancel = child_row(
        "k2", action="cancel", quantity=None, price=None, target="k1", child_id=2
    )
    answer = build_upbit_reconciliation([sent, accepted_cancel], [order("k1")], {})
    assert answer.reconciled is True and answer.order is None


def test_more_fills_than_the_order_held_are_not_believed() -> None:
    answer = build_upbit_reconciliation(
        [child_row("k1")], [order("k1")], {"uuid-1": Decimal("10.00000001")}
    )
    assert answer.reconciled is False
    assert answer.unresolved[0].reason.value == "fill_evidence_conflict"


def test_two_working_orders_are_not_reduced_to_one() -> None:
    children = [child_row("k1"), child_row("k2", child_id=2, broker_order_id="uuid-2")]
    orders = [order("k1"), order("k2", child_id=2, uuid="uuid-2")]
    answer = build_upbit_reconciliation(children, orders, {})
    assert answer.reconciled is False
    assert {item.reason.value for item in answer.unresolved} == {
        "multiple_working_orders"
    }


def test_a_shadowed_submit_is_a_virtual_fill_and_never_working() -> None:
    child = child_row("k1", state=ChildState.SHADOWED, broker_order_id=None)
    answer = build_upbit_reconciliation(
        [child], [order("k1", state="shadowed", uuid=None)], {}
    )
    assert (answer.reconciled, answer.order, answer.filled_qty) == (
        True,
        None,
        Decimal("10"),
    )


def test_b20_quantities_are_exact_decimal_strings() -> None:
    child = child_row("k1", quantity="0.00012345", price=150_000_000)
    answer = build_upbit_reconciliation(
        [child],
        [order("k1", quantity="0.00012345")],
        {"uuid-1": Decimal("0.00002000")},
    )
    body = upbit_reconciliation_json(INTENT, [child], answer)
    assert body["view"] == {
        "filled_qty": "0.00002",
        "order": {"key": "k1", "price": 150_000_000, "qty": "0.00012345"},
    }
    assert upbit_child_json(child)["quantity_decimal"] == "0.00012345"
    assert upbit_child_json(child)["quantity"] is None
    assert exact(Decimal("1E+1")) == "10" and exact(Decimal("0.10")) == "0.1"


def _ledger_row(
    row_id: int, uuid: str, qty: str, price: str, *, source: str = "reconciler"
):
    return SimpleNamespace(
        id=row_id,
        broker="upbit",
        account_mode="live",
        venue="upbit",
        instrument_type="crypto",
        symbol="KRW-XRP",
        raw_symbol=None,
        side="buy",
        currency="KRW",
        broker_order_id=uuid,
        source=source,
        filled_qty=Decimal(qty),
        filled_price=Decimal(price),
        filled_at=NOW + timedelta(seconds=row_id),
        created_at=NOW + timedelta(seconds=row_id + 1),
        quarantined_at=None,
    )


def test_b34_fills_carry_decimal_quantities_and_prices() -> None:
    rows = [
        _ledger_row(1, "uuid-1", "1.25000000", "3001.50000000"),
        _ledger_row(2, "uuid-1", "0.75000000", "3002.00000000"),
        _ledger_row(3, "uuid-stranger", "9", "1"),
    ]
    [entry] = upbit_fill_orders([order("k1")], rows)
    assert entry["broker_order_id"] == "uuid-1" and entry["cumulative_qty"] == "2"
    assert [fill["qty"] for fill in entry["fills"]] == ["1.25", "0.75"]
    assert entry["fills"][0]["price"] == "3001.50000000"
    assert filled_by_uuid([entry]) == {"uuid-1": Decimal("2")}


def test_a_quarantined_row_and_a_shadow_order_are_not_listed() -> None:
    quarantined = _ledger_row(1, "uuid-1", "5", "3000")
    quarantined.quarantined_at = NOW
    [entry] = upbit_fill_orders(
        [order("k1"), order("k2", child_id=2, state="shadowed", uuid=None)],
        [quarantined],
    )
    assert entry["cumulative_qty"] == "0" and entry["fills"] == []
