"""KIS mock reconciliation job (ROB-102).

Composes:
- KISMockLifecycleService (DB read/write)
- KISClient (read-only mock holdings via fetch_my_stocks(is_mock=True))
- kis_mock_holdings_reconciler (pure decision logic)

No broker mutation. No live-account access. The reconciler treats
``baseline_missing`` as an operator-review signal (anomaly); the row's
baseline is captured at order-insert time by the order execution path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.schemas.execution_contracts import OrderLifecycleEvent
from app.services.brokers.kis import KISClient
from app.services.kis_mock_holdings_reconciler import (
    HoldingsSnapshot,
    LedgerOrderInput,
    ReconcilerThresholds,
    classify_orders,
)
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService


def _to_decimal(val: Any) -> Decimal:
    if val in ("", None):
        return Decimal(0)
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


@dataclass(frozen=True, slots=True)
class _HoldingsCollection:
    """Snapshot dict plus per-market fetch-success flags.

    ``kr_ok`` / ``us_ok`` are True only when the corresponding
    ``fetch_my_stocks`` call returned a real list (an empty ``[]`` is a valid
    "truly empty account" and counts as success). A raised exception or a
    ``None`` return leaves the flag False so the reconciler can tell a genuine
    qty-0 position apart from a failed/unverifiable inquiry (ROB-910).
    """

    snapshots: dict[str, HoldingsSnapshot]
    kr_ok: bool
    us_ok: bool


async def _collect_kis_mock_holdings(
    kis_client: KISClient,
    *,
    taken_at: datetime,
) -> _HoldingsCollection:
    """Read-only snapshot of KIS mock holdings (KR + US).

    KIS balance inquiries return only nonzero holdings, so a fully-sold symbol
    is simply absent from the response. We therefore track KR and US fetch
    success independently: a verified-successful market lets the caller treat an
    absent open-order symbol as qty 0, while a failed fetch keeps it unresolved
    (fail-closed) rather than fabricating a zero (ROB-910).
    """
    snapshots: dict[str, HoldingsSnapshot] = {}

    try:
        kr = await kis_client.fetch_my_stocks(is_mock=True, is_overseas=False)
    except Exception:
        logging.exception("KIS mock KR holdings fetch failed")
        kr = None
    kr_ok = kr is not None
    for stock in kr or []:
        symbol = to_db_symbol(str(stock.get("pdno") or ""))
        if not symbol:
            continue
        snapshots[symbol] = HoldingsSnapshot(
            symbol=symbol,
            quantity=_to_decimal(stock.get("hldg_qty")),
            taken_at=taken_at,
        )

    try:
        us = await kis_client.fetch_my_stocks(is_mock=True, is_overseas=True)
    except Exception:
        logging.exception("KIS mock US holdings fetch failed")
        us = None
    us_ok = us is not None
    for stock in us or []:
        symbol = to_db_symbol(str(stock.get("ovrs_pdno") or ""))
        if not symbol:
            continue
        snapshots[symbol] = HoldingsSnapshot(
            symbol=symbol,
            quantity=_to_decimal(stock.get("ovrs_cblc_qty")),
            taken_at=taken_at,
        )

    return _HoldingsCollection(snapshots=snapshots, kr_ok=kr_ok, us_ok=us_ok)


def _synthesize_zero_for_absent_symbols(
    *,
    open_rows: list[Any],
    collection: _HoldingsCollection,
    taken_at: datetime,
) -> None:
    """Fill in qty-0 snapshots for open-order symbols absent from a verified
    market fetch (ROB-910).

    "Broker inquiry succeeded + symbol absent" == "held qty is 0" (KIS lists
    only nonzero holdings). Synthesizing a zero snapshot lets the existing delta
    logic book a sell-to-zero as a full fill. A symbol whose market fetch did
    NOT verifiably succeed is left absent so the reconciler still emits
    ``holdings_snapshot_missing`` (fetch failure != qty 0). When the market
    cannot be determined from ``instrument_type`` we require BOTH markets to have
    succeeded before treating the symbol as zero (conservative).
    """
    snapshots = collection.snapshots
    for row in open_rows:
        symbol = row.symbol
        if not symbol or symbol in snapshots:
            continue
        instrument = str(getattr(row, "instrument_type", "") or "")
        if instrument == "equity_kr":
            verified = collection.kr_ok
        elif instrument == "equity_us":
            verified = collection.us_ok
        else:
            verified = collection.kr_ok and collection.us_ok
        if not verified:
            continue
        snapshots[symbol] = HoldingsSnapshot(
            symbol=symbol,
            quantity=Decimal(0),
            taken_at=taken_at,
        )


async def run_kis_mock_reconciliation(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    limit: int = 100,
    symbol: str | None = None,
    thresholds: ReconcilerThresholds | None = None,
    kis_client: KISClient | None = None,
) -> dict[str, Any]:
    """Fetch open mock orders, fetch mock holdings, propose & optionally apply transitions.

    ``symbol`` (ROB-404) restricts reconciliation to one symbol's open orders —
    the delta-budget kernel groups by (symbol, side) so a single-symbol pass is
    self-consistent. ``None`` keeps the full-batch behavior.
    """
    thresholds = thresholds or ReconcilerThresholds()
    lifecycle_svc = KISMockLifecycleService(db)
    open_rows = await lifecycle_svc.list_open_orders(limit=limit, symbol=symbol)
    if not open_rows:
        return {
            "success": True,
            "account_mode": "kis_mock",
            "broker": "kis",
            "orders_processed": 0,
            "transitions_applied": 0,
            "dry_run": dry_run,
            "transitions": [],
            "events": [],
            "message": "No open KIS mock orders found",
        }

    now = datetime.now(UTC)
    client = kis_client if kis_client is not None else KISClient(is_mock=True)
    collection = await _collect_kis_mock_holdings(client, taken_at=now)
    # ROB-910: an open-order symbol absent from a verified-successful market
    # fetch means the position is now zero (KIS lists only nonzero holdings).
    # Synthesize a zero snapshot so sell-to-zero books a fill; a failed fetch
    # leaves the symbol unresolved → holdings_snapshot_missing (fail-closed).
    _synthesize_zero_for_absent_symbols(
        open_rows=open_rows, collection=collection, taken_at=now
    )
    holdings_map = collection.snapshots

    order_inputs: list[LedgerOrderInput] = [
        LedgerOrderInput(
            ledger_id=row.id,
            symbol=row.symbol,
            side=row.side,
            ordered_qty=_to_decimal(row.quantity),
            lifecycle_state=row.lifecycle_state,
            holdings_baseline_qty=(
                Decimal(str(row.holdings_baseline_qty))
                if row.holdings_baseline_qty is not None
                else None
            ),
            accepted_at=row.trade_date,
            price=_to_decimal(row.price),
        )
        for row in open_rows
    ]

    proposals = classify_orders(
        orders=order_inputs,
        holdings=holdings_map,
        thresholds=thresholds,
        now=now,
    )

    applied_count = 0
    transition_logs: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for proposal in proposals:
        # ``observed_delta`` is the raw un-apportioned per-order delta (diagnostic
        # only — a pending sibling in a same-symbol group may show a non-zero
        # delta it did NOT receive). ``attributed_fill_qty`` is the authoritative
        # apportioned quantity that drives lifecycle and order-history status.
        detail = {
            "observed_holdings_qty": (
                str(proposal.observed_holdings_qty)
                if proposal.observed_holdings_qty is not None
                else None
            ),
            "observed_delta": (
                str(proposal.observed_delta)
                if proposal.observed_delta is not None
                else None
            ),
            "attributed_fill_qty": (
                str(proposal.attributed_fill_qty)
                if proposal.attributed_fill_qty is not None
                else None
            ),
        }
        outcome = await lifecycle_svc.apply_lifecycle_transition(
            ledger_id=proposal.ledger_id,
            next_state=proposal.next_state,
            reason_code=proposal.reason_code,
            detail=detail,
            dry_run=dry_run,
        )
        if outcome.get("applied"):
            applied_count += 1
        transition_logs.append(outcome)
        events.append(
            OrderLifecycleEvent(
                account_mode="kis_mock",
                execution_source="reconciler",
                state=proposal.next_state,
                occurred_at=now,
                detail={
                    "ledger_id": proposal.ledger_id,
                    "symbol": proposal.symbol,
                    "prior_state": proposal.prior_state,
                    "reason_code": proposal.reason_code,
                    **detail,
                },
            ).model_dump(mode="json")
        )

    return {
        "success": True,
        "account_mode": "kis_mock",
        "broker": "kis",
        "orders_processed": len(open_rows),
        "transitions_applied": applied_count,
        "dry_run": dry_run,
        "transitions": transition_logs,
        "events": events,
    }
