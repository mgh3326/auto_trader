"""ROB-121 — Tests for ResearchRetrospectiveService."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.models.analysis import StockInfo
from app.models.research_pipeline import (
    ResearchSession,
    ResearchSummary,
    StageAnalysis,
    SummaryStageLink,
)
from app.models.trading import InstrumentType
from app.models.trading_decision import (
    TradingDecisionOutcome,
    TradingDecisionProposal,
    TradingDecisionSession,
)
from app.services.research_retrospective_service import (
    ResearchRetrospectiveService,
)


async def _ensure_stock(db_session, symbol: str, instrument_type: str) -> StockInfo:
    from sqlalchemy import select

    si = (
        await db_session.execute(select(StockInfo).where(StockInfo.symbol == symbol))
    ).scalar_one_or_none()
    if si is None:
        si = StockInfo(symbol=symbol, name=symbol, instrument_type=instrument_type)
        db_session.add(si)
        await db_session.flush()
    return si


async def _seed_summary(
    db_session,
    *,
    symbol: str,
    instrument_type: str,
    decision: str,
    confidence: int = 60,
    executed_at: datetime | None = None,
    stages: list[str] | None = None,
    stage_stale: dict[str, bool] | None = None,
    stage_unavailable: list[str] | None = None,
) -> ResearchSummary:
    si = await _ensure_stock(db_session, symbol, instrument_type)
    rs = ResearchSession(stock_info_id=si.id, status="finalized")
    db_session.add(rs)
    await db_session.flush()
    summary = ResearchSummary(
        session_id=rs.id,
        decision=decision,
        confidence=confidence,
        executed_at=executed_at or datetime.now(UTC),
    )
    db_session.add(summary)
    await db_session.flush()

    stage_stale = stage_stale or {}
    stage_unavailable_set = set(stage_unavailable or [])
    for stage in stages or []:
        verdict = "unavailable" if stage in stage_unavailable_set else "bull"
        freshness = (
            {"stale_flags": ["stale"]} if stage_stale.get(stage) else {}
        )
        sa = StageAnalysis(
            session_id=rs.id,
            stage_type=stage,
            verdict=verdict,
            confidence=70,
            signals={},
            source_freshness=freshness,
        )
        db_session.add(sa)
        await db_session.flush()
        link = SummaryStageLink(
            summary_id=summary.id,
            stage_analysis_id=sa.id,
            weight=0.5,
            direction="support",
        )
        db_session.add(link)
    await db_session.flush()
    return summary


async def _seed_proposal_with_outcome(
    db_session,
    user,
    *,
    symbol: str,
    instrument_type: InstrumentType,
    user_response: str,
    pnl_pct: float | None,
    generated_at: datetime | None = None,
) -> TradingDecisionProposal:
    session = TradingDecisionSession(
        user_id=user.id,
        source_profile="test",
        status="open",
        generated_at=generated_at or datetime.now(UTC),
    )
    db_session.add(session)
    await db_session.flush()

    proposal = TradingDecisionProposal(
        session_id=session.id,
        symbol=symbol,
        instrument_type=instrument_type,
        proposal_kind="enter",
        side="buy",
        original_payload={"x": 1},
        user_response=user_response,
        responded_at=None if user_response == "pending" else datetime.now(UTC),
    )
    db_session.add(proposal)
    await db_session.flush()

    if pnl_pct is not None:
        outcome = TradingDecisionOutcome(
            proposal_id=proposal.id,
            track_kind="accepted_live",
            horizon="final",
            price_at_mark=100,
            pnl_pct=pnl_pct,
            marked_at=datetime.now(UTC),
        )
        db_session.add(outcome)
        await db_session.flush()
    return proposal


@pytest.mark.asyncio
async def test_overview_empty_window_emits_warning(db_session) -> None:
    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC)
    start = end - timedelta(days=30)

    out = await svc.build_overview(
        period_start=start, period_end=end, market=None, strategy=None
    )

    assert out.summaries_total == 0
    assert out.sessions_total == 0
    assert "no_research_summaries_in_window" in out.warnings


@pytest.mark.asyncio
async def test_overview_counts_decisions(db_session) -> None:
    await _seed_summary(
        db_session, symbol="005930", instrument_type="equity_kr", decision="buy"
    )
    await _seed_summary(
        db_session, symbol="000660", instrument_type="equity_kr", decision="hold"
    )
    await _seed_summary(
        db_session, symbol="AAPL", instrument_type="equity_us", decision="sell"
    )

    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=30)

    out = await svc.build_overview(
        period_start=start, period_end=end, market=None, strategy=None
    )

    assert out.summaries_total == 3
    assert out.sessions_total == 3
    assert out.decision_distribution.ai_buy == 1
    assert out.decision_distribution.ai_hold == 1
    assert out.decision_distribution.ai_sell == 1
    assert "no_research_summaries_in_window" not in out.warnings


@pytest.mark.asyncio
async def test_overview_market_filter_excludes_other_markets(db_session) -> None:
    await _seed_summary(
        db_session, symbol="005930", instrument_type="equity_kr", decision="buy"
    )
    await _seed_summary(
        db_session, symbol="AAPL", instrument_type="equity_us", decision="buy"
    )

    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=30)

    kr = await svc.build_overview(
        period_start=start, period_end=end, market="KR", strategy=None
    )
    us = await svc.build_overview(
        period_start=start, period_end=end, market="US", strategy=None
    )

    assert kr.summaries_total == 1
    assert kr.decision_distribution.ai_buy == 1
    assert us.summaries_total == 1


@pytest.mark.asyncio
async def test_stage_coverage_reports_stale_and_unavailable(db_session) -> None:
    await _seed_summary(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        decision="buy",
        stages=["market", "news", "fundamentals", "social"],
        stage_stale={"news": True},
        stage_unavailable=["social"],
    )

    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=30)

    out = await svc.build_overview(
        period_start=start, period_end=end, market=None, strategy=None
    )

    coverage = {s.stage_type: s for s in out.stage_coverage}
    assert coverage["market"].coverage_pct == 100.0
    assert coverage["news"].stale_pct == 100.0
    assert coverage["social"].unavailable_pct == 100.0
    assert coverage["social"].coverage_pct == 100.0


@pytest.mark.asyncio
async def test_overview_user_response_distribution(db_session, user) -> None:
    await _seed_proposal_with_outcome(
        db_session,
        user,
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        user_response="accept",
        pnl_pct=2.5,
    )
    await _seed_proposal_with_outcome(
        db_session,
        user,
        symbol="000660",
        instrument_type=InstrumentType.equity_kr,
        user_response="reject",
        pnl_pct=None,
    )

    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=30)

    out = await svc.build_overview(
        period_start=start, period_end=end, market=None, strategy=None
    )

    assert out.decision_distribution.user_accept == 1
    assert out.decision_distribution.user_reject == 1


@pytest.mark.asyncio
async def test_pnl_summary_uses_final_horizon(db_session, user) -> None:
    await _seed_proposal_with_outcome(
        db_session,
        user,
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        user_response="accept",
        pnl_pct=10.0,
    )
    await _seed_proposal_with_outcome(
        db_session,
        user,
        symbol="000660",
        instrument_type=InstrumentType.equity_kr,
        user_response="accept",
        pnl_pct=-2.0,
    )

    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=30)

    out = await svc.build_overview(
        period_start=start, period_end=end, market=None, strategy=None
    )

    assert out.pnl.realized_pnl_pct_avg == pytest.approx(4.0)
    assert out.pnl.sample_size == 2


@pytest.mark.asyncio
async def test_stage_performance_groups_by_stage_combo(db_session, user) -> None:
    await _seed_summary(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        decision="buy",
        stages=["market", "news"],
    )
    await _seed_summary(
        db_session,
        symbol="000660",
        instrument_type="equity_kr",
        decision="buy",
        stages=["market"],
    )
    await _seed_proposal_with_outcome(
        db_session,
        user,
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        user_response="accept",
        pnl_pct=5.0,
    )
    await _seed_proposal_with_outcome(
        db_session,
        user,
        symbol="000660",
        instrument_type=InstrumentType.equity_kr,
        user_response="accept",
        pnl_pct=-3.0,
    )

    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=30)

    rows = await svc.build_stage_performance(
        period_start=start, period_end=end, market=None, strategy=None
    )

    combos = {r.stage_combo: r for r in rows}
    assert "market+news" in combos
    assert "market" in combos
    assert combos["market+news"].sample_size == 1
    assert combos["market"].sample_size == 1


@pytest.mark.asyncio
async def test_list_decisions_returns_drilldown_rows(db_session, user) -> None:
    await _seed_summary(
        db_session, symbol="005930", instrument_type="equity_kr", decision="buy"
    )
    await _seed_proposal_with_outcome(
        db_session,
        user,
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        user_response="accept",
        pnl_pct=4.5,
    )

    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=30)

    out = await svc.list_decisions(
        period_start=start,
        period_end=end,
        market=None,
        strategy=None,
        limit=10,
    )

    assert out.total >= 1
    row = out.rows[0]
    assert row.symbol == "005930"
    assert row.market == "KR"
    assert row.ai_decision == "buy"
    assert row.user_response == "accept"
    assert row.realized_pnl_pct == pytest.approx(4.5)
    assert row.proposal_id is not None
    assert row.research_session_id is not None


@pytest.mark.asyncio
async def test_list_decisions_market_filter(db_session, user) -> None:
    await _seed_summary(
        db_session, symbol="005930", instrument_type="equity_kr", decision="buy"
    )
    await _seed_summary(
        db_session, symbol="AAPL", instrument_type="equity_us", decision="buy"
    )

    svc = ResearchRetrospectiveService(db_session)
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=30)

    out = await svc.list_decisions(
        period_start=start, period_end=end, market="US", strategy=None, limit=10
    )

    symbols = {r.symbol for r in out.rows}
    assert "AAPL" in symbols
    assert "005930" not in symbols
