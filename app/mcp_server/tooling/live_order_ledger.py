"""ROB-407 — 제네릭 live 주문 accepted-only ledger + evidence-gated reconcile.

US/해외(equity_us)·crypto(crypto) live 주문 전용. KR domestic은 kis_live_ledger.py 유지.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.mcp_server.tooling.kis_live_ledger import _order_session_factory, _to_float
from app.mcp_server.tooling.live_order_evidence import get_evidence_adapter
from app.mcp_server.tooling.order_journal import (
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _save_order_fill,
)
from app.models.review import LiveOrderLedger
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillVerdict,
)

logger = logging.getLogger(__name__)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


async def _save_live_order_ledger(
    *,
    broker: str,
    account_scope: str,
    market: str,
    symbol: str,
    exchange: str | None,
    market_symbol: str | None,
    side: str,
    order_kind: str,
    quantity: float | None,
    price: float | None,
    amount: float | None,
    currency: str | None,
    order_no: str | None,
    order_time: str | None,
    status: str,
    response_code: str | None,
    response_message: str | None,
    raw_response: dict[str, Any] | None,
    reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    exit_reason: str | None,
    indicators_snapshot: dict[str, Any] | None,
    dt_approval_issue_id: str | None = None,
    dt_requester_agent_id: str | None = None,
    dt_caller_source: str | None = None,
    report_item_uuid: uuid.UUID | None = None,
) -> int:
    async with _order_session_factory()() as db:
        row = LiveOrderLedger(
            trade_date=datetime.now(UTC),
            broker=broker,
            account_scope=account_scope,
            market=market,
            symbol=symbol,
            exchange=exchange,
            market_symbol=market_symbol,
            side=side,
            order_kind=order_kind,
            quantity=_to_decimal(quantity),
            price=_to_decimal(price),
            amount=_to_decimal(amount),
            currency=currency,
            order_no=order_no,
            order_time=order_time,
            status=status,
            lifecycle_state="accepted" if status == "accepted" else "rejected",
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            target_price=_to_decimal(target_price),
            stop_loss=_to_decimal(stop_loss),
            min_hold_days=min_hold_days,
            notes=notes,
            exit_reason=exit_reason,
            indicators_snapshot=indicators_snapshot,
            dt_approval_issue_id=dt_approval_issue_id,
            dt_requester_agent_id=dt_requester_agent_id,
            dt_caller_source=dt_caller_source,
            report_item_uuid=report_item_uuid,
        )
        db.add(row)
        # flush assigns the PK inside the transaction; read it before commit so
        # we never issue a post-commit refresh (a concurrent delete of this row —
        # e.g. another xdist worker truncating the table — would make refresh fail).
        await db.flush()
        ledger_id = row.id
        await db.commit()
        return ledger_id


async def _load_live_ledger_row(ledger_id: int) -> LiveOrderLedger | None:
    async with _order_session_factory()() as db:
        row = await db.get(LiveOrderLedger, ledger_id)
        if row is not None:
            db.expunge(row)
        return row


def _derive_live_send_status(*, rt_cd: str | None, order_no: str | None) -> str:
    """rt_cd=='0' (또는 order_no 존재) → accepted, 그 외 rejected."""
    if rt_cd is not None and str(rt_cd) not in ("0", ""):
        return "rejected"
    if order_no:
        return "accepted"
    return "rejected" if rt_cd not in (None, "0", "") else "accepted"


async def _list_open_live_ledger_rows(
    *,
    market: str | None,
    broker: str | None,
    symbol: str | None,
    order_no: str | None,
    limit: int,
) -> list[LiveOrderLedger]:
    async with _order_session_factory()() as db:
        stmt = select(LiveOrderLedger).where(
            LiveOrderLedger.status.in_(("accepted", "pending", "partial"))
        )
        if market:
            stmt = stmt.where(LiveOrderLedger.market == market)
        if broker:
            stmt = stmt.where(LiveOrderLedger.broker == broker)
        if symbol:
            stmt = stmt.where(LiveOrderLedger.symbol == symbol)
        if order_no:
            stmt = stmt.where(LiveOrderLedger.order_no == order_no)
        stmt = stmt.order_by(LiveOrderLedger.created_at.asc()).limit(limit)
        rows = list((await db.execute(stmt)).scalars().all())
        for r in rows:
            db.expunge(r)
        return rows


async def _update_live_ledger_outcome(
    *,
    ledger_id: int,
    status: str,
    filled_qty: Decimal | None = None,
    avg_fill_price: Decimal | None = None,
    trade_id: int | None = None,
    journal_id: int | None = None,
) -> None:
    async with _order_session_factory()() as db:
        row = await db.get(LiveOrderLedger, ledger_id)
        if row is None:
            return
        row.status = status
        # lifecycle_state mirrors status for live
        row.lifecycle_state = status
        if filled_qty is not None:
            row.filled_qty = filled_qty
        if avg_fill_price is not None:
            row.avg_fill_price = avg_fill_price
        if trade_id is not None:
            row.trade_id = trade_id
        if journal_id is not None:
            row.journal_id = journal_id
        row.reconciled_at = datetime.now(UTC)
        await db.commit()


async def _reconcile_one_live_row(
    row: LiveOrderLedger, *, dry_run: bool
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ledger_id": row.id,
        "order_id": row.order_no,
        "broker": row.broker,
        "market": row.market,
        "symbol": row.symbol,
    }
    adapter = get_evidence_adapter(row.broker)
    evidence = await adapter.fetch_evidence(row)
    base["verdict"] = evidence.verdict.value

    if evidence.verdict == FillVerdict.PENDING:
        base["action"] = "noop_pending"
        return base

    if evidence.verdict == FillVerdict.NONE:
        base["action"] = "marked_cancelled"
        if not dry_run:
            await _update_live_ledger_outcome(ledger_id=row.id, status="cancelled")
        return base

    # FILLED / PARTIAL — broker 확정값. 델타 멱등 booking.
    broker_cum = evidence.filled_qty or Decimal("0")
    already = row.filled_qty or Decimal("0")
    delta = broker_cum - already
    avg_price = evidence.avg_price or Decimal("0")
    new_status = "filled" if evidence.verdict == FillVerdict.FILLED else "partial"
    base["filled_qty"] = float(broker_cum)
    base["avg_price"] = float(avg_price)
    base["delta_qty"] = float(delta)

    if delta <= 0:
        base["action"] = "noop_already_booked"
        if not dry_run:
            await _update_live_ledger_outcome(
                ledger_id=row.id,
                status=new_status,
                filled_qty=broker_cum,
                avg_fill_price=avg_price,
            )
        return base

    if dry_run:
        base["action"] = "would_book"
        return base

    trade_id = await _save_order_fill(
        symbol=row.symbol,
        instrument_type=("equity_us" if row.market == "us" else "crypto"),
        side=row.side,
        price=float(avg_price),
        quantity=float(delta),
        total_amount=float(avg_price) * float(delta),
        fee=0.0,
        currency=(row.currency or ("USD" if row.market == "us" else "KRW")),
        account=row.broker,
        order_id=row.order_no,
    )
    journal_id = row.journal_id
    if row.side == "buy" and row.journal_id is None:
        jr = await _create_trade_journal_for_buy(
            symbol=row.symbol,
            market_type=("equity_us" if row.market == "us" else "crypto"),
            preview={
                "price": float(avg_price),
                "quantity": float(broker_cum),
                "estimated_value": float(avg_price) * float(broker_cum),
            },
            thesis=(row.thesis or "").strip() or "reconciled fill",
            strategy=(row.strategy or "").strip() or "reconciled fill",
            target_price=float(row.target_price) if row.target_price else None,
            stop_loss=float(row.stop_loss) if row.stop_loss else None,
            min_hold_days=row.min_hold_days,
            notes=row.notes,
            indicators_snapshot=row.indicators_snapshot,
            account_type="live",
            account=row.broker,
        )
        journal_id = jr.get("journal_id")
        if trade_id and journal_id:
            await _link_journal_to_fill(
                symbol=row.symbol,
                trade_id=trade_id,
                account_type="live",
                account=row.broker,
            )
    elif row.side == "sell":
        # ROB-164/ROB-407: re-attach the defensive-trim approval note to the
        # closed journal. The fields were captured at send (order_execution
        # records the order-history audit then; the journal close is deferred
        # here to evidence-gated reconcile).
        dt_ctx = None
        if row.dt_approval_issue_id and row.dt_requester_agent_id:
            from app.mcp_server.tooling.order_validation import DefensiveTrimContext

            dt_ctx = DefensiveTrimContext(
                approval_issue_id=row.dt_approval_issue_id,
                requester_agent_id=row.dt_requester_agent_id,
                approval_verified_at=row.trade_date or datetime.now(UTC),
            )
        await _close_journals_on_sell(
            symbol=row.symbol,
            sell_quantity=float(delta),
            sell_price=float(avg_price),
            exit_reason=(row.exit_reason or row.reason),
            account_type="live",
            account=row.broker,
            defensive_trim_ctx=dt_ctx,
        )

    await _update_live_ledger_outcome(
        ledger_id=row.id,
        status=new_status,
        filled_qty=broker_cum,
        avg_fill_price=avg_price,
        trade_id=trade_id,
        journal_id=journal_id,
    )
    base["action"] = "booked"
    base["trade_id"] = trade_id
    base["journal_id"] = journal_id
    return base


async def live_reconcile_orders_impl(
    *,
    market: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    order_id: str | None = None,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    try:
        rows = await _list_open_live_ledger_rows(
            market=market, broker=broker, symbol=symbol, order_no=order_id, limit=limit
        )
    except Exception as exc:
        logger.exception("Failed to list open live ledger rows: %s", exc)
        return {"success": False, "error": str(exc) or exc.__class__.__name__}

    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        try:
            outcome = await _reconcile_one_live_row(row, dry_run=dry_run)
        except Exception as exc:
            logger.warning("live reconcile failed order_no=%s: %s", row.order_no, exc)
            outcome = {
                "ledger_id": row.id,
                "order_id": row.order_no,
                "verdict": "anomaly",
                "error": str(exc) or exc.__class__.__name__,
            }
        reconciled.append(outcome)
        v = str(outcome.get("verdict", "anomaly"))
        counts[v] = counts.get(v, 0) + 1

    return {
        "success": True,
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "message": f"Reconciled {len(reconciled)} live order(s) (dry_run={dry_run}): {counts}",
    }


async def _record_live_order(
    *,
    broker: str,
    account_scope: str,
    market: str,
    normalized_symbol: str,
    exchange: str | None,
    market_symbol: str | None,
    side: str,
    order_kind: str,
    currency: str,
    order_no: str | None,
    order_time: str | None,
    rt_cd: str | None,
    response_message: str | None,
    dry_run_result: dict[str, Any],
    execution_result: dict[str, Any],
    reason: str | None,
    exit_reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    inline_confirm: bool = False,
    dt_approval_issue_id: str | None = None,
    dt_requester_agent_id: str | None = None,
    dt_caller_source: str | None = None,
    report_item_uuid: uuid.UUID | None = None,
) -> dict[str, Any]:
    price_val = _to_float(dry_run_result.get("price"), default=0.0)
    qty_val = _to_float(dry_run_result.get("quantity"), default=0.0)
    amt_val = _to_float(dry_run_result.get("estimated_value"), default=0.0)
    status = _derive_live_send_status(
        rt_cd=rt_cd, order_no=str(order_no) if order_no else None
    )
    ledger_id = await _save_live_order_ledger(
        broker=broker,
        account_scope=account_scope,
        market=market,
        symbol=normalized_symbol,
        exchange=exchange,
        market_symbol=market_symbol,
        side=side,
        order_kind=order_kind,
        quantity=qty_val,
        price=price_val,
        amount=amt_val,
        currency=currency,
        order_no=str(order_no) if order_no else None,
        order_time=order_time,
        status=status,
        response_code=rt_cd,
        response_message=response_message,
        raw_response=execution_result,
        reason=reason,
        thesis=thesis,
        strategy=strategy,
        target_price=target_price,
        stop_loss=stop_loss,
        min_hold_days=min_hold_days,
        notes=notes,
        exit_reason=exit_reason,
        indicators_snapshot=indicators_snapshot,
        dt_approval_issue_id=dt_approval_issue_id,
        dt_requester_agent_id=dt_requester_agent_id,
        dt_caller_source=dt_caller_source,
        report_item_uuid=report_item_uuid,
    )
    fill_recorded = False
    inline_outcome: dict[str, Any] | None = None
    if inline_confirm and status == "accepted":
        row = await _load_live_ledger_row(ledger_id)
        if row is not None:
            inline_outcome = await _reconcile_one_live_row(row, dry_run=False)
            fill_recorded = inline_outcome.get("action") == "booked"
    return {
        "success": True,
        "dry_run": False,
        "preview": dry_run_result,
        "execution": execution_result,
        "broker": broker,
        "account_scope": account_scope,
        "market": market,
        "ledger_id": ledger_id,
        "order_id": str(order_no) if order_no else None,
        "broker_status": status,
        "fill_recorded": fill_recorded,
        "journal_created": bool(inline_outcome and inline_outcome.get("journal_id")),
        "inline_reconcile": inline_outcome,
        "message": (
            "Live order accepted (pending fill); run live_reconcile_orders to book fill"
            if status == "accepted" and not fill_recorded
            else (
                "Live order filled inline"
                if fill_recorded
                else f"Live order not accepted (broker_status={status})"
            )
        ),
    }


async def list_live_orders_by_report_item_uuid(
    report_item_uuid: uuid.UUID,
) -> list[dict[str, Any]]:
    """ROB-473 — return live US/crypto orders linked to a report item (audit)."""
    async with _order_session_factory()() as db:
        rows = (
            (
                await db.execute(
                    select(LiveOrderLedger)
                    .where(LiveOrderLedger.report_item_uuid == report_item_uuid)
                    .order_by(LiveOrderLedger.id.desc())
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "ledger_id": r.id,
            "order_no": r.order_no,
            "symbol": r.symbol,
            "side": r.side,
            "status": r.status,
            "account_scope": r.account_scope,
            "market": r.market,
            "report_item_uuid": str(r.report_item_uuid) if r.report_item_uuid else None,
        }
        for r in rows
    ]
