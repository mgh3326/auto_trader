from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
from app.mcp_server.tooling.order_journal import (
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _save_order_fill,
)
from app.mcp_server.tooling.toss_live_evidence import TossEvidenceAdapter
from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService
from app.services.brokers.toss.errors import TossApiResponseError

logger = logging.getLogger(__name__)


def _reconcile_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TossApiResponseError):
        return {
            "type": exc.__class__.__name__,
            "status_code": exc.status_code,
            "code": exc.envelope.code,
            "request_id": exc.envelope.request_id,
            "message": exc.envelope.message,
            "data": exc.envelope.data,
        }
    return {
        "type": exc.__class__.__name__,
        "message": str(exc) or exc.__class__.__name__,
    }


def _manual_review_reason(row: TossLiveOrderLedger, exc: Exception) -> str:
    return (
        "reconcile failed; operator must verify Toss order detail "
        f"before booking or closing ledger_id={row.id} order_id={row.broker_order_id}: "
        f"{str(exc) or exc.__class__.__name__}"
    )



async def record_toss_place_order(
    *,
    market: str,
    symbol: str,
    side: str,
    order_type: str,
    time_in_force: str,
    quantity: Decimal | None,
    price: Decimal | None,
    order_amount: Decimal | None,
    currency: str | None,
    client_order_id: str,
    broker_order_id: str | None,
    raw_response: dict[str, Any],
    reason: str | None,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: Decimal | None,
    stop_loss: Decimal | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    report_item_uuid: str | None,
) -> dict[str, Any]:
    status = "accepted" if broker_order_id else "rejected"
    async with _order_session_factory()() as db:
        row = await TossLiveOrderLedgerService(db).record_send(
            operation_kind="place",
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
            original_order_id=None,
            status=status,
            broker_status=None,
            response_code="0" if status == "accepted" else None,
            response_message=None,
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
            report_item_uuid=report_item_uuid,
        )
    return {
        "ledger_id": row.id,
        "broker_status": row.status,
        "fill_recorded": False,
        "journal_created": False,
    }


async def record_toss_replacement_order(
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
    original_order_id: str,
    replacement_order_id: str,
    raw_response: dict[str, Any],
) -> dict[str, Any]:
    async with _order_session_factory()() as db:
        svc = TossLiveOrderLedgerService(db)
        row = await svc.record_send(
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
            client_order_id=uuid.uuid4().hex,
            broker_order_id=replacement_order_id,
            original_order_id=original_order_id,
            status="accepted",
            broker_status=None,
            response_code="0",
            response_message=None,
            raw_response=raw_response,
        )
        await svc.mark_replaced(
            broker_order_id=original_order_id,
            replaced_by_order_id=replacement_order_id,
        )
    return {"ledger_id": row.id, "broker_status": row.status}


async def _reconcile_one_toss_row(
    row: TossLiveOrderLedger, *, dry_run: bool
) -> dict[str, Any]:
    base = {
        "ledger_id": row.id,
        "order_id": row.broker_order_id,
        "client_order_id": row.client_order_id,
        "market": row.market,
        "symbol": row.symbol,
        "operation_kind": row.operation_kind,
    }
    evidence = await TossEvidenceAdapter().fetch_evidence(row)
    base["verdict"] = evidence.verdict
    base["broker_status"] = evidence.broker_status
    base["local_status"] = evidence.local_status

    if evidence.verdict == "pending":
        if (
            evidence.local_status in {"cancel_rejected", "replace_rejected"}
            and not dry_run
        ):
            async with _order_session_factory()() as db:
                svc = TossLiveOrderLedgerService(db)
                await svc.update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    raw_response=evidence.raw_order,
                )
                if row.original_order_id and row.broker_order_id:
                    await svc.clear_replacement_link(
                        original_order_id=row.original_order_id,
                        replacement_order_id=row.broker_order_id,
                    )
        base["action"] = "noop_pending"
        return base

    if row.operation_kind == "cancel":
        base["action"] = "audit_only_cancel_row"
        if not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    raw_response=evidence.raw_order,
                )
        return base

    if evidence.verdict == "none":
        base["action"] = f"marked_{evidence.local_status}"
        if not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    commission=evidence.commission,
                    tax=evidence.tax,
                    settlement_date=evidence.settlement_date,
                    raw_response=evidence.raw_order,
                )
        return base

    broker_cum = evidence.filled_qty
    already = row.filled_qty or Decimal("0")
    delta = broker_cum - already
    avg_price = evidence.avg_price or Decimal("0")
    base["filled_qty"] = float(broker_cum)
    base["avg_price"] = float(avg_price)
    base["delta_qty"] = float(delta)

    if delta <= 0:
        base["action"] = "noop_already_booked"
        if not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    filled_qty=broker_cum,
                    avg_fill_price=avg_price,
                    commission=evidence.commission,
                    tax=evidence.tax,
                    settlement_date=evidence.settlement_date,
                    raw_response=evidence.raw_order,
                )
        return base

    if dry_run:
        base["action"] = "would_book"
        return base

    trade_id = await _save_order_fill(
        symbol=row.symbol,
        instrument_type=("equity" if row.market == "kr" else "equity_us"),
        side=row.side,
        price=float(avg_price),
        quantity=float(delta),
        total_amount=float(avg_price) * float(delta),
        fee=float(evidence.fee_total),
        currency=row.currency or ("KRW" if row.market == "kr" else "USD"),
        account="toss",
        order_id=row.broker_order_id,
    )

    journal_id = row.journal_id
    if row.side == "buy" and row.journal_id is None:
        jr = await _create_trade_journal_for_buy(
            symbol=row.symbol,
            market_type=("equity" if row.market == "kr" else "equity_us"),
            preview={
                "price": float(avg_price),
                "quantity": float(broker_cum),
                "estimated_value": float(avg_price) * float(broker_cum),
            },
            thesis=(row.thesis or "").strip() or "toss reconciled fill",
            strategy=(row.strategy or "").strip() or "toss reconciled fill",
            target_price=float(row.target_price) if row.target_price else None,
            stop_loss=float(row.stop_loss) if row.stop_loss else None,
            min_hold_days=row.min_hold_days,
            notes=row.notes,
            indicators_snapshot=row.indicators_snapshot,
            account_type="live",
            account="toss",
        )
        journal_id = jr.get("journal_id")
        if trade_id and journal_id:
            await _link_journal_to_fill(
                symbol=row.symbol,
                trade_id=trade_id,
                account_type="live",
                account="toss",
            )
    elif row.side == "sell":
        await _close_journals_on_sell(
            symbol=row.symbol,
            sell_quantity=float(delta),
            sell_price=float(avg_price),
            exit_reason=(row.exit_reason or row.reason),
            account_type="live",
            account="toss",
        )

    async with _order_session_factory()() as db:
        await TossLiveOrderLedgerService(db).update_reconcile_outcome(
            ledger_id=row.id,
            status=evidence.local_status,
            broker_status=evidence.broker_status,
            filled_qty=broker_cum,
            avg_fill_price=avg_price,
            commission=evidence.commission,
            tax=evidence.tax,
            settlement_date=evidence.settlement_date,
            trade_id=trade_id,
            journal_id=journal_id,
            raw_response=evidence.raw_order,
        )

    base["action"] = "booked"
    base["trade_id"] = trade_id
    base["journal_id"] = journal_id
    return base


async def toss_reconcile_orders_impl(
    *,
    symbol: str | None = None,
    order_id: str | None = None,
    market: str | None = None,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    async with _order_session_factory()() as db:
        rows = await TossLiveOrderLedgerService(db).list_open(
            symbol=symbol,
            order_id=order_id,
            market=market,
            limit=limit,
        )

    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        try:
            outcome = await _reconcile_one_toss_row(row, dry_run=dry_run)
        except Exception as exc:
            logger.warning(
                "toss reconcile failed order_id=%s: %s", row.broker_order_id, exc
            )
            error_details = _reconcile_error_payload(exc)
            reason = _manual_review_reason(row, exc)
            if not dry_run:
                async with _order_session_factory()() as db:
                    await TossLiveOrderLedgerService(db).mark_manual_review(
                        ledger_id=row.id,
                        reason=reason,
                        error=error_details,
                    )
            outcome = {
                "ledger_id": row.id,
                "order_id": row.broker_order_id,
                "client_order_id": row.client_order_id,
                "market": row.market,
                "symbol": row.symbol,
                "operation_kind": row.operation_kind,
                "verdict": "anomaly",
                "action": "requires_manual_review",
                "requires_manual_review": True,
                "manual_review_reason": reason,
                "error": str(exc) or exc.__class__.__name__,
                "error_details": error_details,
            }
        reconciled.append(outcome)
        verdict = str(outcome.get("verdict", "anomaly"))
        counts[verdict] = counts.get(verdict, 0) + 1

    return {
        "success": True,
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "message": f"Reconciled {len(reconciled)} Toss live order(s) (dry_run={dry_run}): {counts}",
    }
