from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.toss import TossReadClient
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

logger = logging.getLogger(__name__)


def _market_from_order(order: Any) -> str | None:
    currency = str(getattr(order, "currency", "") or "").upper()
    if currency == "KRW":
        return "kr"
    if currency == "USD":
        return "us"
    return None


def _has_fill_evidence(order: Any) -> bool:
    execution = dict(getattr(order, "execution", {}) or {})
    try:
        return float(execution.get("filledQuantity") or 0) > 0
    except (TypeError, ValueError):
        return False


def _should_seed(order: Any) -> bool:
    status = str(getattr(order, "status", "") or "").upper()
    return status in {"PENDING", "PARTIAL_FILLED"} or _has_fill_evidence(order)


class TossFillPollerService:
    def __init__(self, db: AsyncSession, *, client: TossReadClient) -> None:
        self._db = db
        self._client = client

    async def _collect_orders(
        self,
        *,
        from_date: str,
        to_date: str,
        closed_page_cap: int,
    ) -> tuple[list[Any], dict[str, Any]]:
        orders: list[Any] = []
        open_page = await self._client.list_orders(status="OPEN")
        orders.extend(open_page.orders)

        cursor: str | None = None
        seen_cursors: set[str] = set()
        closed_pages = 0
        capped = False
        repeated_cursor = False
        while True:
            page = await self._client.list_orders(
                status="CLOSED",
                from_date=from_date,
                to_date=to_date,
                cursor=cursor,
                limit=100,
            )
            closed_pages += 1
            orders.extend(page.orders)
            if not page.has_next or not page.next_cursor:
                break
            if closed_pages >= closed_page_cap:
                capped = True
                break
            if page.next_cursor in seen_cursors:
                repeated_cursor = True
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        return orders, {
            "closed_pages": closed_pages,
            "closed_pages_capped": capped,
            "repeated_cursor": repeated_cursor,
            "from_date": from_date,
            "to_date": to_date,
        }

    async def discover_external_orders(
        self,
        *,
        dry_run: bool,
        lookback_days: int,
        closed_page_cap: int,
    ) -> dict[str, Any]:
        service = TossLiveOrderLedgerService(self._db)
        state = await service.get_poll_state("orders")
        now = datetime.now(UTC)
        if state and state.last_success_at:
            start = state.last_success_at.astimezone(UTC).date() - timedelta(days=1)
        else:
            start = (now - timedelta(days=lookback_days)).date()
        orders, scan = await self._collect_orders(
            from_date=start.isoformat(),
            to_date=now.date().isoformat(),
            closed_page_cap=closed_page_cap,
        )

        candidates = [order for order in orders if _should_seed(order)]
        existing = await service.existing_broker_order_ids(
            {str(order.order_id) for order in candidates}
        )
        missing = [order for order in candidates if str(order.order_id) not in existing]

        seeded = 0
        skipped_unsupported_market = 0
        if not dry_run:
            for order in missing:
                market = _market_from_order(order)
                if market is None:
                    skipped_unsupported_market += 1
                    continue
                await service.record_external_order(order, market=market)
                seeded += 1
            await service.mark_poll_success("orders", at=now)

        return {
            "success": True,
            "dry_run": dry_run,
            "scanned": len(orders),
            "candidates": len(candidates),
            "seeded": seeded,
            "would_seed": len(missing) if dry_run else 0,
            "skipped_existing": len(existing),
            "skipped_unsupported_market": skipped_unsupported_market,
            "scan": scan,
        }
