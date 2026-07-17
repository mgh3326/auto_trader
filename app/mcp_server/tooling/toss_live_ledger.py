from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import sentry_sdk

from app.core.config import settings
from app.core.portfolio_links import build_position_detail_url
from app.core.timezone import now_kst
from app.mcp_server.tooling.fx_pnl import capture_reconcile_spot_fx
from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
from app.mcp_server.tooling.order_journal import (
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _save_order_fill,
)
from app.mcp_server.tooling.toss_live_evidence import (
    TossBatchEvidenceSource,
    TossEvidenceAdapter,
)
from app.models.review import TossLiveOrderLedger
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.brokers.toss import TossReadClient
from app.services.brokers.toss.errors import TossApiResponseError, TossRateLimitError
from app.services.fill_notification import (
    is_fill_notifiable,
    normalize_toss_fill,
)
from app.services.live_correlation import live_correlation_id
from app.services.live_place_provenance import publish_place_time_forecast
from app.services.toss_execution_ledger import upsert_toss_execution_fill
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

_TOSS_MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us"}

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
    exit_intent: str | None = None,
    exit_reason: str | None,
    retrospective_id: int | None = None,
    approval_issue_id: str | None = None,
    thesis: str | None,
    strategy: str | None,
    target_price: Decimal | None,
    stop_loss: Decimal | None,
    min_hold_days: int | None,
    notes: str | None,
    indicators_snapshot: dict[str, Any] | None,
    report_item_uuid: str | None,
    approval_hash: str | None = None,
    correlation_id_override: str | None = None,
    rung: str | int | None = 0,
) -> dict[str, Any]:
    status = "accepted" if broker_order_id else "rejected"

    correlation_id = correlation_id_override or live_correlation_id(
        account_scope="toss_live",
        symbol=symbol,
        side=side,
        price=(price if price is not None else Decimal("0")),
        quantity=(quantity if quantity is not None else Decimal("0")),
        kst_trade_day=now_kst().strftime("%Y-%m-%d"),
        rung=rung,
    )

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
            exit_intent=exit_intent,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            min_hold_days=min_hold_days,
            notes=notes,
            exit_reason=exit_reason,
            retrospective_id=retrospective_id,
            approval_issue_id=approval_issue_id,
            indicators_snapshot=indicators_snapshot,
            report_item_uuid=report_item_uuid,
            approval_hash=approval_hash,
            correlation_id=correlation_id,
        )

    if status == "accepted":
        await publish_place_time_forecast(
            correlation_id=correlation_id,
            symbol=symbol,
            instrument_type=_TOSS_MARKET_TO_INSTRUMENT.get(market, market),
            side=side,
            target_price=float(target_price) if target_price is not None else None,
            min_hold_days=min_hold_days,
            session_label="toss_live_place",
            created_by="auto_place_live",
            report_item_uuid=report_item_uuid,
        )

    return {
        "ledger_id": row.id,
        "broker_status": row.status,
        "fill_recorded": False,
        "journal_created": False,
        "correlation_id": correlation_id,
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


async def _notify_toss_fill(
    row: TossLiveOrderLedger,
    *,
    delta: Decimal,
    avg_price: Decimal,
    fill_status: str | None,
) -> bool:
    if not settings.toss_fill_notify_enabled:
        return False

    order = normalize_toss_fill(
        row,
        delta=delta,
        avg_price=avg_price,
        fill_status=fill_status,
    )
    if not is_fill_notifiable(order):
        logger.info(
            "toss fill notification skipped below threshold ledger_id=%s order_id=%s amount=%s currency=%s",
            row.id,
            row.broker_order_id,
            order.filled_amount,
            order.currency,
        )
        return False

    try:
        return await get_trade_notifier().notify_fill(
            order,
            enrichment=None,
            detail_url=build_position_detail_url(row.symbol, row.market),
        )
    except Exception:
        logger.warning(
            "toss fill notification failed ledger_id=%s order_id=%s",
            row.id,
            row.broker_order_id,
            exc_info=True,
        )
        return False


async def _converge_toss_proposal_rung(
    row: TossLiveOrderLedger,
    *,
    ledger_status: str,
    filled_qty: Decimal | None,
) -> dict[str, Any] | None:
    """Project committed Toss evidence in an independent committed session."""
    from app.services.order_proposals import OrderProposalsService

    if ledger_status not in {"partial", "filled", "cancelled", "rejected"}:
        return None

    try:
        async with _order_session_factory()() as db:
            service = OrderProposalsService(db)
            market = _TOSS_MARKET_TO_INSTRUMENT.get(row.market, row.market)
            if ledger_status == "partial":
                rung = await service.record_fill_evidence(
                    correlation_id=getattr(row, "correlation_id", None),
                    broker_order_id=row.broker_order_id,
                    idempotency_key=row.client_order_id,
                    filled_qty=filled_qty,
                    terminal_state="partially_filled",
                    now=datetime.now(UTC),
                    account_mode="toss_live",
                )
                await db.commit()
                if rung is None:
                    return None
                return {"converged": True, "proposal_rung_state": rung.state}
            rung_id = await service.find_unambiguous_evidence_rung_id(
                correlation_id=getattr(row, "correlation_id", None),
                broker_order_id=row.broker_order_id,
                idempotency_key=row.client_order_id,
                account_mode="toss_live",
                symbol=row.symbol,
                market=market,
            )
            if rung_id is None:
                return None
            # A broker-confirmed cancel may carry a final cumulative partial fill.
            # Project that quantity first, then cancel with filled_qty=None so the
            # service preserves the partial audit value on the terminal rung.
            if ledger_status == "cancelled" and filled_qty and filled_qty > 0:
                await service.record_fill_evidence(
                    correlation_id=getattr(row, "correlation_id", None),
                    broker_order_id=row.broker_order_id,
                    filled_qty=filled_qty,
                    terminal_state="partially_filled",
                    now=datetime.now(UTC),
                    account_mode="toss_live",
                )

            terminal_state = {
                "partial": "partially_filled",
                "filled": "filled",
                "cancelled": "cancelled",
                # Toss reports DAY expiry as REJECTED after an order was already
                # submitted.  `expired` is the legal evidence-grounded rung
                # terminal state from resting/partially_filled.
                "rejected": "expired",
            }[ledger_status]
            rung = await service.record_fill_evidence_for_rung(
                rung_id=rung_id,
                correlation_id=getattr(row, "correlation_id", None),
                broker_order_id=row.broker_order_id,
                idempotency_key=row.client_order_id,
                filled_qty=(
                    None if terminal_state in {"cancelled", "expired"} else filled_qty
                ),
                terminal_state=terminal_state,
                now=datetime.now(UTC),
                account_mode="toss_live",
                symbol=row.symbol,
                market=market,
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - ledger booking remains authoritative
        logger.error(
            "Toss proposal rung convergence failed ledger_id=%s order_id=%s "
            "ledger_status=%s: %s",
            row.id,
            row.broker_order_id,
            ledger_status,
            exc,
        )
        return {"converged": False, "error": str(exc) or exc.__class__.__name__}
    if rung is None:
        return None
    return {"converged": True, "proposal_rung_state": rung.state}


async def _repair_terminal_toss_proposal_projections(
    *,
    symbol: str | None,
    order_id: str | None,
    market: str | None,
    limit: int,
) -> dict[str, int]:
    """Idempotently repair terminal ledger rows skipped by the open-row scan."""
    async with _order_session_factory()() as db:
        rows = await TossLiveOrderLedgerService(db).list_terminal_projection_candidates(
            symbol=symbol,
            order_id=order_id,
            market=market,
            limit=limit,
        )

    report = {"candidates": len(rows), "converged": 0, "failed": 0}
    for row in rows:
        result = await _converge_toss_proposal_rung(
            row,
            ledger_status=row.status,
            filled_qty=row.filled_qty,
        )
        if result is not None and result.get("converged") is False:
            report["failed"] += 1
        else:
            report["converged"] += 1
    return report


async def _reconcile_one_toss_row(
    row: TossLiveOrderLedger,
    *,
    dry_run: bool,
    evidence_source: Any | None = None,
    fallback_client: Any | None = None,
) -> dict[str, Any]:
    base = {
        "ledger_id": row.id,
        "order_id": row.broker_order_id,
        "client_order_id": row.client_order_id,
        "market": row.market,
        "symbol": row.symbol,
        "operation_kind": row.operation_kind,
    }
    if evidence_source is not None:
        evidence = await evidence_source.evidence_for(row)
    else:
        evidence = await TossEvidenceAdapter(client=fallback_client).fetch_evidence(row)
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
            converged = await _converge_toss_proposal_rung(
                row,
                ledger_status=evidence.local_status,
                filled_qty=row.filled_qty,
            )
            if converged is not None:
                base["proposal_rung"] = converged
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

    if not dry_run:
        projection_status = (
            "cancelled" if evidence.local_status == "cancelled" else evidence.verdict
        )
        converged = await _converge_toss_proposal_rung(
            row,
            ledger_status=projection_status,
            filled_qty=broker_cum,
        )
        if converged is not None:
            base["proposal_rung"] = converged

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

    # ROB-568 — US FX spot capture
    fx_capture = None
    if row.market == "us":
        fx_capture = await capture_reconcile_spot_fx()

    trade_id = await _save_order_fill(
        symbol=row.symbol,
        instrument_type=("equity_kr" if row.market == "kr" else "equity_us"),
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
    fx_summary = None
    if row.side == "buy" and row.journal_id is None:
        jr = await _create_trade_journal_for_buy(
            symbol=row.symbol,
            market_type=("equity_kr" if row.market == "kr" else "equity_us"),
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
            correlation_id=getattr(row, "correlation_id", None),
            buy_fx_rate=float(fx_capture.rate)
            if fx_capture and fx_capture.rate
            else None,
            fx_rate_source=fx_capture.fx_rate_source if fx_capture else None,
            fx_pnl_accuracy=fx_capture.fx_pnl_accuracy if fx_capture else None,
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
        fx_summary = await _close_journals_on_sell(
            symbol=row.symbol,
            sell_quantity=float(delta),
            sell_price=float(avg_price),
            exit_reason=(row.exit_reason or row.reason),
            account_type="live",
            account="toss",
            sell_fx_rate=float(fx_capture.rate)
            if fx_capture and fx_capture.rate
            else None,
            fx_rate_source=fx_capture.fx_rate_source if fx_capture else None,
            fx_pnl_accuracy=fx_capture.fx_pnl_accuracy if fx_capture else None,
        )

    async with _order_session_factory()() as db:
        svc = TossLiveOrderLedgerService(db)
        execution_status, execution_ledger_id = await upsert_toss_execution_fill(
            db,
            row,
            evidence,
            previous_filled_qty=already,
            delta=delta,
            avg_price=avg_price,
        )
        if fx_summary:
            await svc.update_reconcile_outcome(
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
                buy_fx_rate=Decimal(str(fx_summary["buy_fx_rate"]))
                if fx_summary.get("buy_fx_rate") is not None
                else None,
                sell_fx_rate=Decimal(str(fx_summary["sell_fx_rate"]))
                if fx_summary.get("sell_fx_rate") is not None
                else None,
                fx_pnl_krw=Decimal(str(fx_summary["fx_pnl_krw"]))
                if fx_summary.get("fx_pnl_krw") is not None
                else None,
                security_pnl_usd=Decimal(str(fx_summary["security_pnl_usd"]))
                if fx_summary.get("security_pnl_usd") is not None
                else None,
                security_pnl_krw=Decimal(str(fx_summary["security_pnl_krw"]))
                if fx_summary.get("security_pnl_krw") is not None
                else None,
                total_pnl_krw=Decimal(str(fx_summary["total_pnl_krw"]))
                if fx_summary.get("total_pnl_krw") is not None
                else None,
                fx_rate_source=fx_summary.get("fx_rate_source"),
                fx_pnl_accuracy=fx_summary.get("fx_pnl_accuracy"),
                raw_response=evidence.raw_order,
            )
            base.update(fx_summary)
        else:
            await svc.update_reconcile_outcome(
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
                buy_fx_rate=Decimal(str(fx_capture.rate))
                if fx_capture and fx_capture.rate
                else None,
                fx_rate_source=fx_capture.fx_rate_source if fx_capture else None,
                fx_pnl_accuracy=fx_capture.fx_pnl_accuracy if fx_capture else None,
                raw_response=evidence.raw_order,
            )
            if fx_capture:
                base["buy_fx_rate"] = (
                    float(fx_capture.rate) if fx_capture.rate else None
                )
                base["fx_rate_source"] = fx_capture.fx_rate_source
                base["fx_pnl_accuracy"] = fx_capture.fx_pnl_accuracy

    base["execution_ledger"] = {
        "status": execution_status,
        "id": execution_ledger_id,
    }
    base["action"] = "booked"
    base["trade_id"] = trade_id
    base["journal_id"] = journal_id
    base["fill_notified"] = await _notify_toss_fill(
        row,
        delta=delta,
        avg_price=avg_price,
        fill_status="partial" if evidence.verdict == "partial" else "filled",
    )
    return base


# ROB-669 — transient reconcile failures (could-not-verify-right-now) must NOT
# become permanent anomalies. Reserve anomaly for broker-confirmed contradiction.
_TRANSIENT_TOSS_CODES = frozenset(
    {
        "rate-limit-exceeded",
        "edge-rate-limit-exceeded",
        "internal-error",
        "maintenance",
        "expired-token",
        "invalid-token",
    }
)
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_transient_reconcile_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, TossRateLimitError):
        return True
    if isinstance(exc, TossApiResponseError):
        if exc.status_code in _TRANSIENT_HTTP_STATUSES:
            return True
        return exc.envelope.code in _TRANSIENT_TOSS_CODES
    # 404 order-not-found, 403/non-JSON, idempotency conflict, and any
    # unclassifiable code fault fall through to anomaly (surface it, recoverable
    # via reopen_anomalies once fixed) — never silently retried forever.
    return False


def _transient_outcome(
    row: TossLiveOrderLedger, exc: Exception, error_details: dict[str, Any]
) -> dict[str, Any]:
    return {
        "ledger_id": row.id,
        "order_id": row.broker_order_id,
        "client_order_id": row.client_order_id,
        "market": row.market,
        "symbol": row.symbol,
        "operation_kind": row.operation_kind,
        "verdict": "deferred",
        "action": "deferred_transient_retryable",
        "retryable": True,
        "error": str(exc) or exc.__class__.__name__,
        "error_details": error_details,
    }


async def _handle_reconcile_row_error(
    row: TossLiveOrderLedger, exc: Exception, *, dry_run: bool
) -> dict[str, Any]:
    error_details = _reconcile_error_payload(exc)
    if _is_transient_reconcile_error(exc):
        logger.warning(
            "toss reconcile transient (left retryable) order_id=%s: %s",
            row.broker_order_id,
            exc,
        )
        if not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).record_transient_reconcile_error(
                    ledger_id=row.id, error=error_details
                )
        return _transient_outcome(row, exc, error_details)

    logger.warning(
        "toss reconcile anomaly (broker-confirmed) order_id=%s: %s",
        row.broker_order_id,
        exc,
    )
    reason = _manual_review_reason(row, exc)
    if not dry_run:
        async with _order_session_factory()() as db:
            await TossLiveOrderLedgerService(db).mark_manual_review(
                ledger_id=row.id, reason=reason, error=error_details
            )
    return {
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


async def toss_reconcile_orders_impl(
    *,
    symbol: str | None = None,
    order_id: str | None = None,
    market: str | None = None,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    projection_repair = {"candidates": 0, "converged": 0, "failed": 0}
    if not dry_run:
        projection_repair = await _repair_terminal_toss_proposal_projections(
            symbol=symbol,
            order_id=order_id,
            market=market,
            limit=limit,
        )

    # Self-healing reopen + list_open run in ONE session block so both the
    # recoverable-anomaly rows and the open rows are live-session ORM objects that
    # detach together at block exit (matching the existing detached-row loop).
    async with _order_session_factory()() as db:
        service = TossLiveOrderLedgerService(db)
        reopen_report = await service.reopen_anomalies_for_reconcile(
            dry_run=dry_run, market=market, symbol=symbol, limit=limit
        )
        reopened_rows = reopen_report.pop("rows")  # ORM rows, not echoed
        open_rows = await service.list_open(
            symbol=symbol,
            order_id=order_id,
            market=market,
            limit=limit,
        )
        # Work-list = list_open rows + reopened rows, deduped by ledger id
        # (a non-dry-run reopened row is now 'accepted' and may also be in
        # open_rows). Touch attributes here while the session is still open.
        seen: set[int] = set()
        rows: list[TossLiveOrderLedger] = []
        for row in [*open_rows, *reopened_rows]:
            if row.id in seen:
                continue
            seen.add(row.id)
            rows.append(row)

    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    batch_build_error: dict[str, Any] | None = None

    # ROB-687 — one TossReadClient for the whole run so account-seq is resolved at
    # most once (per-instance cache; client.py:128-129,135) instead of once per
    # fresh per-row client. The ACCOUNT group is 1 TPS (rate_limiter.py:27), so a
    # per-row /accounts N+1 serializes into ~1s of sleep per open row. Defensive:
    # if Toss is disabled/misconfigured, degrade exactly as before (per-row/batch
    # construct their own; the per-row error handler classifies the failure).
    shared_client: TossReadClient | None = None
    if rows:
        try:
            shared_client = TossReadClient.from_settings()
        except Exception as exc:  # noqa: BLE001 — degrade to legacy path
            logger.warning(
                "toss reconcile: shared client unavailable (%s); legacy per-row path",
                exc,
            )
            shared_client = None

    evidence_source = None
    if rows:
        try:
            evidence_source = await TossBatchEvidenceSource.build(
                rows=rows, symbol=symbol, client=shared_client
            )
        except Exception as exc:  # noqa: BLE001 — batch is an optimization
            # Any batch-build failure (disabled/network/transient/bug) degrades to
            # the per-row single-fetch path; per-row R1 classification still applies.
            # ROB-687: this was previously a silent one-line WARNING, which hid the
            # root cause of the /accounts N+1 for weeks — surface it with a stack
            # trace + Sentry capture + a result echo so it can be diagnosed.
            logger.exception(
                "toss reconcile batch evidence build failed; per-row fallback"
            )
            sentry_sdk.capture_exception(exc)
            batch_build_error = {"type": type(exc).__name__, "message": str(exc)}
            evidence_source = None

    try:
        for row in rows:
            try:
                outcome = await _reconcile_one_toss_row(
                    row,
                    dry_run=dry_run,
                    evidence_source=evidence_source,
                    fallback_client=shared_client,
                )
            except Exception as exc:  # noqa: BLE001 — classified in the handler
                outcome = await _handle_reconcile_row_error(row, exc, dry_run=dry_run)
            reconciled.append(outcome)
            verdict = str(outcome.get("verdict", "anomaly"))
            counts[verdict] = counts.get(verdict, 0) + 1
    finally:
        if evidence_source is not None:
            await evidence_source.aclose()  # no-op for a run-owned client
        if shared_client is not None:
            await shared_client.aclose()

    result: dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "reopened": reopen_report,  # {dry_run, reopened, candidates}
        "proposal_projection_repair": projection_repair,
        "batch_build_error": batch_build_error,
        "message": (
            f"Reconciled {len(reconciled)} Toss live order(s) "
            f"(dry_run={dry_run}): {counts}"
        ),
    }
    if evidence_source is not None:
        result["window"] = {
            "from": evidence_source.window_from,
            "to": evidence_source.window_to,
            "closed_pages_capped": evidence_source.closed_pages_capped,
            "single_fetch_fallbacks": evidence_source.single_fetch_count,
        }
    return result
