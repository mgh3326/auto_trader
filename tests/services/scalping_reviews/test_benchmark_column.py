from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.scalping_reviews import ScalpingDailyReview

_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_benchmark_return_bps_column_round_trips(db_session) -> None:
    review = ScalpingDailyReview(
        review_date=dt.date(2026, 6, 20),
        product="usdm_futures",
        account_scope="binance_demo",
        session_tag="",
        decision="review",
        status="draft",
        created_at=_NOW,
        updated_at=_NOW,
        benchmark_return_bps=Decimal("12.3456"),
    )
    db_session.add(review)
    await db_session.flush()
    await db_session.refresh(review)
    assert review.benchmark_return_bps == Decimal("12.3456")
