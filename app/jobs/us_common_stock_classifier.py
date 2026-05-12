"""Job wrapper for US common-stock classification sync."""

from __future__ import annotations

from app.services.us_common_stock_classifier import (
    CommonStockSyncResult,
    sync_us_common_stock_flags,
)


async def run_us_common_stock_flag_sync(*, commit: bool = False) -> CommonStockSyncResult:
    return await sync_us_common_stock_flags(commit=commit)
