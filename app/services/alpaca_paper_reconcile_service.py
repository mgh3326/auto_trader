"""Evidence-first reconcile for manually submitted Alpaca paper orders (ROB-953)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.alpaca_paper_ledger_service import (
    KNOWN_OPEN_BROKER_STATUSES,
    LIFECYCLE_SUBMITTED,
    RECORD_KIND_EXECUTION,
)
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
    classify_fill_evidence,
)

_TERMINAL_STATES = frozenset(
    {
        "filled",
        "position_reconciled",
        "closed",
        "final_reconciled",
        "canceled",
        "anomaly",
    }
)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def normalize_alpaca_order_for_classify(
    order: Any, fills: list[Any] | None = None
) -> dict[str, Any]:
    """Adapt Alpaca's cumulative order/fill values to the shared classifier shape."""
    relevant = [
        fill for fill in fills or [] if getattr(fill, "order_id", None) == order.id
    ]
    cumulative = max(
        (_decimal(getattr(fill, "cum_qty", None)) or Decimal("0") for fill in relevant),
        default=Decimal("0"),
    )
    filled_qty = _decimal(getattr(order, "filled_qty", None)) or cumulative
    avg_price = _decimal(getattr(order, "filled_avg_price", None))
    if avg_price is None:
        priced_fills = [
            (
                _decimal(getattr(fill, "qty", None)),
                _decimal(getattr(fill, "price", None)),
            )
            for fill in relevant
        ]
        total_qty = sum(
            (
                qty
                for qty, price in priced_fills
                if qty is not None and price is not None
            ),
            start=Decimal("0"),
        )
        if total_qty > 0:
            avg_price = (
                sum(
                    (
                        qty * price
                        for qty, price in priced_fills
                        if qty is not None and price is not None
                    ),
                    start=Decimal("0"),
                )
                / total_qty
            )
    return {
        "odno": order.id,
        "ord_qty": getattr(order, "qty", None),
        "tot_ccld_qty": filled_qty,
        "ccld_unpr": avg_price,
    }


class AlpacaPaperReconcileService:
    """Reconcile existing non-terminal ledger rows using read-only broker evidence."""

    def __init__(self, ledger: Any, broker: Any) -> None:
        self._ledger = ledger
        self._broker = broker

    async def reconcile(
        self,
        *,
        symbol: str | None = None,
        client_order_id: str | None = None,
        dry_run: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        if client_order_id is not None:
            row = await self._ledger.get_execution_by_client_order_id(client_order_id)
            rows = [row] if row is not None else []
        else:
            rows = await self._ledger.list_recent(limit=limit)
        candidates = [
            row
            for row in rows
            if getattr(row, "record_kind", None) == RECORD_KIND_EXECUTION
            and getattr(row, "lifecycle_state", None) not in _TERMINAL_STATES
            and (symbol is None or getattr(row, "execution_symbol", None) == symbol)
            and (
                client_order_id is None
                or getattr(row, "client_order_id", None) == client_order_id
            )
        ]
        outcomes = [
            await self._reconcile_one(row, dry_run=dry_run) for row in candidates
        ]
        return {
            "success": True,
            "dry_run": dry_run,
            "reconciled": outcomes,
            "count": len(outcomes),
        }

    async def _reconcile_one(self, row: Any, *, dry_run: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ledger_id": row.id,
            "client_order_id": row.client_order_id,
            "symbol": row.execution_symbol,
            "transition_depth": "none",
        }
        try:
            order = await self._broker.get_order_by_client_order_id(row.client_order_id)
        except Exception as exc:  # read failure is never execution evidence
            result.update(
                action="noop_requires_manual_review",
                requires_manual_review=True,
                reason=str(exc) or exc.__class__.__name__,
            )
            return result
        if order is None:
            result.update(
                action="noop_requires_manual_review",
                requires_manual_review=True,
                reason="broker_order_not_found",
            )
            return result

        fills: list[Any] | None = None
        if (
            _decimal(getattr(order, "filled_qty", None)) or Decimal("0")
        ) <= 0 or getattr(order, "filled_avg_price", None) is None:
            try:
                fills = await self._broker.list_fills(limit=100)
            except Exception as exc:
                if str(getattr(order, "status", "")).lower() == "filled":
                    result.update(
                        action="noop_requires_manual_review",
                        requires_manual_review=True,
                        reason=str(exc) or exc.__class__.__name__,
                    )
                    return result
                fills = None
        evidence: FillEvidence = classify_fill_evidence(
            order_no=order.id, rows=[normalize_alpaca_order_for_classify(order, fills)]
        )
        result["verdict"] = evidence.verdict.value
        if evidence.verdict is FillVerdict.NONE:
            result.update(
                action="noop_requires_manual_review",
                requires_manual_review=True,
                reason=evidence.reason_code,
            )
            return result
        if evidence.verdict is FillVerdict.PENDING:
            result.update(action="noop_pending")
            return result

        broker_qty = evidence.filled_qty or Decimal("0")
        already = _decimal(getattr(row, "filled_qty", None)) or Decimal("0")
        result.update(
            filled_qty=str(broker_qty),
            avg_price=str(evidence.avg_price),
            delta_qty=str(broker_qty - already),
        )
        if broker_qty <= already:
            result.update(
                action="noop_already_booked",
                transition_depth=getattr(row, "lifecycle_state", "none"),
            )
            return result

        # An open status claiming a fill is explicitly recorded as the ledger's
        # existing anomaly state; it must not be promoted to a terminal fill.
        broker_status = str(getattr(order, "status", "")).lower()
        anomaly = (
            broker_status in KNOWN_OPEN_BROKER_STATUSES
            and broker_status != "partially_filled"
            and broker_qty > 0
        )
        if anomaly:
            target = "anomaly"
        elif evidence.verdict is FillVerdict.PARTIAL:
            target = "partial"
        else:
            target = "filled"
        result["transition_depth"] = target
        result["action"] = f"would_book_{target}" if dry_run else f"booked_{target}"
        if dry_run:
            return result
        await self._ledger.record_status(
            row.client_order_id,
            {
                "status": "partially_filled"
                if evidence.verdict is FillVerdict.PARTIAL
                else order.status,
                "filled_qty": str(broker_qty),
                "filled_avg_price": str(evidence.avg_price),
                "id": order.id,
            },
            raw_response={
                "reconcile_order": normalize_alpaca_order_for_classify(order, fills)
            },
            lifecycle_state_override=(
                LIFECYCLE_SUBMITTED if evidence.verdict is FillVerdict.PARTIAL else None
            ),
        )
        return result


__all__ = ["AlpacaPaperReconcileService", "normalize_alpaca_order_for_classify"]
