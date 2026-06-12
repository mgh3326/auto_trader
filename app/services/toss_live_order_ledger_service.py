from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TossLiveOrderLedger


def parse_report_item_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    candidate = str(value).strip()
    if not candidate:
        return None
    return uuid.UUID(candidate)


class TossLiveOrderLedgerService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record_send(
        self,
        *,
        operation_kind: str,
        market: str,
        symbol: str,
        side: str,
        order_type: str,
        time_in_force: str | None,
        quantity: Decimal | None,
        price: Decimal | None,
        order_amount: Decimal | None,
        currency: str | None,
        client_order_id: str,
        broker_order_id: str | None,
        original_order_id: str | None,
        status: str,
        broker_status: str | None,
        response_code: str | None,
        response_message: str | None,
        raw_response: dict[str, Any] | None,
        reason: str | None = None,
        thesis: str | None = None,
        strategy: str | None = None,
        target_price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        min_hold_days: int | None = None,
        notes: str | None = None,
        exit_reason: str | None = None,
        indicators_snapshot: dict[str, Any] | None = None,
        report_item_uuid: str | uuid.UUID | None = None,
    ) -> TossLiveOrderLedger:
        row = TossLiveOrderLedger(
            trade_date=datetime.now(UTC),
            broker="toss",
            account_mode="toss_live",
            operation_kind=operation_kind,
            market=market,
            symbol=symbol,
            side=side,
            order_type=order_type,
            time_in_force=time_in_force,
            quantity=quantity,
            price=price,
            order_amount=order_amount,
            currency=currency,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            original_order_id=original_order_id,
            status=status,
            broker_status=broker_status,
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            exit_reason=exit_reason,
            indicators_snapshot=indicators_snapshot,
            report_item_uuid=parse_report_item_uuid(report_item_uuid),
        )
        self._db.add(row)
        await self._db.flush()
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def mark_replaced(
        self, *, broker_order_id: str, replaced_by_order_id: str
    ) -> None:
        stmt = select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.broker_order_id == broker_order_id
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.replaced_by_order_id = replaced_by_order_id
        row.status = "replaced"
        await self._db.commit()

    async def list_open(
        self,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        market: str | None = None,
        limit: int = 100,
    ) -> list[TossLiveOrderLedger]:
        stmt = select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.status.in_(("accepted", "pending", "partial"))
        )
        stmt = stmt.where(TossLiveOrderLedger.operation_kind.in_(("place", "modify")))
        if symbol:
            stmt = stmt.where(TossLiveOrderLedger.symbol == symbol)
        if order_id:
            stmt = stmt.where(TossLiveOrderLedger.broker_order_id == order_id)
        if market:
            stmt = stmt.where(TossLiveOrderLedger.market == market)
        stmt = stmt.order_by(TossLiveOrderLedger.created_at.asc()).limit(limit)
        rows = list((await self._db.execute(stmt)).scalars().all())
        for row in rows:
            self._db.expunge(row)
        return rows

    async def update_reconcile_outcome(
        self,
        *,
        ledger_id: int,
        status: str,
        broker_status: str | None,
        filled_qty: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        commission: Decimal | None = None,
        tax: Decimal | None = None,
        settlement_date: date | None = None,
        trade_id: int | None = None,
        journal_id: int | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        row = await self._db.get(TossLiveOrderLedger, ledger_id)
        if row is None:
            return
        row.status = status
        row.broker_status = broker_status
        if filled_qty is not None:
            row.filled_qty = filled_qty
        if avg_fill_price is not None:
            row.avg_fill_price = avg_fill_price
        if commission is not None:
            row.commission = commission
        if tax is not None:
            row.tax = tax
        if settlement_date is not None:
            row.settlement_date = settlement_date
        if trade_id is not None:
            row.trade_id = trade_id
        if journal_id is not None:
            row.journal_id = journal_id
        if raw_response is not None:
            row.raw_response = raw_response
        row.reconciled_at = datetime.now(UTC)
        await self._db.commit()
