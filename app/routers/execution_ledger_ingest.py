"""Token-authed execution-ledger ingest surface (fillwire P0, spec §3 P0).

Two endpoints, both authenticated upstream by ``AuthMiddleware`` against
``settings.EXECUTION_LEDGER_INGEST_TOKEN`` — never by a session cookie:

* ``POST /trading/api/execution-ledger/fills/ingest`` — batch fill upsert
  through :class:`~app.services.execution_ledger.repository.ExecutionLedgerRepository`,
  reported per item so one bad fill cannot discard the batch's good ones.
* ``POST /trading/api/execution-ledger/reconcile/trigger`` — reconnect
  backfill, dry-run by default, deduped per market.

The ledger commit is the authority. Downstream work (Upbit proposal rung
projection, fill notification) runs *after* the commit through the same shared
orchestration the websocket monitor uses, and a downstream failure never
un-commits or re-inserts a fill.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.schemas.execution_ledger_ingest import (
    INGESTED_ROW_SOURCE,
    MAX_REASON_CHARS,
    ExecutionLedgerFillIngestItemResult,
    ExecutionLedgerFillIngestRequest,
    ExecutionLedgerFillIngestResponse,
    ExecutionLedgerReconcileTriggerRequest,
    ExecutionLedgerReconcileTriggerResponse,
)
from app.services.execution_ledger.fill_ingest import run_post_upsert_downstream
from app.services.execution_ledger.normalizers import _redact_sensitive_keys
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.fill_notification import (
    FillOrder,
    normalize_kis_fill,
    normalize_upbit_fill,
)
from app.services.reconcile_trigger import get_reconcile_trigger_coordinator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trading", tags=["execution-ledger-ingest"])

_INGEST_PATH = "/api/execution-ledger/fills/ingest"
_RECONCILE_TRIGGER_PATH = "/api/execution-ledger/reconcile/trigger"


def _sanitize_reason(value: object) -> str:
    """Bounded, single-line rejection reason — never a raw payload echo."""
    text = " ".join(str(value or "").split())
    if not text:
        text = "rejected"
    if len(text) > MAX_REASON_CHARS:
        text = text[: MAX_REASON_CHARS - 1] + "…"
    return text


def _validation_reason(exc: ValidationError) -> str:
    """Field-level reason from a pydantic error, without the input values."""
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<body>"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return _sanitize_reason("; ".join(parts) or "invalid fill")


_MARKET_TYPE_BY_INSTRUMENT = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
}


def _fill_order_from_canonical(fill: ExecutionLedgerUpsert) -> FillOrder:
    """Build the notification view from the canonical upsert fields alone.

    ``raw_payload_json`` is optional on the wire, so a valid canonical fill
    without a broker frame must still notify. The frame, when present, only
    adds context the canonical row cannot carry (Upbit cumulative
    ``executed_volume`` for rung projection, broker-specific order metadata).
    """
    qty = float(fill.filled_qty)
    price = float(fill.filled_price)
    notional = float(fill.filled_notional) if fill.filled_notional else qty * price
    return FillOrder(
        symbol=fill.raw_symbol or fill.symbol,
        side="bid" if fill.side == "buy" else "ask",
        filled_price=price,
        filled_qty=qty,
        filled_amount=notional,
        filled_at=fill.filled_at.isoformat(),
        account=fill.broker if fill.account_mode == "live" else fill.account_mode,
        order_id=fill.broker_order_id,
        fill_status="filled",
        market_type=_MARKET_TYPE_BY_INSTRUMENT.get(str(fill.instrument_type)),
        currency=fill.currency,
    )


def _rebuild_fill_order(fill: ExecutionLedgerUpsert) -> FillOrder:
    """Reconstruct the notification view of a posted fill.

    When the producer carried the original broker frame, the same normalizers
    the websocket monitor uses run on it so the notification is byte-identical
    across both paths. Otherwise the canonical fields are used — a missing raw
    frame is never a reason to drop the notification entirely.
    """
    raw = fill.raw_payload_json
    if isinstance(raw, dict) and raw:
        try:
            if fill.broker == "upbit":
                return normalize_upbit_fill(raw)
            if fill.broker == "kis":
                return normalize_kis_fill(raw)
        except Exception:  # noqa: BLE001 - fall back to the canonical fields
            logger.warning(
                "Ingest could not normalize the raw frame; using canonical fields: "
                "broker=%s order_id=%s",
                fill.broker,
                fill.broker_order_id,
                exc_info=True,
            )
    return _fill_order_from_canonical(fill)


@router.post(_INGEST_PATH, response_model=ExecutionLedgerFillIngestResponse)
async def ingest_execution_ledger_fills(
    request: ExecutionLedgerFillIngestRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExecutionLedgerFillIngestResponse:
    # Auth: validated upstream by AuthMiddleware against
    # settings.EXECUTION_LEDGER_INGEST_TOKEN. Do NOT require a session user.
    repository = ExecutionLedgerRepository(db)
    results: list[ExecutionLedgerFillIngestItemResult] = []
    accepted: list[tuple[ExecutionLedgerUpsert, str]] = []

    for raw_fill in request.fills:
        try:
            fill = ExecutionLedgerUpsert.model_validate(raw_fill)
        except ValidationError as exc:
            results.append(
                ExecutionLedgerFillIngestItemResult(
                    status="rejected", row_id=None, reason=_validation_reason(exc)
                )
            )
            continue

        # The envelope ``source`` is transport provenance and never enters the
        # row. The row's own ``source`` is forced to ``websocket``: a producer
        # pushing over this transport cannot claim reconciler or manual-import
        # provenance for the fill it just posted.
        fill.source = INGESTED_ROW_SOURCE  # type: ignore[assignment]
        # The envelope carries the transport run authority, so it wins over any
        # run id the item declared for itself.
        if request.source_run_id is not None:
            fill.source_run_id = request.source_run_id
        if fill.raw_payload_json is not None:
            fill.raw_payload_json = _redact_sensitive_keys(fill.raw_payload_json)

        try:
            # A per-item savepoint keeps one failing fill from discarding the
            # batch's already-applied rows.
            async with db.begin_nested():
                status, row_id = await repository.upsert_fill(fill)
        except Exception as exc:  # noqa: BLE001 - reported per item
            logger.warning(
                "Execution ledger ingest rejected one fill: broker=%s order_id=%s "
                "fill_seq=%s error=%s",
                fill.broker,
                fill.broker_order_id,
                fill.fill_seq,
                exc.__class__.__name__,
            )
            results.append(
                ExecutionLedgerFillIngestItemResult(
                    status="rejected",
                    row_id=None,
                    reason=_sanitize_reason(exc.__class__.__name__),
                )
            )
            continue

        results.append(
            ExecutionLedgerFillIngestItemResult(
                status=status, row_id=row_id, reason=None
            )
        )
        accepted.append((fill, status))

    await db.commit()

    # Downstream only after the ledger is durable, and only for rows that
    # actually landed. Failures here are best-effort by contract.
    for fill, status in accepted:
        try:
            await run_post_upsert_downstream(
                broker=fill.broker,
                upsert_status=status,
                fill_order=_rebuild_fill_order(fill),
                raw_event=fill.raw_payload_json,
                correlation_id=fill.correlation_id,
            )
        except Exception:  # noqa: BLE001 - the committed fill stands
            logger.error(
                "Execution ledger ingest downstream failed after commit: "
                "broker=%s order_id=%s fill_seq=%s",
                fill.broker,
                fill.broker_order_id,
                fill.fill_seq,
                exc_info=True,
            )

    rejected = sum(1 for item in results if item.status == "rejected")
    logger.info(
        "Execution ledger ingest processed: source=%s received=%s accepted=%s "
        "rejected=%s",
        request.source,
        len(results),
        len(accepted),
        rejected,
    )
    return ExecutionLedgerFillIngestResponse(
        source=request.source,
        source_run_id=request.source_run_id,
        received=len(results),
        accepted=len(accepted),
        rejected=rejected,
        results=results,
    )


@router.post(
    _RECONCILE_TRIGGER_PATH,
    response_model=ExecutionLedgerReconcileTriggerResponse,
)
async def trigger_execution_ledger_reconcile(
    request: ExecutionLedgerReconcileTriggerRequest,
) -> ExecutionLedgerReconcileTriggerResponse:
    # Auth: validated upstream by AuthMiddleware (same token as fills ingest).
    coordinator = get_reconcile_trigger_coordinator()
    outcome = await coordinator.trigger(
        market=request.market, dry_run=request.dry_run, reason=request.reason
    )
    kernel: dict[str, Any] | None = outcome.kernel
    return ExecutionLedgerReconcileTriggerResponse(
        market=request.market,
        dry_run=request.dry_run,
        reason=request.reason,
        status=outcome.status,  # type: ignore[arg-type]
        deduped=outcome.deduped,
        backfilled=outcome.backfilled,
        dedupe_window_seconds=coordinator.window_seconds,
        kernel=kernel,
        error=outcome.error,
    )
