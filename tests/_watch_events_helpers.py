"""ROB-602 watch-event 테스트 공유 헬퍼 (test-importing-test 회피)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchEvent

_BASE = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def utc_at(offset_min: int) -> datetime:
    return _BASE + timedelta(minutes=offset_min)


async def mk_watch_event(
    session: AsyncSession,
    *,
    symbol: str,
    market: str = "crypto",
    delivery_status: str = "delivered",
    delivered_at: datetime | None = None,
    source_report_uuid=None,
    kst_date: str = "2026-06-20",
) -> InvestmentWatchEvent:
    """review.investment_watch_events 한 행 생성. caller가 session.commit() 한다.

    NOT NULL/CHECK 제약으로 insert가 실패하면 app/models/investment_reports.py 의
    InvestmentWatchEvent 정의를 보고 누락 컬럼을 여기 추가한다(추측 금지, 모델 기준).
    """
    ev = InvestmentWatchEvent(
        market=market,
        target_kind="asset",
        symbol=symbol,
        metric="price",
        operator="below",
        threshold=100,
        threshold_key=f"{symbol}:price:below:100",
        intent="buy_review",
        action_mode="notify_only",
        outcome="notified",
        kst_date=kst_date,
        correlation_id=f"corr-{symbol}-{delivery_status}-{delivered_at}",
        idempotency_key=f"event:{symbol}:{kst_date}:{symbol}:price:below:100:{uuid.uuid4()}",
        source_report_uuid=source_report_uuid
        if source_report_uuid is not None
        else uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        delivery_status=delivery_status,
        delivered_at=delivered_at if delivery_status == "delivered" else None,
    )
    session.add(ev)
    await session.flush()
    return ev
