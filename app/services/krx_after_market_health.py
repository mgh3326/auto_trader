"""Read-only freshness alert for the KRX after-market eligibility snapshot."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models.krx_after_market_eligibility import KrxAfterMarketEligibility
from app.services.krx_after_market import KRX_AFTER_LIST_STALE_AFTER


def assess_krx_after_market_health(
    *,
    row_count: int,
    oldest_asof: datetime | None,
    newest_asof: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    """Use the oldest row so a partial refresh cannot hide stale evidence."""
    if row_count == 0:
        status = "missing"
    elif (
        oldest_asof is None
        or newest_asof is None
        or oldest_asof.utcoffset() is None
        or newest_asof.utcoffset() is None
        or oldest_asof > newest_asof
        or newest_asof > now
    ):
        status = "unknown"
    elif now - oldest_asof > KRX_AFTER_LIST_STALE_AFTER:
        status = "stale"
    else:
        status = "fresh"
    return {
        "status": status,
        "requires_attention": status != "fresh",
        "row_count": row_count,
        "oldest_asof": oldest_asof.isoformat() if oldest_asof is not None else None,
        "newest_asof": newest_asof.isoformat() if newest_asof is not None else None,
        "stale_after_days": KRX_AFTER_LIST_STALE_AFTER.days,
        "warning": (
            "KRX after-market eligibility is missing, stale or unverified. "
            "Non-NXT evening eligibility cannot be established; refresh a verified "
            "KRX-published list using the operator import procedure."
            if status != "fresh"
            else None
        ),
    }


async def load_krx_after_market_health(
    db: AsyncSession | AsyncConnection, *, now: datetime
) -> dict[str, Any]:
    """One aggregate SELECT, without symbol, order or account data."""
    row_count, oldest_asof, newest_asof = (
        await db.execute(
            select(
                func.count(),
                func.min(KrxAfterMarketEligibility.list_asof),
                func.max(KrxAfterMarketEligibility.list_asof),
            ).select_from(KrxAfterMarketEligibility)
        )
    ).one()
    return assess_krx_after_market_health(
        row_count=row_count, oldest_asof=oldest_asof, newest_asof=newest_asof, now=now
    )
