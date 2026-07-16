"""ROB-918: kr-preopen sessions get an advisory-only new_candidates section.

Hard safety requirement under test: injecting the new-candidate section must
never add a trading_decision_proposals row — the section is observation-only
and lives entirely inside market_brief (JSONB).
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.models.trading_decision import TradingDecisionProposal
from app.schemas.research_run_decision_session import (
    LiveRefreshSnapshot,
    ResearchRunDecisionSessionRequest,
    ResearchRunSelector,
)
from app.services.research_run_decision_session_service import (
    create_decision_session_from_research_run,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def restore_trading_decision_service_module():
    """Keep this persistence-backed service isolated from router mock tests."""
    from app.services import trading_decision_service

    importlib.reload(trading_decision_service)
    yield
    importlib.reload(trading_decision_service)


async def _proposal_count(db_session) -> int:
    return (
        await db_session.execute(select(func.count(TradingDecisionProposal.id)))
    ).scalar_one()


@pytest.mark.unit
async def test_kr_preopen_session_gets_new_candidates_section_without_proposals(
    db_session, user, research_run_factory, research_run_candidate_factory
):
    run = await research_run_factory(
        db_session, user_id=user.id, market_scope="kr", stage="preopen"
    )
    await research_run_candidate_factory(
        db_session, research_run_id=run.id, symbol="005930"
    )

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime.now(UTC), quote_by_symbol={}, warnings=[]
    )
    request = ResearchRunDecisionSessionRequest(
        selector=ResearchRunSelector(run_uuid=run.run_uuid)
    )

    before = await _proposal_count(db_session)
    result = await create_decision_session_from_research_run(
        db_session,
        user_id=user.id,
        research_run=run,
        snapshot=snapshot,
        request=request,
    )
    after = await _proposal_count(db_session)

    market_brief = result.session.market_brief
    assert market_brief is not None
    assert "new_candidates" in market_brief
    new_candidates = market_brief["new_candidates"]
    assert new_candidates["advisory_only"] is True
    assert new_candidates["market_state"] in {"normal", "crash_warning", "unknown"}
    for section in ("consecutive_gainers", "theme_leaders", "double_buy"):
        assert isinstance(new_candidates[section], list)

    # Hard safety requirement: the new-candidate section adds zero proposal
    # rows. Only the one seeded research-run candidate becomes a proposal.
    assert after - before == 1


@pytest.mark.unit
async def test_non_kr_or_non_preopen_session_omits_new_candidates(
    db_session, user, research_run_factory, research_run_candidate_factory
):
    run = await research_run_factory(
        db_session, user_id=user.id, market_scope="us", stage="preopen"
    )
    await research_run_candidate_factory(
        db_session, research_run_id=run.id, symbol="AAPL"
    )

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime.now(UTC), quote_by_symbol={}, warnings=[]
    )
    request = ResearchRunDecisionSessionRequest(
        selector=ResearchRunSelector(run_uuid=run.run_uuid)
    )

    result = await create_decision_session_from_research_run(
        db_session,
        user_id=user.id,
        research_run=run,
        snapshot=snapshot,
        request=request,
    )

    assert result.session.market_brief["new_candidates"] is None
