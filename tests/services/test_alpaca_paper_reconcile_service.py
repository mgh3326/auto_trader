"""Evidence-first booking for manually submitted Alpaca paper orders (ROB-953)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.alpaca.schemas import Order


@dataclass
class Row:
    id: int = 20
    client_order_id: str = "rob73-08ebbf8c64e2dd93"
    execution_symbol: str = "ISRG"
    lifecycle_state: str = "submitted"
    filled_qty: Decimal = Decimal("0")
    record_kind: str = "execution"


class Ledger:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.status_calls: list[dict[str, Any]] = []

    async def list_recent(self, limit: int) -> list[Row]:
        return self.rows[:limit]

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
        self.status_calls.append(order)
        row = next(
            r
            for r in self.rows
            if r.client_order_id == client_order_id and r.record_kind == "execution"
        )
        row.filled_qty = Decimal(str(order["filled_qty"]))
        row.lifecycle_state = (
            "filled"
            if order["status"] == "filled"
            else "submitted"
            if order["status"] == "partially_filled"
            else "anomaly"
        )
        return row


class Broker:
    def __init__(self, order: Order | None) -> None:
        self.order = order

    async def get_order_by_client_order_id(self, _: str) -> Order | None:
        return self.order

    async def list_fills(self, **_: Any) -> list[Any]:
        return []


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

    order = filled_order(qty="0")
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
