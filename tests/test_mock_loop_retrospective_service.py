"""ROB-405 Slice D — mock loop retrospective aggregation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual, TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.services.trade_journal.mock_loop_retrospective_service import (
    build_mock_loop_retrospective,
)


async def _seed_cycle(db, *, day, cid, pnl="5", verdict="good", market="kr"):
    db.add(
        InvestmentWatchEvent(
            event_uuid=uuid4(),
            idempotency_key=f"idem-{uuid4()}",
            source_report_uuid=uuid4(),
            source_item_uuid=uuid4(),
            market=market,
            target_kind="asset",
            symbol="005930",
            metric="price",
            operator="below",
            threshold=Decimal("49000"),
            threshold_key="49000",
            intent="buy_review",
            action_mode="auto_execute_mock",
            outcome="executed",
            current_value=Decimal("49500"),
            correlation_id=cid,
            kst_date=day,
        )
    )
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal("50000"),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid,
        status="closed",
        exit_price=Decimal("52500"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal(pnl),
    )
    db.add(j)
    await db.commit()
    db.add(TradeJournalReview(journal_id=j.id, verdict=verdict, verdict_source="auto"))
    db.add(
        TradeJournalCounterfactual(
            journal_id=j.id, correlation_id=cid, symbol="005930", market=market,
            trigger_price=Decimal("49000"), actual_fill_price=Decimal("50000"),
            fill_vs_trigger_pct=Decimal("2.0408"),
            no_action_vs_fill_pct=Decimal("4.0000"),
        )
    )
    await db.commit()
    return j


@pytest.mark.asyncio
async def test_cycle_summary_hit(db_session: AsyncSession):
    day = "2026-06-02"
    cid = f"corr-{uuid4().hex}"
    await _seed_cycle(db_session, day=day, cid=cid, pnl="5", verdict="good")
    cycles = await build_mock_loop_retrospective(
        db_session, kst_date_from=day, kst_date_to=day
    )
    assert len(cycles) == 1
    c = cycles[0]
    assert c["kst_date"] == day
    assert c["triggered"] == 1
    assert c["by_outcome"] == {"executed": 1}
    assert c["filled"] == 1
    assert c["hits"] == 1 and c["misses"] == 0
    assert c["hit_ratio"] == 1.0
    assert c["avg_pnl_pct"] == 5.0
    assert c["verdict"]["good"] == 1
    assert c["counterfactual"]["count"] == 1
    assert c["counterfactual"]["avg_fill_vs_trigger_pct"] == 2.0408


@pytest.mark.asyncio
async def test_cycle_summary_miss(db_session: AsyncSession):
    day = "2026-06-03"
    cid = f"corr-{uuid4().hex}"
    await _seed_cycle(db_session, day=day, cid=cid, pnl="-3", verdict="bad")
    cycles = await build_mock_loop_retrospective(
        db_session, kst_date_from=day, kst_date_to=day
    )
    c = cycles[0]
    assert c["hits"] == 0 and c["misses"] == 1
    assert c["hit_ratio"] == 0.0
    assert c["verdict"]["bad"] == 1


@pytest.mark.asyncio
async def test_market_filter_excludes(db_session: AsyncSession):
    day = "2026-06-04"
    await _seed_cycle(db_session, day=day, cid=f"c-{uuid4().hex}", market="us")
    cycles = await build_mock_loop_retrospective(
        db_session, kst_date_from=day, kst_date_to=day, market="kr"
    )
    assert cycles[0]["triggered"] == 0
    assert cycles[0]["filled"] == 0


@pytest.mark.asyncio
async def test_multi_day_range_separates(db_session: AsyncSession):
    await _seed_cycle(db_session, day="2026-06-05", cid=f"c-{uuid4().hex}", pnl="5")
    await _seed_cycle(db_session, day="2026-06-06", cid=f"c-{uuid4().hex}", pnl="-1")
    cycles = await build_mock_loop_retrospective(
        db_session, kst_date_from="2026-06-05", kst_date_to="2026-06-06"
    )
    by_day = {c["kst_date"]: c for c in cycles}
    assert by_day["2026-06-05"]["hits"] == 1
    assert by_day["2026-06-06"]["misses"] == 1
