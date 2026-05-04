"""KIS mock reconciliation job (ROB-102).

Composes:
- KISMockLifecycleService (DB read/write)
- KISClient (read-only mock holdings via fetch_my_stocks(is_mock=True))
- kis_mock_holdings_reconciler (pure decision logic)

No broker mutation. No live-account access. The reconciler treats
``baseline_missing`` as an operator-review signal (anomaly); the row's
baseline is captured at order-insert time by the order execution path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.schemas.execution_contracts import OrderLifecycleEvent
from app.services.brokers.kis import KISClient
from app.services.kis_mock_holdings_reconciler import (
    HoldingsSnapshot,
    LedgerOrderInput,
    ReconcilerThresholds,
    classify_orders,
)
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService


def _to_decimal(val: Any) -> Decimal:
    if val in ("", None):
        return Decimal(0)
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


async def _collect_kis_mock_holdings(
    kis_client: KISClient,
    *,
    taken_at: datetime,
) -> dict[str, HoldingsSnapshot]:
    """Read-only snapshot of KIS mock holdings (KR + US)."""
    snapshots: dict[str, HoldingsSnapshot] = {}

    kr = await kis_client.fetch_my_stocks(is_mock=True, is_overseas=False)
    for stock in kr or []:
        symbol = to_db_symbol(str(stock.get("pdno") or ""))
        if not symbol:
            continue
        snapshots[symbol] = HoldingsSnapshot(
            symbol=symbol,
            quantity=_to_decimal(stock.get("hldg_qty")),
            taken_at=taken_at,
        )

    us = await kis_client.fetch_my_stocks(is_mock=True, is_overseas=True)
    for stock in us or []:
        symbol = to_db_symbol(str(stock.get("ovrs_pdno") or ""))
        if not symbol:
            continue
        snapshots[symbol] = HoldingsSnapshot(
            symbol=symbol,
            quantity=_to_decimal(stock.get("ovrs_cblc_qty")),
            taken_at=taken_at,
        )

    return snapshots


async def run_kis_mock_reconciliation(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    limit: int = 100,
    thresholds: ReconcilerThresholds | None = None,
    kis_client: KISClient | None = None,
) -> dict[str, Any]:
    """Fetch open mock orders, fetch mock holdings, propose & optionally apply transitions."""
    thresholds = thresholds or ReconcilerThresholds()
    lifecycle_svc = KISMockLifecycleService(db)
    open_rows = await lifecycle_svc.list_open_orders(limit=limit)
    if not open_rows:
        return {
            "success": True,
            "account_mode": "kis_mock",
            "broker": "kis",
            "orders_processed": 0,
            "transitions_applied": 0,
            "dry_run": dry_run,
            "transitions": [],
            "events": [],
            "message": "No open KIS mock orders found",
        }

    now = datetime.now(UTC)
    client = kis_client if kis_client is not None else KISClient(is_mock=True)
    holdings_map = await _collect_kis_mock_holdings(client, taken_at=now)

    order_inputs: list[LedgerOrderInput] = [
        LedgerOrderInput(
            ledger_id=row.id,
            symbol=row.symbol,
            side=row.side,
            ordered_qty=_to_decimal(row.quantity),
            lifecycle_state=row.lifecycle_state,
            holdings_baseline_qty=(
                Decimal(str(row.holdings_baseline_qty))
                if row.holdings_baseline_qty is not None
                else None
            ),
            accepted_at=row.trade_date,
        )
        for row in open_rows
    ]

    proposals = classify_orders(
        orders=order_inputs,
        holdings=holdings_map,
        thresholds=thresholds,
        now=now,
    )

    applied_count = 0
    transition_logs: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for proposal in proposals:
        detail = {
            "observed_holdings_qty": (
                str(proposal.observed_holdings_qty)
                if proposal.observed_holdings_qty is not None
                else None
            ),
            "observed_delta": (
                str(proposal.observed_delta)
                if proposal.observed_delta is not None
                else None
            ),
        }
        outcome = await lifecycle_svc.apply_lifecycle_transition(
            ledger_id=proposal.ledger_id,
            next_state=proposal.next_state,
            reason_code=proposal.reason_code,
            detail=detail,
            dry_run=dry_run,
        )
        if outcome.get("applied"):
            applied_count += 1
        transition_logs.append(outcome)
        events.append(
            OrderLifecycleEvent(
                account_mode="kis_mock",
                execution_source="reconciler",
                state=proposal.next_state,
                occurred_at=now,
                detail={
                    "ledger_id": proposal.ledger_id,
                    "symbol": proposal.symbol,
                    "prior_state": proposal.prior_state,
                    "reason_code": proposal.reason_code,
                    **detail,
                },
            ).model_dump(mode="json")
        )

    return {
        "success": True,
        "account_mode": "kis_mock",
        "broker": "kis",
        "orders_processed": len(open_rows),
        "transitions_applied": applied_count,
        "dry_run": dry_run,
        "transitions": transition_logs,
        "events": events,
    }
