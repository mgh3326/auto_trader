"""ROB-1461 Acceptance 3: the query reports each state and writes nothing."""

from __future__ import annotations

import ast
import copy
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.execution_intent_gateway_server import create_app
from app.routers import execution_intent_gateway as route
from app.services.execution_intent_gateway.decisions import (
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
)
from app.services.execution_intent_gateway.query import (
    LedgerEvidence,
    LedgerOrder,
    Reconciliation,
    UnresolvedReason,
    WorkingOrder,
    _lineages,
    build_reconciliation,
    executed_by_order_day,
    order_days_of,
)
from tests.services.execution_intent_gateway.support import (
    MemoryGatewayStore,
    intent_snapshot,
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "query-test-token"
HEADERS = {"X-Execution-Intent-Gateway-Token": TOKEN}
#: 09:30 KST on 2026-11-02.
SENT_AT = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
SENT_DAY = "20261102"
#: 23:00 KST on 2026-11-01. An intent can live 24 hours, so it can cross a day.
DAY_BEFORE_AT = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
DAY_BEFORE = "20261101"


def _child(
    child_id: int,
    key: str,
    state: ChildState,
    *,
    action: str = "submit",
    target: str | None = None,
    quantity: int | None = 10,
    price: int | None = 70_000,
    order_id: str | None = None,
    reason: str | None = None,
    result: dict[str, Any] | None = None,
    created_at: datetime = SENT_AT,
) -> ChildSnapshot:
    return ChildSnapshot(
        child_id=child_id,
        idempotency_key=key,
        request_digest=f"digest-{key}",
        action=action,
        target_idempotency_key=target,
        quantity=None if action == "cancel" else quantity,
        limit_price_krw=None if action == "cancel" else price,
        state=state,
        refusal_reason=reason,
        broker_order_id=order_id,
        result=result,
        created_at=created_at,
    )


def _ledger(
    *,
    order_no: str | None = "1001",
    status: str = "accepted",
    lifecycle: str = "accepted",
    quantity: int = 10,
    filled: int = 0,
    confirmed: bool | None = None,
) -> LedgerOrder:
    return LedgerOrder(
        order_no=order_no,
        status=status,
        lifecycle_state=lifecycle,
        quantity=quantity,
        filled_qty=filled,
        broker_cancel_confirmed=confirmed,
    )


SUBMIT_OK = _child(1, "key-00001", ChildState.ACCEPTED, order_id="1001")


# build_reconciliation: what each recorded state and each piece of evidence means.


def test_no_children_means_no_order_and_nothing_filled() -> None:
    assert build_reconciliation([], LedgerEvidence()) == Reconciliation(
        True, None, 0, ()
    )


def test_accepted_submit_is_the_working_order() -> None:
    result = build_reconciliation([SUBMIT_OK], LedgerEvidence())
    assert result == Reconciliation(True, WorkingOrder("key-00001", 70_000, 10), 0, ())


def test_refused_child_counts_as_nothing_sent() -> None:
    refused = _child(1, "key-00001", ChildState.REFUSED, reason="notional_cap_exceeded")
    assert build_reconciliation([refused], LedgerEvidence()) == Reconciliation(
        True, None, 0, ()
    )


@pytest.mark.parametrize("state", [ChildState.UNCERTAIN, ChildState.SENDING])
def test_unknown_outcome_without_evidence_stays_unresolved(state: ChildState) -> None:
    result = build_reconciliation([_child(1, "key-00001", state)], LedgerEvidence())
    assert result.reconciled is False
    assert result.order is None
    assert [(u.idempotency_key, u.state, u.reason) for u in result.unresolved] == [
        ("key-00001", state.value, UnresolvedReason.NO_LEDGER_EVIDENCE)
    ]


@pytest.mark.parametrize("state", [ChildState.UNCERTAIN, ChildState.SENDING])
def test_unknown_submit_with_an_accepted_ledger_row_is_working(
    state: ChildState,
) -> None:
    evidence = LedgerEvidence(orders_by_root={"key-00001": (_ledger(),)})
    result = build_reconciliation([_child(1, "key-00001", state)], evidence)
    assert result == Reconciliation(True, WorkingOrder("key-00001", 70_000, 10), 0, ())


def test_unknown_submit_with_a_rejected_ledger_row_is_closed() -> None:
    evidence = LedgerEvidence(
        orders_by_root={
            "key-00001": (
                _ledger(order_no=None, status="rejected", lifecycle="failed"),
            )
        }
    )
    result = build_reconciliation(
        [_child(1, "key-00001", ChildState.UNCERTAIN)], evidence
    )
    assert result == Reconciliation(True, None, 0, ())


def test_unknown_submit_with_an_unknown_ledger_row_stays_unresolved() -> None:
    evidence = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(order_no=None, status="unknown"),)}
    )
    result = build_reconciliation(
        [_child(1, "key-00001", ChildState.UNCERTAIN)], evidence
    )
    assert result.reconciled is False


def test_accepted_cancel_closes_the_order() -> None:
    children = [
        SUBMIT_OK,
        _child(
            2,
            "key-00002",
            ChildState.ACCEPTED,
            action="cancel",
            target="key-00001",
            order_id="1001",
        ),
    ]
    assert build_reconciliation(children, LedgerEvidence()) == Reconciliation(
        True, None, 0, ()
    )


@pytest.mark.parametrize("state", [ChildState.UNCERTAIN, ChildState.SENDING])
def test_unknown_cancel_stays_unresolved(state: ChildState) -> None:
    children = [
        SUBMIT_OK,
        _child(2, "key-00002", state, action="cancel", target="key-00001"),
    ]
    evidence = LedgerEvidence(orders_by_root={"key-00001": (_ledger(),)})
    result = build_reconciliation(children, evidence)
    assert result.reconciled is False
    assert [u.idempotency_key for u in result.unresolved] == ["key-00002"]


def test_unknown_amend_stays_unresolved_even_with_an_open_ledger_row() -> None:
    children = [
        SUBMIT_OK,
        _child(
            2, "key-00002", ChildState.UNCERTAIN, action="amend", target="key-00001"
        ),
    ]
    evidence = LedgerEvidence(orders_by_root={"key-00001": (_ledger(),)})
    assert build_reconciliation(children, evidence).reconciled is False


def test_accepted_amend_moves_the_working_order_to_its_key() -> None:
    children = [
        SUBMIT_OK,
        _child(
            2,
            "key-00002",
            ChildState.ACCEPTED,
            action="amend",
            target="key-00001",
            quantity=6,
            price=70_100,
            order_id="1002",
        ),
    ]
    result = build_reconciliation(children, LedgerEvidence())
    assert result.order == WorkingOrder("key-00002", 70_100, 6)


def test_soft_cancelled_ledger_row_is_not_proof_the_order_is_gone() -> None:
    evidence = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(lifecycle="cancelled", confirmed=False),)}
    )
    result = build_reconciliation([SUBMIT_OK], evidence)
    assert result.reconciled is False
    assert result.unresolved[0].reason is UnresolvedReason.LEDGER_STATE_UNRESOLVED


def test_broker_confirmed_cancel_in_the_ledger_closes_an_unknown_cancel() -> None:
    children = [
        SUBMIT_OK,
        _child(
            2, "key-00002", ChildState.UNCERTAIN, action="cancel", target="key-00001"
        ),
    ]
    evidence = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(lifecycle="cancelled", confirmed=True),)}
    )
    assert build_reconciliation(children, evidence) == Reconciliation(True, None, 0, ())


@pytest.mark.parametrize("lifecycle", ["anomaly", "stale", "reconciled", "planned"])
def test_unclear_ledger_state_stays_unresolved(lifecycle: str) -> None:
    evidence = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(lifecycle=lifecycle),)}
    )
    assert build_reconciliation([SUBMIT_OK], evidence).reconciled is False


def test_expired_ledger_row_closes_the_order() -> None:
    evidence = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(lifecycle="expired"),)}
    )
    assert build_reconciliation([SUBMIT_OK], evidence) == Reconciliation(
        True, None, 0, ()
    )


def test_two_ledger_rows_for_one_order_are_ambiguous() -> None:
    evidence = LedgerEvidence(orders_by_root={"key-00001": (_ledger(), _ledger())})
    result = build_reconciliation([SUBMIT_OK], evidence)
    assert result.unresolved[0].reason is UnresolvedReason.LEDGER_ROW_AMBIGUOUS


def test_partial_fill_keeps_the_order_and_reports_the_quantity() -> None:
    evidence = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(lifecycle="fill", filled=4),)}
    )
    result = build_reconciliation([SUBMIT_OK], evidence)
    assert result == Reconciliation(True, WorkingOrder("key-00001", 70_000, 10), 4, ())


def test_full_fill_closes_the_order() -> None:
    evidence = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(lifecycle="fill", filled=10),)}
    )
    assert build_reconciliation([SUBMIT_OK], evidence) == Reconciliation(
        True, None, 10, ()
    )


def test_filled_counts_every_order_id_in_the_lineage() -> None:
    children = [
        SUBMIT_OK,
        _child(
            2,
            "key-00002",
            ChildState.ACCEPTED,
            action="amend",
            target="key-00001",
            quantity=8,
            order_id="1002",
        ),
    ]
    evidence = LedgerEvidence(
        filled_by_order_day={
            ("1001", SENT_DAY): 2,
            ("1002", SENT_DAY): 1,
            ("9999", SENT_DAY): 7,
        }
    )
    assert build_reconciliation(children, evidence).filled_qty == 3


def test_filled_takes_the_larger_of_the_two_ledgers() -> None:
    evidence = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(lifecycle="fill", filled=2),)},
        filled_by_order_day={("1001", SENT_DAY): 5},
    )
    assert build_reconciliation([SUBMIT_OK], evidence).filled_qty == 5


def _reconcile_with_fills(
    children: list[ChildSnapshot],
    orders_by_root: dict[str, tuple[LedgerOrder, ...]],
    fills: list[tuple[str, int, str, datetime]],
) -> Reconciliation:
    """The reader's path without the database: days, then fills, then the fold."""
    days = order_days_of(_lineages(children), orders_by_root)
    return build_reconciliation(
        children,
        LedgerEvidence(
            orders_by_root=orders_by_root,
            filled_by_order_day=executed_by_order_day(fills, days),
        ),
    )


@pytest.mark.parametrize("source", ["websocket", "reconciler"])
def test_an_old_fill_under_a_reused_number_does_not_close_a_live_order(
    source: str,
) -> None:
    result = _reconcile_with_fills(
        [SUBMIT_OK],
        {"key-00001": (_ledger(),)},
        [("1001", 10, source, datetime(2026, 10, 30, 1, 0, tzinfo=UTC))],
    )
    assert result == Reconciliation(True, WorkingOrder("key-00001", 70_000, 10), 0, ())


def test_a_same_day_fill_still_closes_the_order() -> None:
    result = _reconcile_with_fills(
        [SUBMIT_OK],
        {"key-00001": (_ledger(),)},
        [("1001", 10, "reconciler", datetime(2026, 11, 2, 0, 31, tzinfo=UTC))],
    )
    assert result == Reconciliation(True, None, 10, ())


def test_an_old_fill_does_not_resolve_an_unknown_submit() -> None:
    unknown = _child(1, "key-00001", ChildState.UNCERTAIN)
    ledger_row = _ledger(status="pending", lifecycle="planned")
    result = _reconcile_with_fills(
        [unknown],
        {"key-00001": (ledger_row,)},
        [("1001", 10, "reconciler", datetime(2026, 10, 30, 1, 0, tzinfo=UTC))],
    )
    assert result.reconciled is False
    assert result.filled_qty == 0


def test_the_trading_day_boundary_is_kst_midnight() -> None:
    days = {("1001", SENT_DAY)}
    before_midnight = datetime(2026, 11, 1, 14, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    assert (
        executed_by_order_day([("1001", 3, "websocket", before_midnight)], days) == {}
    )
    assert executed_by_order_day([("1001", 3, "websocket", after_midnight)], days) == {
        ("1001", SENT_DAY): 3
    }


def _submit_day_before(order_id: str | None = "1001") -> ChildSnapshot:
    state = ChildState.ACCEPTED if order_id else ChildState.UNCERTAIN
    return _child(1, "key-00001", state, order_id=order_id, created_at=DAY_BEFORE_AT)


def _amend_today(
    state: ChildState = ChildState.ACCEPTED,
    order_id: str | None = "1002",
    quantity: int = 10,
) -> ChildSnapshot:
    return _child(
        2,
        "key-00002",
        state,
        action="amend",
        target="key-00001",
        order_id=order_id,
        quantity=quantity,
    )


def test_a_journaled_number_counts_only_on_the_day_its_child_was_sent() -> None:
    children = [_submit_day_before(), _amend_today()]
    assert order_days_of(_lineages(children), {}) == {
        ("1001", DAY_BEFORE),
        ("1002", SENT_DAY),
    }


def test_the_ledger_naming_a_journaled_number_adds_no_day() -> None:
    children = [_submit_day_before(), _amend_today()]
    days = order_days_of(
        _lineages(children), {"key-00001": (_ledger(order_no="1002"),)}
    )
    assert days == {("1001", DAY_BEFORE), ("1002", SENT_DAY)}


def test_an_old_fill_under_the_amends_number_does_not_close_it() -> None:
    children = [_submit_day_before(), _amend_today()]
    result = _reconcile_with_fills(
        children,
        {"key-00001": (_ledger(order_no="1002"),)},
        [("1002", 10, "reconciler", DAY_BEFORE_AT)],
    )
    assert result == Reconciliation(True, WorkingOrder("key-00002", 70_000, 10), 0, ())


def test_a_number_known_only_from_the_ledger_takes_the_unnumbered_senders_days() -> (
    None
):
    children = [_submit_day_before(None), _amend_today(ChildState.UNCERTAIN, None)]
    days = order_days_of(
        _lineages(children), {"key-00001": (_ledger(order_no="1002"),)}
    )
    assert days == {("1002", DAY_BEFORE), ("1002", SENT_DAY)}


def test_a_ledger_only_number_takes_only_the_unnumbered_senders_day() -> None:
    children = [_submit_day_before(), _amend_today(ChildState.UNCERTAIN, None)]
    days = order_days_of(
        _lineages(children), {"key-00001": (_ledger(order_no="1002"),)}
    )
    assert days == {("1001", DAY_BEFORE), ("1002", SENT_DAY)}


def test_a_ledger_number_no_sender_could_hold_counts_no_execution() -> None:
    days = order_days_of(
        _lineages([SUBMIT_OK]), {"key-00001": (_ledger(order_no="9999"),)}
    )
    assert days == {("1001", SENT_DAY)}


@pytest.mark.parametrize("state", [ChildState.ACCEPTED, ChildState.UNCERTAIN])
def test_a_cancel_gives_its_targets_number_no_new_day(state: ChildState) -> None:
    children = [
        _submit_day_before(),
        _child(
            2, "key-00002", state, action="cancel", target="key-00001", order_id="1001"
        ),
    ]
    assert order_days_of(_lineages(children), {}) == {("1001", DAY_BEFORE)}


def test_a_cancel_day_fill_under_a_reused_number_is_not_counted() -> None:
    children = [
        _submit_day_before(),
        _child(
            2,
            "key-00002",
            ChildState.UNCERTAIN,
            action="cancel",
            target="key-00001",
            order_id="1001",
        ),
    ]
    result = _reconcile_with_fills(
        children,
        {"key-00001": (_ledger(),)},
        [("1001", 10, "reconciler", SENT_AT)],
    )
    assert result.reconciled is False
    assert result.filled_qty == 0


def test_two_lineages_sharing_a_reused_number_keep_their_own_fills() -> None:
    children = [
        _submit_day_before(),
        _child(2, "key-00002", ChildState.ACCEPTED, order_id="1001"),
    ]
    result = _reconcile_with_fills(
        children, {}, [("1001", 10, "reconciler", DAY_BEFORE_AT)]
    )
    assert result == Reconciliation(True, WorkingOrder("key-00002", 70_000, 10), 10, ())


def test_one_lineage_reusing_a_number_sums_its_two_orders() -> None:
    children = [_submit_day_before(), _amend_today(order_id="1001")]
    result = _reconcile_with_fills(
        children,
        {},
        [
            ("1001", 2, "reconciler", DAY_BEFORE_AT),
            ("1001", 3, "reconciler", SENT_AT),
        ],
    )
    assert result.filled_qty == 5


def test_a_fill_for_an_order_with_no_established_day_is_not_counted() -> None:
    fill = ("9999", 10, "reconciler", SENT_AT)
    assert executed_by_order_day([fill], {("1001", SENT_DAY)}) == {}


# After an amend, closure looks only at the replacement order. The view's total
# stays cumulative: fillwire holds back sends with it.


_AMENDED = (
    _child(1, "key-00001", ChildState.ACCEPTED, order_id="1001"),
    _amend_today(quantity=6),
)
_AMENDED_LEDGER = {"key-00001": (_ledger(order_no="1002", quantity=6),)}
_AMENDED_WORKING = WorkingOrder("key-00002", 70_000, 6)


def _fills(*quantities: tuple[str, int]) -> list[tuple[str, int, str, datetime]]:
    return [(number, qty, "reconciler", SENT_AT) for number, qty in quantities]


def test_fills_on_the_replaced_order_do_not_close_the_amended_one() -> None:
    result = _reconcile_with_fills(
        list(_AMENDED), _AMENDED_LEDGER, _fills(("1001", 10))
    )
    assert result == Reconciliation(True, _AMENDED_WORKING, 10, ())


def test_old_and_new_fills_together_do_not_close_the_amended_order() -> None:
    result = _reconcile_with_fills(
        list(_AMENDED), _AMENDED_LEDGER, _fills(("1001", 4), ("1002", 2))
    )
    assert result == Reconciliation(True, _AMENDED_WORKING, 6, ())


def test_the_journal_alone_does_not_close_the_amended_order_either() -> None:
    result = _reconcile_with_fills(list(_AMENDED), {}, _fills(("1001", 4), ("1002", 2)))
    assert result == Reconciliation(True, _AMENDED_WORKING, 6, ())


@pytest.mark.parametrize("orders_by_root", [_AMENDED_LEDGER, {}])
def test_fills_on_the_replacement_itself_close_the_amended_order(
    orders_by_root: dict[str, tuple[LedgerOrder, ...]],
) -> None:
    result = _reconcile_with_fills(
        list(_AMENDED), orders_by_root, _fills(("1001", 4), ("1002", 6))
    )
    assert result == Reconciliation(True, None, 10, ())


def test_an_amended_ledger_rows_own_quantity_does_not_close_it() -> None:
    ledger_row = _ledger(order_no="1002", lifecycle="fill", quantity=6, filled=6)
    result = _reconcile_with_fills(list(_AMENDED), {"key-00001": (ledger_row,)}, [])
    assert result == Reconciliation(True, _AMENDED_WORKING, 6, ())


def test_old_fills_do_not_close_an_unknown_amend_the_ledger_followed() -> None:
    children = [_AMENDED[0], _amend_today(ChildState.UNCERTAIN, None, 6)]
    result = _reconcile_with_fills(children, _AMENDED_LEDGER, _fills(("1001", 6)))
    assert result.reconciled is False
    assert [u.idempotency_key for u in result.unresolved] == ["key-00002"]


def test_fills_on_the_replacement_close_an_unknown_amend_the_ledger_followed() -> None:
    children = [_AMENDED[0], _amend_today(ChildState.UNCERTAIN, None, 6)]
    result = _reconcile_with_fills(children, _AMENDED_LEDGER, _fills(("1002", 6)))
    assert result == Reconciliation(True, None, 6, ())


def test_a_cancel_after_an_amend_still_looks_at_the_replacement() -> None:
    children = [
        *_AMENDED,
        _child(
            3,
            "key-00003",
            ChildState.UNCERTAIN,
            action="cancel",
            target="key-00002",
            order_id="1002",
        ),
    ]
    closed = _reconcile_with_fills(children, _AMENDED_LEDGER, _fills(("1002", 6)))
    assert closed == Reconciliation(True, None, 6, ())
    held = _reconcile_with_fills(children, _AMENDED_LEDGER, _fills(("1001", 6)))
    assert held.reconciled is False


def test_two_working_orders_refuse_to_name_one() -> None:
    children = [
        SUBMIT_OK,
        _child(2, "key-00002", ChildState.ACCEPTED, order_id="1002"),
    ]
    result = build_reconciliation(children, LedgerEvidence())
    assert result.reconciled is False
    assert {u.reason for u in result.unresolved} == {
        UnresolvedReason.MULTIPLE_WORKING_ORDERS
    }


def test_a_closed_lineage_does_not_hide_an_unknown_one() -> None:
    children = [
        _child(1, "key-00001", ChildState.UNCERTAIN),
        _child(2, "key-00002", ChildState.ACCEPTED, order_id="1002"),
    ]
    result = build_reconciliation(children, LedgerEvidence())
    assert result.reconciled is False
    assert result.order is None


# The HTTP query, against a store that fails the test if anything writes.


class _ReadOnlyStore(MemoryGatewayStore):
    async def reserve(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a query must not reserve")

    async def finish(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a query must not finish")


class _Evidence:
    def __init__(self, evidence: LedgerEvidence | None = None) -> None:
        self.evidence = evidence or LedgerEvidence()

    async def read(
        self, intent: IntentSnapshot, children: Sequence[ChildSnapshot]
    ) -> LedgerEvidence:
        return self.evidence


def _client(
    store: MemoryGatewayStore, evidence: LedgerEvidence | None = None
) -> TestClient:
    app = create_app()

    def no_gateway() -> None:
        raise AssertionError("a query must not build the gateway")

    app.dependency_overrides[route.get_intent_gateway] = no_gateway
    app.dependency_overrides[route.get_gateway_reader] = lambda: store
    app.dependency_overrides[route.get_evidence_reader] = lambda: _Evidence(evidence)
    return TestClient(app)


def _seed(*children: ChildSnapshot, account: str = "kis_mock") -> _ReadOnlyStore:
    intent = intent_snapshot(account=account)
    store = _ReadOnlyStore(intent)
    for child in children:
        store.children.append(child)
        store._intent_of_child[child.child_id] = intent.intent_id
    return store


@pytest.fixture(autouse=True)
def _route_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN", TOKEN)


@pytest.mark.parametrize(
    ("state", "order_id", "reason"),
    [
        (ChildState.ACCEPTED, "1001", None),
        (ChildState.REFUSED, None, "quantity_cap_exceeded"),
        (ChildState.UNCERTAIN, None, None),
        (ChildState.SENDING, None, None),
    ],
)
def test_child_query_returns_each_recorded_state(
    state: ChildState, order_id: str | None, reason: str | None
) -> None:
    store = _seed(
        _child(
            1,
            "key-00001",
            state,
            order_id=order_id,
            reason=reason,
            result={"success": state is ChildState.ACCEPTED},
        )
    )
    answer = _client(store).get(
        "/execution-intents/children/key-00001", headers=HEADERS
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["state"] == state.value
    assert body["broker_order_id"] == order_id
    assert body["refusal_reason"] == reason
    assert body["result"] == {"success": state is ChildState.ACCEPTED}
    assert body["intent_id"] == str(next(iter(store.intents)))


@pytest.mark.parametrize(
    ("state", "order_id", "reconciled"),
    [
        (ChildState.ACCEPTED, "1001", True),
        (ChildState.REFUSED, None, True),
        (ChildState.UNCERTAIN, None, False),
        (ChildState.SENDING, None, False),
    ],
)
def test_intent_query_returns_each_recorded_state(
    state: ChildState, order_id: str | None, reconciled: bool
) -> None:
    store = _seed(_child(1, "key-00001", state, order_id=order_id))
    intent_id = next(iter(store.intents))
    answer = _client(store).get(
        f"/execution-intents/intents/{intent_id}", headers=HEADERS
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["children"][0]["state"] == state.value
    assert body["children"][0]["broker_order_id"] == order_id
    assert body["reconciled"] is reconciled


def test_view_matches_fillwire_exec_view_when_reconciled() -> None:
    store = _seed(SUBMIT_OK)
    intent_id = next(iter(store.intents))
    body = (
        _client(store)
        .get(f"/execution-intents/intents/{intent_id}", headers=HEADERS)
        .json()
    )
    assert body["view"] == {
        "filled_qty": 0,
        "order": {"key": "key-00001", "price": 70_000, "qty": 10},
    }


def test_view_is_null_when_any_outcome_is_unknown() -> None:
    store = _seed(_child(1, "key-00001", ChildState.UNCERTAIN))
    intent_id = next(iter(store.intents))
    body = (
        _client(store)
        .get(f"/execution-intents/intents/{intent_id}", headers=HEADERS)
        .json()
    )
    assert body["view"] is None
    assert body["unresolved"] == [
        {
            "idempotency_key": "key-00001",
            "state": "uncertain",
            "reason": "no_ledger_evidence",
        }
    ]


def test_a_partial_fill_total_is_not_reported_without_a_view() -> None:
    store = _seed(SUBMIT_OK)
    intent_id = next(iter(store.intents))
    ambiguous = LedgerEvidence(
        orders_by_root={"key-00001": (_ledger(filled=2), _ledger())},
        filled_by_order_day={("1001", SENT_DAY): 2},
    )
    body = (
        _client(store, ambiguous)
        .get(f"/execution-intents/intents/{intent_id}", headers=HEADERS)
        .json()
    )
    assert body["view"] is None
    assert "filled_qty" not in body


def test_no_working_order_omits_the_order_field() -> None:
    store = _seed()
    intent_id = next(iter(store.intents))
    body = (
        _client(store)
        .get(f"/execution-intents/intents/{intent_id}", headers=HEADERS)
        .json()
    )
    assert body["view"] == {"filled_qty": 0}


def test_query_writes_nothing() -> None:
    store = _seed(SUBMIT_OK, _child(2, "key-00002", ChildState.UNCERTAIN))
    before = (copy.deepcopy(store.children), list(store.events))
    intent_id = next(iter(store.intents))
    client = _client(store)
    for _ in range(3):
        client.get(f"/execution-intents/intents/{intent_id}", headers=HEADERS)
        client.get("/execution-intents/children/key-00002", headers=HEADERS)
    assert (store.children, store.events) == before


@pytest.mark.parametrize(
    "path",
    [
        "/execution-intents/children/unknown-key",
        "/execution-intents/intents/00000000-0000-4000-8000-000000000000",
    ],
)
def test_unknown_key_or_intent_is_404(path: str) -> None:
    assert _client(_seed()).get(path, headers=HEADERS).status_code == 404


def test_non_mock_intent_is_not_reported() -> None:
    store = _seed(SUBMIT_OK, account="kis_live")
    intent_id = next(iter(store.intents))
    answer = _client(store).get(
        f"/execution-intents/intents/{intent_id}", headers=HEADERS
    )
    assert answer.status_code == 404


def test_a_failed_read_is_503_and_never_a_guess() -> None:
    store = _seed(SUBMIT_OK)
    intent_id = next(iter(store.intents))

    class _Broken:
        async def read(self, *args: Any) -> LedgerEvidence:
            raise RuntimeError("secret detail")

    client = _client(store)
    client.app.dependency_overrides[route.get_evidence_reader] = lambda: _Broken()  # type: ignore[attr-defined]
    answer = client.get(f"/execution-intents/intents/{intent_id}", headers=HEADERS)
    assert answer.status_code == 503
    assert answer.json() == {"detail": "query_unavailable"}


def test_the_query_module_holds_no_write_or_broker_call() -> None:
    tree = ast.parse(
        (ROOT / "app/services/execution_intent_gateway/query.py").read_text("utf-8")
    )
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }
    assert not names & {
        "insert",
        "update",
        "delete",
        "commit",
        "flush",
        "reserve",
        "finish",
        "KISClient",
    }
