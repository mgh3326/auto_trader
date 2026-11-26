"""Preview-first reconciliation of Alpaca paper crypto fee activities."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.alpaca_paper_crypto_fee_evidence import resolve_crypto_fee_evidence


class AlpacaPaperCryptoFeeReconcileService:
    def __init__(self, ledger: Any, broker: Any) -> None:
        self._ledger = ledger
        self._broker = broker

    async def reconcile(
        self, *, commit: bool = False, limit: int = 100
    ) -> dict[str, Any]:
        rows = await self._ledger.list_crypto_fee_candidates(limit=limit)
        outcomes = []
        for row in rows:
            order_id = row.broker_order_id
            if not order_id:
                evidence = None
                reason = "broker_order_id_missing"
            else:
                try:
                    activities = await self._broker.list_crypto_fee_activities(
                        order_id=order_id
                    )
                    evidence = resolve_crypto_fee_evidence(
                        order_id=order_id,
                        symbol=row.execution_symbol,
                        side=row.side,
                        filled_qty=Decimal(str(row.filled_qty)),
                        activities=activities,
                    )
                    reason = evidence.reason
                except Exception:
                    evidence = None
                    reason = "fee_activity_unreadable"
            fee_amount = evidence.amount if evidence else None
            fee_currency = evidence.currency if evidence else None
            net_received_qty = evidence.net_received_qty if evidence else None
            if commit:
                await self._ledger.record_crypto_fee_evidence(
                    row.client_order_id,
                    fee_amount=fee_amount,
                    fee_currency=fee_currency,
                    net_received_qty=net_received_qty,
                    reason=reason,
                )
            outcomes.append(
                {
                    "client_order_id": row.client_order_id,
                    "broker_order_id": order_id,
                    "fee_amount": str(fee_amount) if fee_amount is not None else None,
                    "fee_currency": fee_currency,
                    "net_received_qty": (
                        str(net_received_qty) if net_received_qty is not None else None
                    ),
                    "reason": reason,
                    "action": "recorded" if commit else "would_record",
                }
            )
        return {"dry_run": not commit, "count": len(outcomes), "items": outcomes}
