from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


async def record_toss_place_order(
    *,
    order_id: str,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    market: str,
    currency: str,
    note: str | None = None,
    reason: str | None = None,
    strategy: str | None = None,
    signal: str | None = None,
) -> None:
    """Records a newly placed Toss order as accepted in the ledger."""
    async with _order_session_factory()() as db:
        await TossLiveOrderLedgerService(db).record_order(
            broker_order_id=order_id,
            symbol=symbol,
            side=side.lower(),
            operation_kind="place",
            status="accepted",
            quantity=quantity,
            price=price,
            market=market,
            currency=currency,
            notes=note,
            reason=reason,
            strategy=strategy,
            signal=signal,
        )


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
        if evidence.local_status in {"cancel_rejected", "replace_rejected"} and not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).update_reconcile_outcome(
                    ledger_id=row.id,
                    status=evidence.local_status,
                    broker_status=evidence.broker_status,
                    raw_response=evidence.raw_order,
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
            logger.warning("toss reconcile failed order_id=%s: %s", row.broker_order_id, exc)
            outcome = {
                "ledger_id": row.id,
                "order_id": row.broker_order_id,
                "verdict": "anomaly",
                "error": str(exc) or exc.__class__.__name__,
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
