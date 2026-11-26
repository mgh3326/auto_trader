"""Preview-first reconciliation of Alpaca paper crypto fee activities."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.services.alpaca_paper_crypto_fee_evidence import (
    activity_posting_date,
    count_filled_legs_for_day,
    resolve_crypto_fee_evidence,
)


class AlpacaPaperCryptoFeeReconcileService:
    def __init__(self, ledger: Any, broker: Any) -> None:
        self._ledger = ledger
        self._broker = broker

    async def reconcile(
        self,
        *,
        commit: bool = False,
        limit: int = 100,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        rows = await self._ledger.list_crypto_fee_candidates(
            limit=limit, client_order_id=client_order_id
        )
        outcomes = []
        for row in rows:
            order_id = row.broker_order_id
            if not order_id:
                evidence = None
                reason = "fee_pending_eod"
            else:
                try:
                    terminal_filled = row.order_status == "filled"
                    fills = await self._broker.list_order_fills(order_id=order_id)
                    filled_qty = Decimal(str(row.filled_qty))
                    if (
                        not terminal_filled
                        or not fills
                        or sum((fill.qty for fill in fills), Decimal("0")) != filled_qty
                        or any(fill.order_id != order_id for fill in fills)
                    ):
                        activities = await self._broker.list_crypto_fee_activities(
                            order_id=order_id
                        )
                        posting_date = (
                            activity_posting_date(activities[0]) if activities else None
                        ) or next(
                            (
                                fill.transaction_time.date()
                                for fill in reversed(fills)
                                if fill.transaction_time is not None
                            ),
                            datetime.now(UTC).date(),
                        )
                        evidence = resolve_crypto_fee_evidence(
                            symbol=row.execution_symbol,
                            side=row.side,
                            filled_qty=filled_qty,
                            activities=activities,
                            posting_date=posting_date,
                            filled_leg_count=0,
                            terminal_filled=False,
                        )
                        reason = evidence.reason
                    else:
                        activities = await self._broker.list_crypto_fee_activities(
                            order_id=order_id
                        )
                        posting_date = (
                            activity_posting_date(activities[0]) if activities else None
                        ) or next(
                            (
                                fill.transaction_time.date()
                                for fill in reversed(fills)
                                if fill.transaction_time is not None
                            ),
                            datetime.now(UTC).date(),
                        )
                        if activities and all(
                            activity_posting_date(activity) == posting_date
                            for activity in activities
                        ):
                            day_fills = await self._broker.list_fills_for_date(
                                date=posting_date
                            )
                            filled_leg_count = count_filled_legs_for_day(
                                fills=day_fills,
                                symbol=row.execution_symbol,
                                posting_date=posting_date,
                                expected_order_id=order_id,
                            )
                        else:
                            filled_leg_count = 0
                        evidence = resolve_crypto_fee_evidence(
                            symbol=row.execution_symbol,
                            side=row.side,
                            filled_qty=filled_qty,
                            activities=activities,
                            posting_date=posting_date,
                            filled_leg_count=filled_leg_count,
                            terminal_filled=terminal_filled,
                        )
                        reason = evidence.reason
                except Exception:
                    evidence = None
                    reason = "fee_activity_unreadable"
            fee_amount = evidence.amount if evidence else None
            fee_currency = evidence.currency if evidence else None
            net_received_qty = evidence.net_received_qty if evidence else None
            written: bool | None = None
            if commit:
                written = await self._ledger.record_crypto_fee_evidence(
                    row.client_order_id,
                    expected_broker_order_id=order_id,
                    expected_filled_qty=Decimal(str(row.filled_qty)),
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
                    "action": (
                        "recorded"
                        if commit and written
                        else "stale_evidence"
                        if commit
                        else "would_record"
                    ),
                }
            )
        return {"dry_run": not commit, "count": len(outcomes), "items": outcomes}
