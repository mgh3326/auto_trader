"""ROB-119 — TaskIQ background task for syncing pending orders."""

from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.core.db import AsyncSessionLocal
from app.services.pending_order_sync_service import PendingOrderSyncService

logger = logging.getLogger(__name__)


@broker.task(task_name="sync_pending_orders_all_venues")
async def sync_pending_orders_all_venues(user_id: int) -> dict[str, int]:
    """Sync pending orders from all supported brokers."""
    from app.services.brokers.kis.client import kis
    from app.services.brokers.upbit.client import upbit
    # Add other brokers as needed

    async with AsyncSessionLocal() as db:
        service = PendingOrderSyncService(db)
        
        # Define adapters for each broker to match BrokerPendingOrder protocol
        class KISAdapter:
            async def fetch_open_orders(self):
                # Placeholder: real implementation would call kis.fetch_open_orders()
                return []

        class UpbitAdapter:
            async def fetch_open_orders(self):
                # Placeholder: real implementation would call upbit.get_open_orders()
                return []

        venues = {
            "kis_mock": KISAdapter(),
            "upbit": UpbitAdapter(),
        }
        
        return await service.sync_all_venues(user_id=user_id, venues=venues)
