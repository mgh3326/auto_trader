"""ROB-554 — reverse-lookup of live orders linked to report items.

Given report-item UUIDs, return the live orders (US/crypto + KR + Toss KR/US)
whose ROB-473 ``report_item_uuid`` matches, projected into ``LinkedOrderView``
with the reconcile-written fill rollup. Single projection source so the web
bundle, the MCP bundle, and the ROB-473 audit helpers cannot drift.

Covers all live ledgers that carry ``report_item_uuid``: ``LiveOrderLedger``
(US/crypto), ``KISLiveOrderLedger`` (KR domestic), and ``TossLiveOrderLedger``
(KR/US via Toss). Mock, paper, and demo-broker ledgers do not carry the link
and are intentionally out of scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)
from app.schemas.investment_reports import LinkedOrderView


def project_live_order(row: LiveOrderLedger) -> LinkedOrderView:
    """US/crypto ledger row -> LinkedOrderView."""
    return LinkedOrderView(
        broker=row.broker,
        account_scope=row.account_scope,
        market=row.market,
        order_no=row.order_no,
        ledger_id=row.id,
        symbol=row.symbol,
        side=row.side,
        status=row.status,
        filled_qty=row.filled_qty,
        avg_fill_price=row.avg_fill_price,
        order_time=row.order_time,
        reconciled_at=row.reconciled_at,
        exit_reason=row.exit_reason,
        thesis=row.thesis,
        report_item_uuid=row.report_item_uuid,
    )


def project_kis_live_order(row: KISLiveOrderLedger) -> LinkedOrderView:
    """KR ledger row -> LinkedOrderView.

    KR uses ``account_mode`` (not ``account_scope``) and has no ``market``
    column — normalize both into the unified view shape.
    """
    return LinkedOrderView(
        broker=row.broker,
        account_scope=row.account_mode,
        market="kr",
        order_no=row.order_no,
        ledger_id=row.id,
        symbol=row.symbol,
        side=row.side,
        status=row.status,
        filled_qty=row.filled_qty,
        avg_fill_price=row.avg_fill_price,
        order_time=row.order_time,
        reconciled_at=row.reconciled_at,
        exit_reason=row.exit_reason,
        thesis=row.thesis,
        report_item_uuid=row.report_item_uuid,
    )


def project_toss_live_order(row: TossLiveOrderLedger) -> LinkedOrderView:
    """Toss (KR/US) ledger row -> LinkedOrderView.

    Toss uses ``account_mode`` (not ``account_scope``) and has no ``order_no``
    column — fall back to ``broker_order_id`` then ``client_order_id`` for the
    order id, and leave ``order_time`` empty (Toss records no broker time-of-day
    string; ``reconciled_at`` carries the fill timestamp).
    """
    return LinkedOrderView(
        broker=row.broker,
        account_scope=row.account_mode,
        market=row.market,
        order_no=row.broker_order_id or row.client_order_id,
        ledger_id=row.id,
        symbol=row.symbol,
        side=row.side,
        status=row.status,
        filled_qty=row.filled_qty,
        avg_fill_price=row.avg_fill_price,
        order_time=None,
        reconciled_at=row.reconciled_at,
        exit_reason=row.exit_reason,
        thesis=row.thesis,
        report_item_uuid=row.report_item_uuid,
    )


async def list_linked_orders_for_item_uuids(
    db: AsyncSession, item_uuids: Sequence[UUID]
) -> dict[str, list[LinkedOrderView]]:
    """Return ``{str(report_item_uuid): [LinkedOrderView, ...]}`` for the items.

    Three batch queries (one per live ledger: US/crypto, KR domestic, Toss),
    grouped by report_item_uuid. Items with no linked orders are absent from the
    dict (caller treats missing as "no linked orders"). Most-recent-first within
    each ledger (id desc).
    """
    grouped: dict[str, list[LinkedOrderView]] = {}
    uuids = list(item_uuids)
    if not uuids:
        return grouped

    live_rows = (
        (
            await db.execute(
                select(LiveOrderLedger)
                .where(LiveOrderLedger.report_item_uuid.in_(uuids))
                .order_by(LiveOrderLedger.id.desc())
            )
        )
        .scalars()
        .all()
    )
    kis_rows = (
        (
            await db.execute(
                select(KISLiveOrderLedger)
                .where(KISLiveOrderLedger.report_item_uuid.in_(uuids))
                .order_by(KISLiveOrderLedger.id.desc())
            )
        )
        .scalars()
        .all()
    )
    toss_rows = (
        (
            await db.execute(
                select(TossLiveOrderLedger)
                .where(TossLiveOrderLedger.report_item_uuid.in_(uuids))
                .order_by(TossLiveOrderLedger.id.desc())
            )
        )
        .scalars()
        .all()
    )

    for row in live_rows:
        grouped.setdefault(str(row.report_item_uuid), []).append(
            project_live_order(row)
        )
    for row in kis_rows:
        grouped.setdefault(str(row.report_item_uuid), []).append(
            project_kis_live_order(row)
        )
    for row in toss_rows:
        grouped.setdefault(str(row.report_item_uuid), []).append(
            project_toss_live_order(row)
        )
    return grouped
