"""ROB-574/ROB-576/ROB-757 — paused TaskIQ auto-reconcile for Toss live KR/US orders.

Registered with the worker so operators can kick or externally schedule it, but
the in-repo cadence is gated by both default-off safety flags plus the
fill-poll cron when ``TOSS_FILL_POLL_ENABLED`` is on. Recurrence is owned by
operator automation plus env gate flips after safety review.

Reuses the proven toss_reconcile_orders_impl kernel. Send-time Toss order rows
remain accepted-only; fills, journals, and realized PnL are booked only from
confirmed single-order broker evidence.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.core.timezone import now_kst
from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
from app.mcp_server.tooling.market_session import (
    US_SESSION_AFTERHOURS,
    US_SESSION_PREMARKET,
    US_SESSION_REGULAR,
    is_kr_session_day,
    us_market_session,
)
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl
from app.services.brokers.toss import TossReadClient
from app.services.toss_fill_poller_service import TossFillPollerService

logger = logging.getLogger(__name__)


@broker.task(task_name="toss_live.reconcile_periodic")  # no schedule -> paused
async def toss_live_reconcile_periodic() -> dict:
    if not settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED:
        return {
            "status": "paused",
            "message": "TOSS_LIVE_AUTO_RECONCILE_ENABLED is False",
        }
    if not settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED:
        return {
            "status": "paused",
            "message": "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False",
        }
    return await toss_reconcile_orders_impl(dry_run=False)


def _scheduled_toss_fill_poll_labels() -> list[dict[str, str]]:
    if not settings.TOSS_FILL_POLL_ENABLED:
        return []
    return [{"cron": settings.TOSS_FILL_POLL_CRON, "cron_offset": "Asia/Seoul"}]


def _toss_fill_poll_market_gate(now: Any = None) -> dict[str, Any]:
    current_kst = now or now_kst()
    if not settings.TOSS_FILL_POLL_MARKET_GATE_ENABLED:
        return {"active": True, "reason": "market_gate_disabled"}
    kr_active = (
        is_kr_session_day(current_kst.date())
        and current_kst.time()
        >= current_kst.replace(hour=9, minute=0, second=0, microsecond=0).time()
        and current_kst.time()
        < current_kst.replace(hour=20, minute=0, second=0, microsecond=0).time()
    )
    us_session = us_market_session(current_kst)
    us_active = us_session in {
        US_SESSION_PREMARKET,
        US_SESSION_REGULAR,
        US_SESSION_AFTERHOURS,
    }
    return {
        "active": kr_active or us_active,
        "kr_active": kr_active,
        "us_session": us_session,
    }


@broker.task(
    task_name="toss_live.poll_fills_periodic",
    schedule=_scheduled_toss_fill_poll_labels(),
)
async def toss_live_poll_fills_periodic() -> dict:
    if not settings.TOSS_FILL_POLL_ENABLED:
        return {
            "status": "paused",
            "message": "TOSS_FILL_POLL_ENABLED is False",
        }

    gate = _toss_fill_poll_market_gate()
    if not gate["active"]:
        return {
            "status": "skipped",
            "message": "outside Toss fill poll market window",
            "gate": gate,
        }

    client = TossReadClient.from_settings()
    try:
        async with _order_session_factory()() as db:
            discover = await TossFillPollerService(
                db, client=client
            ).discover_external_orders(
                dry_run=False,
                lookback_days=settings.TOSS_FILL_POLL_LOOKBACK_DAYS,
                closed_page_cap=settings.TOSS_FILL_POLL_CLOSED_PAGE_CAP,
            )
        reconcile = await toss_reconcile_orders_impl(
            dry_run=False,
            limit=settings.TOSS_FILL_POLL_RECONCILE_LIMIT,
        )
        return {
            "success": True,
            "discover": discover,
            "reconcile": reconcile,
            "gate": gate,
        }
    finally:
        await client.aclose()
