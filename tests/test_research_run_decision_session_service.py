"""Unit tests for research_run_decision_session_service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

pytestmark = pytest.mark.asyncio

from app.schemas.research_run_decision_session import (
    KrUniverseSnapshot,
    LiveRefreshQuote,
    LiveRefreshSnapshot,
    ResearchRunDecisionSessionRequest,
    ResearchRunSelector,
)
from app.services.research_run_decision_session_service import (
    EmptyResearchRunError,
    ResearchRunNotFound,
    create_decision_session_from_research_run,
    resolve_research_run,
)


@pytest.mark.unit
async def test_resolve_research_run_by_uuid(db_session, user, research_run_factory):
    """Test resolving a research run by UUID."""
    run = await research_run_factory(user_id=user.id)

    selector = ResearchRunSelector(run_uuid=run.run_uuid)
    resolved = await resolve_research_run(
        db_session, user_id=user.id, selector=selector
    )

    assert resolved.id == run.id
    assert resolved.run_uuid == run.run_uuid


@pytest.mark.unit
async def test_resolve_research_run_by_criteria(db_session, user, research_run_factory):
    """Test resolving latest research run by market_scope and stage."""
    # Create two runs, the second one should be latest
    await research_run_factory(
        user_id=user.id, market_scope="kr", stage="preopen", status="open"
    )
    latest = await research_run_factory(
        user_id=user.id, market_scope="kr", stage="preopen", status="open"
    )

    selector = ResearchRunSelector(market_scope="kr", stage="preopen")
    resolved = await resolve_research_run(
        db_session, user_id=user.id, selector=selector
    )

    assert resolved.id == latest.id


@pytest.mark.unit
async def test_resolve_research_run_not_found(db_session, user):
    """Test that ResearchRunNotFound is raised for non-existent run."""
    selector = ResearchRunSelector(
        run_uuid=UUID("11111111-1111-1111-1111-111111111111")
    )

    with pytest.raises(ResearchRunNotFound):
        await resolve_research_run(db_session, user_id=user.id, selector=selector)


@pytest.mark.unit
async def test_resolve_research_run_user_isolation(
    db_session, user, other_user, research_run_factory
):
    """Test that users cannot access other users' research runs."""
    run = await research_run_factory(user_id=user.id)

    selector = ResearchRunSelector(run_uuid=run.run_uuid)

    with pytest.raises(ResearchRunNotFound):
        await resolve_research_run(db_session, user_id=other_user.id, selector=selector)


@pytest.mark.unit
async def test_create_session_empty_candidates_raises(
    db_session, user, research_run_factory
):
    """Test that EmptyResearchRunError is raised when candidates list is empty."""
    run = await research_run_factory(user_id=user.id, candidates=[])

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime.now(UTC),
        quote_by_symbol={},
        warnings=[],
    )
    request = ResearchRunDecisionSessionRequest(
        selector=ResearchRunSelector(run_uuid=run.run_uuid)
    )

    with pytest.raises(EmptyResearchRunError):
        await create_decision_session_from_research_run(
            db_session,
            user_id=user.id,
            research_run=run,
            snapshot=snapshot,
            request=request,
        )


@pytest.mark.unit
async def test_create_session_not_implemented_tradingagents(
    db_session, user, research_run_factory, research_run_candidate_factory
):
    """Test that NotImplementedError is raised for TradingAgents in v1."""
    run = await research_run_factory(user_id=user.id)
    await research_run_candidate_factory(research_run_id=run.id, symbol="000660")

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime.now(UTC),
        quote_by_symbol={
            "000660": LiveRefreshQuote(price=Decimal("50000"), as_of=datetime.now(UTC))
        },
        warnings=[],
    )
    request = ResearchRunDecisionSessionRequest(
        selector=ResearchRunSelector(run_uuid=run.run_uuid),
        include_tradingagents=True,
    )

    with pytest.raises(NotImplementedError):
        await create_decision_session_from_research_run(
            db_session,
            user_id=user.id,
            research_run=run,
            snapshot=snapshot,
            request=request,
        )


@pytest.mark.unit
async def test_create_session_happy_path_kr(
    db_session, user, research_run_factory, research_run_candidate_factory
):
    """Test happy path for KR market with NXT classification."""
    run = await research_run_factory(user_id=user.id, market_scope="kr")
    candidate = await research_run_candidate_factory(
        research_run_id=run.id,
        symbol="000660",
        candidate_kind="proposed",
        proposed_price=Decimal("50000"),
        proposed_qty=Decimal("10"),
    )

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        quote_by_symbol={
            "000660": LiveRefreshQuote(
                price=Decimal("50000"),
                as_of=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
            )
        },
        kr_universe_by_symbol={
            "000660": KrUniverseSnapshot(nxt_eligible=True, name="SK하이닉스")
        },
        warnings=[],
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

    assert result.proposal_count == 1
    assert result.reconciliation_count == 0
    assert result.session is not None
    assert result.session.source_profile == "research_run"
    assert result.session.market_scope == "kr"

    # Check market_brief structure
    assert "advisory_only" in result.session.market_brief
    assert result.session.market_brief["advisory_only"] is True
    assert result.session.market_brief["execution_allowed"] is False
    assert "research_run_uuid" in result.session.market_brief


@pytest.mark.unit
async def test_create_session_us_skips_nxt(
    db_session, user, research_run_factory, research_run_candidate_factory
):
    """Test that US market skips NXT classification."""
    run = await research_run_factory(user_id=user.id, market_scope="us")
    await research_run_candidate_factory(
        research_run_id=run.id,
        symbol="AAPL",
        candidate_kind="proposed",
        proposed_price=Decimal("150.00"),
    )

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime.now(UTC),
        quote_by_symbol={
            "AAPL": LiveRefreshQuote(price=Decimal("150.00"), as_of=datetime.now(UTC))
        },
        warnings=[],
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

    assert result.proposal_count == 1
    assert result.session.market_scope == "us"


@pytest.mark.unit
async def test_create_session_crypto_skips_nxt(
    db_session, user, research_run_factory, research_run_candidate_factory
):
    """Test that crypto market skips NXT classification."""
    run = await research_run_factory(user_id=user.id, market_scope="crypto")
    await research_run_candidate_factory(
        research_run_id=run.id,
        symbol="KRW-BTC",
        candidate_kind="proposed",
        proposed_price=Decimal("100000000"),
    )

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime.now(UTC),
        quote_by_symbol={
            "KRW-BTC": LiveRefreshQuote(
                price=Decimal("100000000"), as_of=datetime.now(UTC)
            )
        },
        warnings=[],
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

    assert result.proposal_count == 1
    assert result.session.market_scope == "crypto"


@pytest.mark.unit
async def test_missing_kr_universe_fail_closed(
    db_session, user, research_run_factory, research_run_candidate_factory
):
    """Test ROB-29 fail-closed: KR pending without universe row gets data_mismatch_requires_review."""
    run = await research_run_factory(user_id=user.id, market_scope="kr")
    await research_run_candidate_factory(
        research_run_id=run.id,
        symbol="000660",
        candidate_kind="pending_order",
        proposed_price=Decimal("50000"),
        payload={"order_id": "test-order-1"},
    )

    # No kr_universe_by_symbol provided - should trigger fail-closed behavior
    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime.now(UTC),
        quote_by_symbol={
            "000660": LiveRefreshQuote(price=Decimal("50000"), as_of=datetime.now(UTC))
        },
        kr_universe_by_symbol={},  # Empty - missing universe row
        warnings=[],
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

    assert result.proposal_count == 1
    # Check that warnings include missing_kr_universe
    assert any("missing_kr_universe" in w for w in result.warnings)
    # Check proposal payload has correct classification
    payload = result.session.proposals[0].original_payload
    assert payload["nxt_classification"] == "data_mismatch_requires_review"
    assert payload["warnings"] == ["missing_kr_universe"]


@pytest.mark.unit
async def test_deterministic_proposal_order(
    db_session, user, research_run_factory, research_run_candidate_factory
):
    """Test that proposals are created in deterministic order (by candidate.id)."""
    run = await research_run_factory(user_id=user.id)

    # Create candidates in reverse order
    c2 = await research_run_candidate_factory(
        research_run_id=run.id, symbol="000660", candidate_kind="proposed"
    )
    c1 = await research_run_candidate_factory(
        research_run_id=run.id, symbol="005930", candidate_kind="proposed"
    )

    snapshot = LiveRefreshSnapshot(
        refreshed_at=datetime.now(UTC),
        quote_by_symbol={
            "000660": LiveRefreshQuote(price=Decimal("50000"), as_of=datetime.now(UTC)),
            "005930": LiveRefreshQuote(price=Decimal("70000"), as_of=datetime.now(UTC)),
        },
        warnings=[],
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

    # Proposals should be ordered by candidate.id
    proposal_symbols = [p.symbol for p in result.session.proposals]
    assert proposal_symbols == [c1.symbol, c2.symbol]  # Ordered by id
