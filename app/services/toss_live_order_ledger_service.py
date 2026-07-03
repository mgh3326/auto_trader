from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TossLiveOrderLedger


def parse_report_item_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """ROB-545 B1 — parse a report_item_uuid fail-open.

    report_item_uuid is free-form agent/Hermes input and audit metadata only.
    A malformed value must never raise here: ``record_send`` runs *after* the
    live POST is accepted, so raising would orphan a real broker order (no
    ledger row -> reconcile can never book the fill). Mirror
    ``order_execution._coerce_report_item_uuid``: a bad string resolves to None.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    candidate = str(value).strip()
    if not candidate:
        return None
    try:
        return uuid.UUID(candidate)
    except (ValueError, TypeError, AttributeError):
        return None


class TossLedgerIdempotencyConflict(Exception):
    """ROB-545 B2 — a live order was recorded twice under the same
    client_order_id but with a *different* broker_order_id.

    The same clientOrderId is Toss's idempotency key, so a different orderId is
    a genuine broker anomaly (a duplicate live order was created). The caller
    must surface the new broker_order_id so the duplicate can be cancelled.
    """

    def __init__(
        self,
        *,
        client_order_id: str,
        existing_broker_order_id: str | None,
        new_broker_order_id: str | None,
    ) -> None:
        self.client_order_id = client_order_id
        self.existing_broker_order_id = existing_broker_order_id
        self.new_broker_order_id = new_broker_order_id
        super().__init__(
            f"client_order_id {client_order_id!r} is already recorded with "
            f"broker_order_id {existing_broker_order_id!r}; refusing to overwrite "
            f"with {new_broker_order_id!r}."
        )


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
        approval_hash: str | None = None,
    ) -> TossLiveOrderLedger:
        # ROB-545 B2 — idempotent on client_order_id. A live POST retried with
        # the same clientOrderId (the smoke's idempotency check) must not raise a
        # UNIQUE IntegrityError: query first, replay the existing row when the
        # broker_order_id matches, and surface an anomaly when it differs.
        existing = (
            await self._db.execute(
                select(TossLiveOrderLedger).where(
                    TossLiveOrderLedger.client_order_id == client_order_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.broker_order_id == broker_order_id:
                return existing
            raise TossLedgerIdempotencyConflict(
                client_order_id=client_order_id,
                existing_broker_order_id=existing.broker_order_id,
                new_broker_order_id=broker_order_id,
            )

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
            approval_hash=approval_hash,
        )
        self._db.add(row)
        await self._db.flush()
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def mark_replaced(
        self, *, broker_order_id: str, replaced_by_order_id: str
    ) -> None:
        """Link an original order to a Toss replacement without closing it.

        The original order's terminal state is evidence-gated. Reconcile must
        still fetch the original order detail because Toss can report fills on
        the original order before it becomes REPLACED/CANCELED.
        """
        stmt = select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.broker_order_id == broker_order_id
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.replaced_by_order_id = replaced_by_order_id
        await self._db.commit()

    async def clear_replacement_link(
        self, *, original_order_id: str, replacement_order_id: str
    ) -> None:
        stmt = select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.broker_order_id == original_order_id,
            TossLiveOrderLedger.replaced_by_order_id == replacement_order_id,
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.replaced_by_order_id = None
        if row.status == "replaced":
            row.status = "partial" if row.filled_qty else "accepted"
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
        stmt = stmt.where(
            TossLiveOrderLedger.operation_kind.in_(("place", "modify", "cancel"))
        )
        if symbol:
            stmt = stmt.where(TossLiveOrderLedger.symbol == symbol)
        if order_id:
            stmt = stmt.where(TossLiveOrderLedger.broker_order_id == order_id)
        if market:
            stmt = stmt.where(TossLiveOrderLedger.market == market)
        stmt = stmt.order_by(
            TossLiveOrderLedger.created_at.asc(), TossLiveOrderLedger.id.asc()
        ).limit(limit)
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
        buy_fx_rate: Decimal | None = None,
        sell_fx_rate: Decimal | None = None,
        fx_pnl_krw: Decimal | None = None,
        security_pnl_usd: Decimal | None = None,
        security_pnl_krw: Decimal | None = None,
        total_pnl_krw: Decimal | None = None,
        fx_rate_source: str | None = None,
        fx_pnl_accuracy: str | None = None,
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

        if buy_fx_rate is not None:
            row.buy_fx_rate = buy_fx_rate
        if sell_fx_rate is not None:
            row.sell_fx_rate = sell_fx_rate
        if fx_pnl_krw is not None:
            row.fx_pnl_krw = fx_pnl_krw
        if security_pnl_usd is not None:
            row.security_pnl_usd = security_pnl_usd
        if security_pnl_krw is not None:
            row.security_pnl_krw = security_pnl_krw
        if total_pnl_krw is not None:
            row.total_pnl_krw = total_pnl_krw
        if fx_rate_source is not None:
            row.fx_rate_source = fx_rate_source
        if fx_pnl_accuracy is not None:
            row.fx_pnl_accuracy = fx_pnl_accuracy

        if raw_response is not None:
            row.raw_response = raw_response
        row.reconciled_at = datetime.now(UTC)
        await self._db.commit()

    async def mark_manual_review(
        self,
        *,
        ledger_id: int,
        reason: str,
        error: dict[str, Any],
        broker_status: str | None = None,
    ) -> None:
        row = await self._db.get(TossLiveOrderLedger, ledger_id)
        if row is None:
            return
        row.status = "anomaly"
        row.broker_status = broker_status
        row.requires_manual_review = True
        row.manual_review_reason = reason
        row.last_reconcile_error = error
        row.reconciled_at = datetime.now(UTC)
        await self._db.commit()

    async def record_transient_reconcile_error(
        self,
        *,
        ledger_id: int,
        error: dict[str, Any],
    ) -> None:
        """ROB-669 — a transient reconcile failure (rate-limit/5xx/token/network).

        Record the error for observability WITHOUT closing the row: status,
        requires_manual_review, manual_review_reason, and reconciled_at are left
        untouched so ``list_open`` re-selects the row and the next pass retries.
        This is the opposite of ``mark_manual_review`` (broker-confirmed anomaly).
        """
        row = await self._db.get(TossLiveOrderLedger, ledger_id)
        if row is None:
            return
        row.last_reconcile_error = error
        await self._db.commit()
