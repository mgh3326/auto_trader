"""KIS mock reconciliation job (ROB-102).

Composes:
- KISMockLifecycleService (DB read/write)
- KISClient (Live holdings fetch)
- kis_mock_holdings_reconciler (Pure logic)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.kis import kis
from app.services.kis_mock_holdings_reconciler import (
    HoldingsSnapshot,
    LedgerOrderInput,
    ReconcilerThresholds,
    classify_orders,
)
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService


async def run_kis_mock_reconciliation(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """Fetch open mock orders, fetch live holdings, and apply transitions."""
    lifecycle_svc = KISMockLifecycleService(db)
    open_rows = await lifecycle_svc.list_open_orders(limit=limit)
    if not open_rows:
        return {
            "orders_processed": 0,
            "transitions_applied": 0,
            "dry_run": dry_run,
            "message": "No open KIS mock orders found",
        }

    # 1. Fetch live holdings from KIS (Domestic only for now as KIS mock is domestic)
    raw_holdings = await kis.fetch_my_stocks()
    
    now = datetime.now(timezone.utc)
    holdings_map: dict[str, HoldingsSnapshot] = {}
    for h in raw_holdings:
        symbol = h["symbol"]
        qty = Decimal(str(h["qty"]))
        holdings_map[symbol] = HoldingsSnapshot(
            symbol=symbol, quantity=qty, taken_at=now
        )

    # 2. Map DB rows to Reconciler input
    order_inputs: list[LedgerOrderInput] = []
    for row in open_rows:
        order_inputs.append(
            LedgerOrderInput(
                ledger_id=row.id,
                symbol=row.symbol,
                side=row.side,  # type: ignore
                ordered_qty=Decimal(str(row.quantity)),
                lifecycle_state=row.lifecycle_state,  # type: ignore
                holdings_baseline_qty=(
                    Decimal(str(row.holdings_baseline_qty))
                    if row.holdings_baseline_qty is not None
                    else None
                ),
                accepted_at=row.trade_date,
            )
        )

    # 3. Classify
    proposals = classify_orders(
        orders=order_inputs,
        holdings=holdings_map,
        thresholds=ReconcilerThresholds(),
        now=now,
    )

    # 4. Apply
    applied_count = 0
    transition_logs = []
    
    for p in proposals:
        # Special case: if baseline was missing, we just used current holdings as the new baseline
        # but the reconciler already emitted 'baseline_missing' -> 'anomaly' proposal.
        # However, for 'accepted' orders with missing baseline, we want to FIX the baseline first.
        if p.reason_code == "baseline_missing":
            snap = holdings_map.get(p.symbol)
            if snap:
                if not dry_run:
                    await lifecycle_svc.record_holdings_baseline(
                        ledger_id=p.ledger_id, baseline_qty=snap.quantity
                    )
                applied_count += 1
                transition_logs.append({
                    "ledger_id": p.ledger_id,
                    "symbol": p.symbol,
                    "action": "fixed_baseline",
                    "baseline_qty": str(snap.quantity),
                })
            continue

        res = await lifecycle_svc.apply_lifecycle_transition(
            ledger_id=p.ledger_id,
            next_state=p.next_state,
            reason_code=p.reason_code,
            detail={
                "observed_holdings_qty": str(p.observed_holdings_qty),
                "observed_delta": str(p.observed_delta),
            },
            dry_run=dry_run,
        )
        if res.get("applied") or res.get("would_change"):
            applied_count += 1
        transition_logs.append(res)

    return {
        "orders_processed": len(open_rows),
        "transitions_applied": applied_count,
        "dry_run": dry_run,
        "transitions": transition_logs,
    }
