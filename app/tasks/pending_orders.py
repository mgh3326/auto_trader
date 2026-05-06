"""ROB-119 — TaskIQ background task for syncing pending orders."""

from __future__ import annotations

import logging

from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.pending_order_sync_service import PendingOrderSyncService

logger = logging.getLogger(__name__)


@broker.task(task_name="sync_pending_orders_all_venues")
async def sync_pending_orders_all_venues(user_id: int) -> dict[str, int]:
    """Sync pending orders from all supported brokers."""
    async with AsyncSessionLocal() as db:
        service = PendingOrderSyncService(db)

        # Placeholder adapters must fail closed. Returning [] from an unsupported
        # adapter would look like a complete empty broker snapshot and could
        # delete existing local pending-order rows as "vanished".
        class KISAdapter:
            async def fetch_open_orders(self):
                raise NotImplementedError("KIS pending-order snapshot unsupported")

        class UpbitAdapter:
            async def fetch_open_orders(self):
                raise NotImplementedError("Upbit pending-order snapshot unsupported")

        venues = {
            "kis_mock": KISAdapter(),
            "upbit": UpbitAdapter(),
        }

        return await service.sync_all_venues(user_id=user_id, venues=venues)
