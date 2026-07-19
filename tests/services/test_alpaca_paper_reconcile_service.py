"""Evidence-first booking for manually submitted Alpaca paper orders (ROB-953)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.alpaca.schemas import Order

TERMINAL_STATES = {
    "filled",
    "position_reconciled",
    "closed",
    "final_reconciled",
    "canceled",
    "anomaly",
}


@dataclass
class Row:
    id: int = 20
    client_order_id: str = "rob73-08ebbf8c64e2dd93"
    execution_symbol: str = "ISRG"
    lifecycle_state: str = "submitted"
    filled_qty: Decimal = Decimal("0")
    record_kind: str = "execution"
    cancel_status: str | None = None
    canceled_at: Any = None


class Ledger:
    """Fake ledger that persists through the *real* lifecycle derivation.

    Using the production mapping here is what makes the action-vs-persisted-state
    assertions meaningful: a hand-rolled fake mapping would re-introduce exactly
    the divergence these tests exist to catch.
    """

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.status_calls: list[dict[str, Any]] = []

    async def list_reconcile_candidates(
        self, limit: int = 100, symbol: str | None = None
    ) -> list[Row]:
        eligible = [
            row
            for row in self.rows
            if row.record_kind == "execution"
            and row.lifecycle_state not in TERMINAL_STATES
            and (symbol is None or row.execution_symbol == symbol)
        ]
        return eligible[:limit]

    async def get_execution_by_client_order_id(
        self, client_order_id: str
    ) -> Row | None:
        return next(
            (
                row
                for row in self.rows
                if row.client_order_id == client_order_id
                and row.record_kind == "execution"
            ),
            None,
        )

    async def record_status(
        self, client_order_id: str, order: dict[str, Any], **_: Any
    ) -> Row:
        from app.services.alpaca_paper_ledger_service import derive_lifecycle_state

        self.status_calls.append(order)
        row = next(
            r
            for r in self.rows
            if r.client_order_id == client_order_id and r.record_kind == "execution"
        )
        row.filled_qty = Decimal(str(order["filled_qty"]))
        row.lifecycle_state = derive_lifecycle_state(
            order["status"],
            row.filled_qty,
            has_cancel_evidence=row.cancel_status is not None
            or row.canceled_at is not None,
        )
        return row


class Broker:
    def __init__(self, order: Order | None) -> None:
        self.order = order
        self.fill_calls: list[dict[str, Any]] = []

    async def get_order_by_client_order_id(self, _: str) -> Order | None:
        return self.order

    async def list_fills(
        self,
        *,
        page_token: str | None = None,
        page_size: int = 100,
        direction: str = "desc",
    ) -> list[Any]:
        self.fill_calls.append(
            {
                "page_token": page_token,
                "page_size": page_size,
                "direction": direction,
            }
        )
        if page_token is not None or self.order is None:
            return []
        qty = Decimal(str(self.order.filled_qty or 0))
        if qty <= 0:
            return []
        return [
            _fill(
                self.order.id,
                str(qty),
                str(self.order.filled_avg_price or Decimal("353.156")),
                str(qty),
            )
        ]


def filled_order(*, status: str = "filled", qty: str = "0.014") -> Order:
    return Order(
        id="36caf22a-e305-4b19-8fdb-3fb88d57c589",
        client_order_id="rob73-08ebbf8c64e2dd93",
        symbol="ISRG",
        qty=Decimal("0.014"),
        filled_qty=Decimal(qty),
        filled_avg_price=Decimal("353.156"),
        side="buy",
        type="limit",
        time_in_force="day",
        status=status,
    )


@pytest.mark.asyncio
async def test_reconcile_books_isrg_broker_fill_as_absolute_ledger_status() -> None:
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row()])
    result = await AlpacaPaperReconcileService(
        ledger, Broker(filled_order())
    ).reconcile(dry_run=False)

    assert result["reconciled"][0]["action"] == "booked_filled"
    assert result["reconciled"][0]["transition_depth"] == "filled"
    assert ledger.rows[0].lifecycle_state == "filled"
    assert ledger.status_calls[0]["filled_qty"] == "0.014"


@pytest.mark.asyncio
async def test_reconcile_is_delta_idempotent_for_same_cumulative_evidence() -> None:
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row(filled_qty=Decimal("0.014"), lifecycle_state="submitted")])
    result = await AlpacaPaperReconcileService(
        ledger, Broker(filled_order())
    ).reconcile(dry_run=False)

    assert result["reconciled"][0]["action"] == "noop_already_booked"
    assert ledger.status_calls == []


@pytest.mark.asyncio
async def test_reconcile_missing_broker_evidence_fails_closed_for_manual_review() -> (
    None
):
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row()])
    result = await AlpacaPaperReconcileService(ledger, Broker(None)).reconcile(
        dry_run=False
    )

    assert result["reconciled"][0]["action"] == "noop_requires_manual_review"
    assert result["reconciled"][0]["requires_manual_review"] is True
    assert ledger.rows[0].lifecycle_state == "submitted"
    assert ledger.status_calls == []


@pytest.mark.asyncio
async def test_reconcile_open_order_without_fill_keeps_submitted() -> None:
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row()])
    result = await AlpacaPaperReconcileService(
        ledger, Broker(filled_order(status="accepted", qty="0"))
    ).reconcile(dry_run=False)

    assert result["reconciled"][0]["action"] == "noop_pending"
    assert ledger.rows[0].lifecycle_state == "submitted"


@pytest.mark.asyncio
async def test_reconcile_partial_fill_stays_submitted_without_early_terminal_booking() -> (
    None
):
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row()])
    order = filled_order(status="partially_filled", qty="0.007")
    order.qty = Decimal("0.014")
    result = await AlpacaPaperReconcileService(ledger, Broker(order)).reconcile(
        dry_run=False
    )

    assert result["reconciled"][0]["action"] == "booked_partial"
    assert ledger.rows[0].lifecycle_state == "submitted"


@pytest.mark.asyncio
async def test_reconcile_open_status_with_fill_books_anomaly_not_terminal_fill() -> (
    None
):
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row()])
    result = await AlpacaPaperReconcileService(
        ledger, Broker(filled_order(status="accepted"))
    ).reconcile(dry_run=False)

    assert result["reconciled"][0]["action"] == "booked_anomaly"
    assert ledger.rows[0].lifecycle_state == "anomaly"


@pytest.mark.asyncio
async def test_reconcile_dry_run_plans_without_a_ledger_write() -> None:
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row()])
    result = await AlpacaPaperReconcileService(
        ledger, Broker(filled_order())
    ).reconcile(dry_run=True)

    assert result["reconciled"][0]["action"] == "would_book_filled"
    assert ledger.status_calls == []


@pytest.mark.asyncio
async def test_reconcile_only_books_execution_row_and_rerun_is_noop() -> None:
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    rows = [
        Row(id=1, record_kind="plan", lifecycle_state="planned"),
        Row(id=2, record_kind="preview", lifecycle_state="previewed"),
        Row(id=3, record_kind="validation_attempt", lifecycle_state="validated"),
        Row(id=4, record_kind="execution"),
    ]
    ledger = Ledger(rows)
    service = AlpacaPaperReconcileService(ledger, Broker(filled_order()))

    first = await service.reconcile(dry_run=False)
    second = await service.reconcile(dry_run=False)

    assert first["count"] == 1
    assert first["reconciled"][0]["ledger_id"] == 4
    assert len(ledger.status_calls) == 1
    assert second == {"success": True, "dry_run": False, "reconciled": [], "count": 0}


@pytest.mark.asyncio
async def test_reconcile_skips_final_reconciled_execution_row() -> None:
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row(lifecycle_state="final_reconciled")])
    result = await AlpacaPaperReconcileService(
        ledger, Broker(filled_order())
    ).reconcile(dry_run=False)

    assert result["count"] == 0
    assert ledger.rows[0].lifecycle_state == "final_reconciled"
    assert ledger.status_calls == []


@pytest.mark.asyncio
async def test_reconcile_filled_status_with_partial_evidence_keeps_partial_state() -> (
    None
):
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row()])
    order = filled_order(status="filled", qty="0.007")
    order.qty = Decimal("0.014")
    result = await AlpacaPaperReconcileService(ledger, Broker(order)).reconcile(
        dry_run=False
    )

    assert result["reconciled"][0]["action"] == "booked_partial"
    assert ledger.status_calls[0]["status"] == "partially_filled"
    assert ledger.rows[0].lifecycle_state == "submitted"


def test_normalize_alpaca_order_uses_quantity_weighted_fill_average() -> None:
    from types import SimpleNamespace

    from app.services.alpaca_paper_reconcile_service import (
        normalize_alpaca_order_for_classify,
    )

    order = filled_order(qty="0.010")
    order.filled_avg_price = None
    normalized = normalize_alpaca_order_for_classify(
        order,
        [
            SimpleNamespace(
                order_id=order.id,
                qty=Decimal("0.005"),
                price=Decimal("100"),
                cum_qty=Decimal("0.005"),
            ),
            SimpleNamespace(
                order_id=order.id,
                qty=Decimal("0.005"),
                price=Decimal("110"),
                cum_qty=Decimal("0.010"),
            ),
        ],
    )

    assert normalized["ccld_unpr"] == Decimal("105")


@pytest.mark.asyncio
async def test_reconcile_filled_missing_evidence_and_failed_fill_read_requires_review() -> (
    None
):
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    class FailingFillsBroker(Broker):
        async def list_fills(self, **_: Any) -> list[Any]:
            raise RuntimeError("fill activity unavailable")

    order = filled_order(qty="0")
    order.filled_qty = None
    order.filled_avg_price = None
    result = await AlpacaPaperReconcileService(
        Ledger([Row()]), FailingFillsBroker(order)
    ).reconcile(dry_run=False)

    assert result["reconciled"][0]["action"] == "noop_requires_manual_review"
    assert result["reconciled"][0]["requires_manual_review"] is True


@pytest.mark.asyncio
async def test_reconcile_specific_old_client_order_uses_direct_ledger_and_broker_lookup() -> (
    None
):
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    old_execution = Row(id=201, client_order_id="old-order")
    rows = [Row(id=index, client_order_id=f"recent-{index}") for index in range(200)]
    ledger = Ledger(rows + [old_execution])
    broker = Broker(filled_order())
    result = await AlpacaPaperReconcileService(ledger, broker).reconcile(
        client_order_id="old-order", dry_run=False, limit=200
    )

    assert result["count"] == 1
    assert result["reconciled"][0]["ledger_id"] == 201
    assert len(ledger.status_calls) == 1


# ---------------------------------------------------------------------------
# ROB-953 R3 (1) — the reported action and the persisted lifecycle_state are
# derived from one mapping, so they must agree for EVERY transition shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("broker_status", "qty", "order_qty", "cancel_status", "expected_label"),
    [
        # broker agrees the order is done and the quantity backs it up
        ("filled", "0.014", "0.014", None, "filled"),
        # broker reports filled but only a partial quantity is evidenced
        ("filled", "0.007", "0.014", None, "partial"),
        ("partially_filled", "0.007", "0.014", None, "partial"),
        # an open status carrying a fill is a contradiction, never a fill
        ("accepted", "0.014", "0.014", None, "anomaly"),
        ("new", "0.014", "0.014", None, "anomaly"),
        ("accepted", "0.007", "0.014", None, "partial"),
        # terminal non-fill statuses reporting a full quantity
        ("rejected", "0.014", "0.014", None, "anomaly"),
        ("expired", "0.014", "0.014", None, "anomaly"),
        ("canceled", "0.014", "0.014", None, "anomaly"),
        # canceled *with* cancel evidence on the row is a legitimate cancel
        ("canceled", "0.014", "0.014", "canceled", "canceled"),
    ],
)
async def test_reported_action_always_matches_persisted_lifecycle_state(
    broker_status: str,
    qty: str,
    order_qty: str,
    cancel_status: str | None,
    expected_label: str,
) -> None:
    """Regression for the round-1/2 defect class: response said one thing, the
    ledger row said another (booked_filled persisted as anomaly, and inverse)."""
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    ledger = Ledger([Row(cancel_status=cancel_status)])
    order = filled_order(status=broker_status, qty=qty)
    order.qty = Decimal(order_qty)

    result = await AlpacaPaperReconcileService(ledger, Broker(order)).reconcile(
        dry_run=False
    )
    outcome = result["reconciled"][0]

    assert outcome["action"] == f"booked_{expected_label}"
    assert outcome["transition_depth"] == expected_label
    # the persisted row is the ground truth the action claims to describe
    assert outcome["lifecycle_state"] == ledger.rows[0].lifecycle_state
    assert outcome["persisted_lifecycle_state"] == ledger.rows[0].lifecycle_state
    assert outcome.get("requires_manual_review") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize("broker_status", ["filled", "rejected", "accepted"])
async def test_dry_run_plan_matches_the_state_a_real_run_would_persist(
    broker_status: str,
) -> None:
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    def run(dry_run: bool) -> Any:
        return AlpacaPaperReconcileService(
            Ledger([Row()]), Broker(filled_order(status=broker_status))
        ).reconcile(dry_run=dry_run)

    planned = (await run(True))["reconciled"][0]
    booked = (await run(False))["reconciled"][0]

    assert planned["lifecycle_state"] == booked["lifecycle_state"]
    assert planned["action"] == booked["action"].replace("booked_", "would_book_")


def test_resolve_transition_labels_cover_every_derivable_state() -> None:
    """No lifecycle state the resolver can produce may fall through unlabelled."""
    from app.services.alpaca_paper_ledger_service import derive_lifecycle_state
    from app.services.alpaca_paper_reconcile_service import (
        _LIFECYCLE_ACTION_LABELS,
        resolve_transition,
    )
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    statuses = [
        "filled",
        "partially_filled",
        "accepted",
        "new",
        "pending_new",
        "rejected",
        "expired",
        "canceled",
        "suspended",
        "weird_unknown_status",
    ]
    for status in statuses:
        for verdict in (FillVerdict.FILLED, FillVerdict.PARTIAL):
            for cancel_evidence in (True, False):
                transition = resolve_transition(
                    verdict=verdict,
                    broker_status=status,
                    filled_qty=Decimal("1"),
                    has_cancel_evidence=cancel_evidence,
                )
                assert transition.lifecycle_state in _LIFECYCLE_ACTION_LABELS
                # the resolver's state is literally what record_status derives
                assert transition.lifecycle_state == derive_lifecycle_state(
                    transition.broker_status,
                    Decimal("1"),
                    has_cancel_evidence=cancel_evidence,
                )


# ---------------------------------------------------------------------------
# ROB-953 R3 (4) — fill-set completeness gates the weighted average
# ---------------------------------------------------------------------------


def _fill(order_id: str, qty: str, price: str, cum: str, fill_id: str = "f1") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        id=fill_id,
        order_id=order_id,
        qty=Decimal(qty),
        price=Decimal(price),
        cum_qty=Decimal(cum),
        transaction_time=None,
    )


class FillsBroker(Broker):
    """Alpaca-semantic activity fake: desc pages continue after page_token."""

    def __init__(self, order: Order | None, activities: list[Any]) -> None:
        super().__init__(order)
        self.activities = activities
        self.calls: list[dict[str, Any]] = []

    async def list_fills(
        self,
        *,
        page_token: str | None = None,
        page_size: int = 100,
        direction: str = "desc",
    ) -> list[Any]:
        assert direction == "desc"
        self.calls.append(
            {
                "page_token": page_token,
                "page_size": page_size,
                "direction": direction,
            }
        )
        start = 0
        if page_token is not None:
            start = next(
                index + 1
                for index, activity in enumerate(self.activities)
                if str(activity.id) == page_token
            )
        return self.activities[start : start + page_size]


@pytest.mark.asyncio
async def test_incomplete_fill_set_requires_manual_review_without_transition() -> None:
    """Σ(fill qty) < the order's cumulative filled_qty means fills were truncated;
    averaging over them would book a wrong price."""
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    order = filled_order(qty="0.014")
    order.filled_avg_price = None
    ledger = Ledger([Row()])
    broker = FillsBroker(order, [_fill(order.id, "0.007", "100", "0.007")])

    result = await AlpacaPaperReconcileService(ledger, broker).reconcile(dry_run=False)
    outcome = result["reconciled"][0]

    assert outcome["action"] == "noop_requires_manual_review"
    assert outcome["requires_manual_review"] is True
    assert outcome["reason"] == "incomplete_fill_set"
    assert outcome["fill_set_complete"] is False
    assert ledger.rows[0].lifecycle_state == "submitted"
    assert ledger.status_calls == []


@pytest.mark.asyncio
async def test_filled_status_with_empty_fills_requires_manual_review() -> None:
    """A broker average cannot substitute for the required fill activity set."""
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    order = filled_order(qty="1")
    ledger = Ledger([Row()])
    broker = FillsBroker(order, [])

    result = await AlpacaPaperReconcileService(ledger, broker).reconcile(
        dry_run=False
    )
    outcome = result["reconciled"][0]

    assert outcome["action"] == "noop_requires_manual_review"
    assert outcome["requires_manual_review"] is True
    assert outcome["reason"] == "incomplete_fill_set"
    assert broker.calls == [
        {"page_token": None, "page_size": 100, "direction": "desc"}
    ]
    assert ledger.status_calls == []


@pytest.mark.asyncio
async def test_filled_status_with_zero_qty_and_no_fills_requires_manual_review() -> (
    None
):
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    order = filled_order(status="filled", qty="0")
    order.filled_avg_price = None
    ledger = Ledger([Row()])

    result = await AlpacaPaperReconcileService(ledger, Broker(order)).reconcile(
        dry_run=False
    )
    outcome = result["reconciled"][0]

    assert outcome["action"] == "noop_requires_manual_review"
    assert outcome["reason"] == "filled_status_without_fill_evidence"
    assert ledger.status_calls == []


@pytest.mark.asyncio
async def test_complete_fill_set_books_exact_quantity_weighted_average() -> None:
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    order = filled_order(qty="0.010")
    order.qty = Decimal("0.010")
    order.filled_avg_price = None
    ledger = Ledger([Row()])
    broker = FillsBroker(
        order,
        [
            _fill(order.id, "0.005", "100", "0.005", fill_id="a"),
            _fill(order.id, "0.005", "110", "0.010", fill_id="b"),
        ],
    )

    result = await AlpacaPaperReconcileService(ledger, broker).reconcile(dry_run=False)
    outcome = result["reconciled"][0]

    assert outcome["fill_set_complete"] is True
    assert outcome["action"] == "booked_filled"
    assert Decimal(outcome["avg_price"]) == Decimal("105")
    assert ledger.rows[0].lifecycle_state == "filled"


@pytest.mark.asyncio
async def test_fill_pages_are_walked_until_the_set_is_complete() -> None:
    """A single 100-row page must not cap the fill set."""
    from datetime import UTC, datetime

    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    order = filled_order(qty="100.5")
    order.qty = Decimal("100.5")
    order.filled_avg_price = None

    page_one = []
    for index in range(100):
        fill = _fill(order.id, "1", "100", str(index + 1), fill_id=f"p1-{index}")
        fill.transaction_time = datetime(2026, 7, 18, 12, index % 60, tzinfo=UTC)
        page_one.append(fill)
    page_two = [_fill(order.id, "0.5", "200", "100.5", fill_id="p2-0")]

    broker = FillsBroker(order, [*page_one, *page_two])
    ledger = Ledger([Row()])

    result = await AlpacaPaperReconcileService(ledger, broker).reconcile(dry_run=False)
    outcome = result["reconciled"][0]

    assert len(broker.calls) == 2
    assert [call["page_token"] for call in broker.calls] == [None, "p1-99"]
    assert all(call["page_size"] == 100 for call in broker.calls)
    assert all(call["direction"] == "desc" for call in broker.calls)
    assert outcome["fill_set_complete"] is True
    assert outcome["action"] == "booked_filled"
    # (100 * 100 + 0.5 * 200) / 100.5
    assert Decimal(outcome["avg_price"]) == (Decimal("10100") / Decimal("100.5"))


# ---------------------------------------------------------------------------
# ROB-953 R3 (5) — eligibility filters run before the bulk limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_limit_applies_after_eligibility_filters() -> None:
    """200 newer preview/terminal rows must not hide the 201st open execution."""
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    noise = [
        Row(
            id=index,
            client_order_id=f"noise-{index}",
            record_kind="preview" if index % 2 else "execution",
            lifecycle_state="previewed" if index % 2 else "filled",
        )
        for index in range(200)
    ]
    eligible = Row(id=201, client_order_id="rob73-08ebbf8c64e2dd93")
    ledger = Ledger([*noise, eligible])

    result = await AlpacaPaperReconcileService(
        ledger, Broker(filled_order())
    ).reconcile(dry_run=False, limit=100)

    assert result["count"] == 1
    assert result["reconciled"][0]["ledger_id"] == 201
    assert eligible.lifecycle_state == "filled"
