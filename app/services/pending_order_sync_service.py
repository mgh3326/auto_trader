"""ROB-119 — Sync pending orders from brokers to local DB."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pending_order import PendingOrder

logger = logging.getLogger(__name__)


class BrokerPendingOrder(Protocol):
    async def fetch_open_orders(self) -> list[dict[str, Any]]: ...


class PendingOrderSyncService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def sync_all_venues(
        self, *, user_id: int, venues: dict[str, BrokerPendingOrder]
    ) -> dict[str, int]:
        """Fetch open orders from all venues and upsert to DB.
        Deletes orders that are no longer present in broker response.
        """
        results = {}
        for venue_name, broker in venues.items():
            try:
                orders = await broker.fetch_open_orders()
                count = await self._sync_venue_orders(
                    user_id=user_id, venue=venue_name, orders=orders
                )
                results[venue_name] = count
            except Exception as exc:
                logger.error(f"Failed to sync venue {venue_name}: {exc}", exc_info=True)
                results[venue_name] = -1

        await self._db.commit()
        return results

    async def _sync_venue_orders(
        self, *, user_id: int, venue: str, orders: list[dict[str, Any]]
    ) -> int:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        seen_ids = []

        for o in orders:
            broker_id = str(o["broker_order_id"])
            seen_ids.append(broker_id)

            # Upsert using PostgreSQL-specific insert for efficiency if on Postgres,
            # but for cross-DB compatibility we'll use a simpler approach or 
            # check the dialect.
            # Since we're using SQLite for tests, we'll use a more portable way.
            
            stmt = select(PendingOrder).where(
                PendingOrder.venue == venue,
                PendingOrder.broker_order_id == broker_id
            )
            existing = (await self._db.execute(stmt)).scalar_one_or_none()

            if existing:
                existing.status = o["status"]
                existing.filled_quantity = Decimal(str(o["filled_quantity"]))
                existing.quantity = Decimal(str(o["quantity"]))
                existing.price = Decimal(str(o["price"])) if o.get("price") else None
                existing.last_seen_at = now
            else:
                new_order = PendingOrder(
                    user_id=user_id,
                    symbol=o["symbol"],
                    market=o["market"],
                    venue=venue,
                    broker_order_id=broker_id,
                    side=o["side"],
                    order_type=o["order_type"],
                    price=Decimal(str(o["price"])) if o.get("price") else None,
                    quantity=Decimal(str(o["quantity"])),
                    filled_quantity=Decimal(str(o.get("filled_quantity", 0))),
                    status=o["status"],
                    ordered_at=o["ordered_at"],
                    last_seen_at=now,
                )
                self._db.add(new_order)

        # Delete orders that vanished from the broker's open list
        delete_stmt = delete(PendingOrder).where(
            PendingOrder.venue == venue,
            PendingOrder.user_id == user_id,
            PendingOrder.broker_order_id.not_in(seen_ids),
        )
        await self._db.execute(delete_stmt)
        return len(seen_ids)
