"""TaskIQ cron entries for the durable daily candle store.

Schedules (Asia/Seoul):
- KR: 16:30 KST Mon-Fri (1h after KOSPI close).
- US: 07:00 KST Tue-Sat (~1h after NYSE close on the corresponding US trading day).
- Crypto: 09:00 KST daily.

Cron times are offset from intraday sync (which runs every 10 minutes)
to keep KIS rate-limit keys uncontended.
"""

from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.daily_candles import run_daily_candles_sync

logger = logging.getLogger(__name__)


@broker.task(
    task_name="candles.daily.kr.sync",
    schedule=[{"cron": "30 16 * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def sync_kr_daily_task() -> dict[str, object]:
    return await run_daily_candles_sync(market="kr")


@broker.task(
    task_name="candles.daily.us.sync",
    schedule=[{"cron": "0 7 * * 2-6", "cron_offset": "Asia/Seoul"}],
)
async def sync_us_daily_task() -> dict[str, object]:
    return await run_daily_candles_sync(market="us")


@broker.task(
    task_name="candles.daily.crypto.sync",
    schedule=[{"cron": "0 9 * * *", "cron_offset": "Asia/Seoul"}],
)
async def sync_crypto_daily_task() -> dict[str, object]:
    return await run_daily_candles_sync(market="crypto")
