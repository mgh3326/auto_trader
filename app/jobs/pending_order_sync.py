"""Pending order sync orchestration — scheduler-agnostic.

All broker venue adapter logic and DB session management live here.
TaskIQ task declaration belongs in app/tasks/pending_orders.py.
"""

from __future__ import annotations

from app.core.db import AsyncSessionLocal
from app.services.pending_order_sync_service import PendingOrderSyncService


async def run_pending_order_sync(user_id: int) -> dict[str, int]:
    """Sync pending orders from all supported brokers for a given user.

    Both adapters intentionally raise NotImplementedError to fail closed —
    returning [] would look like an empty broker snapshot and could delete
    existing local pending-order rows as 'vanished'.
    """
    async with AsyncSessionLocal() as db:
        service = PendingOrderSyncService(db)

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
