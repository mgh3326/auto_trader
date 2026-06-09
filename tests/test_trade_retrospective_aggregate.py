# tests/test_trade_retrospective_aggregate.py
"""ROB-474 — retrospective list + aggregate."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.review import TradeRetrospective
from app.services.trade_journal import trade_retrospective_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()


async def _seed(
    db, *, strategy, pnl, currency="KRW", evidence=True, account_mode="kis_mock"
):
    await svc.save_retrospective(
        db,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode=account_mode,
        outcome="filled",
        strategy_key=strategy,
        realized_pnl=(pnl if evidence else None),
        realized_pnl_currency=(currency if evidence else None),
        pnl_pct=(1.0 if pnl is not None and pnl > 0 else -1.0) if evidence else None,
    )
    await db.commit()


@pytest.mark.asyncio
async def test_aggregate_by_strategy_win_rate_and_sum(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0)
    await _seed(db_session, strategy="A", pnl=-50.0)
    await _seed(db_session, strategy="B", pnl=200.0)
    result = await svc.build_retrospective_aggregate(
        db_session,
        group_by="strategy",
    )
    groups = {g["group"]: g for g in result["groups"]}
    assert groups["A"]["sample_size"] == 2
    assert groups["A"]["wins"] == 1
    assert groups["A"]["misses"] == 1
    assert groups["A"]["win_rate_pct"] == 50.0
    assert groups["A"]["realized_pnl_sum"]["KRW"] == 50.0  # 100 + (-50)
    assert groups["B"]["win_rate_pct"] == 100.0


@pytest.mark.asyncio
async def test_currency_separated_sum(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0, currency="KRW")
    await _seed(db_session, strategy="A", pnl=5.0, currency="USD")
    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")
    g = result["groups"][0]
    assert g["realized_pnl_sum"] == {"KRW": 100.0, "USD": 5.0}


@pytest.mark.asyncio
async def test_no_fill_evidence_excluded_from_aggregate(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0, evidence=True)
    # kiwoom: no evidence row
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kiwoom_mock",
        outcome="unfilled",
        strategy_key="A",
    )
    await db_session.commit()
    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")
    assert result["excluded_no_fill_evidence"] == 1
    assert result["groups"][0]["sample_size"] == 1


@pytest.mark.asyncio
async def test_empty_window_returns_no_groups(db_session: AsyncSession):
    result = await svc.build_retrospective_aggregate(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2000-01-02",
        group_by="strategy",
    )
    assert result["groups"] == []


@pytest.mark.asyncio
async def test_get_retrospectives_list_and_summary(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0)
    res = await svc.get_retrospectives(db_session, strategy_key="A")
    assert res["summary"]["count"] == 1
    assert res["summary"]["by_outcome"]["filled"] == 1
    assert res["entries"][0]["strategy_key"] == "A"


@pytest.mark.asyncio
async def test_win_rate_denominator_is_decided_rows(db_session: AsyncSession):
    # one decided win + one evidence-available UNDECIDED row (no realized_pnl/pnl_pct).
    # win_rate must use decided rows (1) as denominator -> 100.0, not sample_size (2) -> 50.0.
    await _seed(db_session, strategy="A", pnl=100.0)
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
    )  # undecided: no realized_pnl, no pnl_pct
    await db_session.commit()
    g = (await svc.build_retrospective_aggregate(db_session, group_by="strategy"))[
        "groups"
    ][0]
    assert g["sample_size"] == 2
    assert g["wins"] == 1
    assert g["misses"] == 0  # undecided row is NOT a miss
    assert g["win_rate_pct"] == 100.0


@pytest.mark.asyncio
async def test_pnl_pct_only_win(db_session: AsyncSession):
    # percent-only retro (realized_pnl=None, pnl_pct>0) must count as a win
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
        pnl_pct=2.5,
    )
    await db_session.commit()
    g = (await svc.build_retrospective_aggregate(db_session, group_by="strategy"))[
        "groups"
    ][0]
    assert g["wins"] == 1
    assert g["win_rate_pct"] == 100.0
    assert g["realized_pnl_sum"] == {}  # no absolute amount


@pytest.mark.asyncio
async def test_group_by_day(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0)
    result = await svc.build_retrospective_aggregate(db_session, group_by="day")
    assert result["group_by"] == "day"
    today_kst = now_kst().date().isoformat()
    assert result["groups"][0]["group"] == today_kst


@pytest.mark.asyncio
async def test_avg_pnl_pct_value_and_none(db_session: AsyncSession):
    # two rows pnl_pct +1.0 / -1.0 -> avg 0.0 (computed, not None)
    await _seed(db_session, strategy="A", pnl=100.0)  # pnl_pct 1.0
    await _seed(db_session, strategy="A", pnl=-50.0)  # pnl_pct -1.0
    g = (await svc.build_retrospective_aggregate(db_session, group_by="strategy"))[
        "groups"
    ][0]
    assert g["avg_pnl_pct"] == 0.0
    # a group whose only rows have no pnl_pct -> None (not 0)
    await db_session.execute(delete(TradeRetrospective))
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="B",
        realized_pnl=10.0,
        realized_pnl_currency="KRW",
    )  # decided by realized_pnl, but pnl_pct is None
    await db_session.commit()
    g2 = (await svc.build_retrospective_aggregate(db_session, group_by="strategy"))[
        "groups"
    ][0]
    assert g2["avg_pnl_pct"] is None
