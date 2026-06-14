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
from datetime import UTC, datetime, timedelta
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


async def list_live_orders_for_symbol(
    db: AsyncSession,
    market: str,
    symbol: str,
    *,
    days: int = 90,
    limit: int = 50,
) -> list[LinkedOrderView]:
    """ROB-559 — per-symbol live order history across the 3 live ledgers.

    The stock-detail page (``/invest/stocks/{market}/{symbol}``) keys by symbol
    rather than ``report_item_uuid`` (ROB-554). ``market`` selects the ledgers:

    * ``crypto`` → ``LiveOrderLedger`` (market='crypto'). Crypto symbols are the
      full Upbit pair as stored, e.g. ``KRW-BTC`` (NOT prefix-stripped 'BTC' —
      that convention is execution_ledger's), so the URL pair matches directly.
    * ``us`` → ``LiveOrderLedger`` (market='us') + ``TossLiveOrderLedger`` (us).
    * ``kr`` → ``KISLiveOrderLedger`` (KR domestic) + ``TossLiveOrderLedger`` (kr).

    Live-only by construction (mock/paper/demo never write these). Each ledger is
    queried by exact (upper-cased) symbol within the ``days`` window, capped at
    ``limit``; results merge most-recent-first by ``created_at`` and re-cap.
    """
    sym = symbol.strip().upper()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    collected: list[tuple[datetime, LinkedOrderView]] = []

    async def _add_live(market_value: str) -> None:
        rows = (
            (
                await db.execute(
                    select(LiveOrderLedger)
                    .where(
                        LiveOrderLedger.symbol == sym,
                        LiveOrderLedger.market == market_value,
                        LiveOrderLedger.created_at >= cutoff,
                    )
                    .order_by(LiveOrderLedger.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            collected.append((row.created_at, project_live_order(row)))

    async def _add_kis() -> None:
        rows = (
            (
                await db.execute(
                    select(KISLiveOrderLedger)
                    .where(
                        KISLiveOrderLedger.symbol == sym,
                        KISLiveOrderLedger.created_at >= cutoff,
                    )
                    .order_by(KISLiveOrderLedger.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            collected.append((row.created_at, project_kis_live_order(row)))

    async def _add_toss(market_value: str) -> None:
        rows = (
            (
                await db.execute(
                    select(TossLiveOrderLedger)
                    .where(
                        TossLiveOrderLedger.symbol == sym,
                        TossLiveOrderLedger.market == market_value,
                        TossLiveOrderLedger.created_at >= cutoff,
                    )
                    .order_by(TossLiveOrderLedger.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            collected.append((row.created_at, project_toss_live_order(row)))

    if market == "crypto":
        await _add_live("crypto")
    elif market == "us":
        await _add_live("us")
        await _add_toss("us")
    elif market == "kr":
        await _add_kis()
        await _add_toss("kr")
    else:
        return []

    collected.sort(key=lambda t: t[0], reverse=True)
    return [view for _, view in collected[:limit]]
