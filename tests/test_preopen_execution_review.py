"""Schema tests for ROB-101 execution review additions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.unit
def test_execution_review_summary_defaults_are_advisory_and_blocked():
    from app.schemas.execution_contracts import ExecutionGuard, ExecutionReadiness
    from app.schemas.preopen import ExecutionReviewStage, ExecutionReviewSummary

    readiness = ExecutionReadiness(
        account_mode="db_simulated",
        execution_source="preopen",
        is_ready=False,
        guard=ExecutionGuard(
            execution_allowed=False,
            approval_required=True,
            blocking_reasons=["mvp_read_only"],
        ),
    )
    summary = ExecutionReviewSummary(
        readiness=readiness,
        stages=[
            ExecutionReviewStage(
                stage_id="data_news",
                label="Data / news readiness",
                status="ready",
                summary="news ready",
            )
        ],
    )

    assert summary.advisory_only is True
    assert summary.execution_allowed is False
    assert summary.basket_preview is None
    assert summary.readiness.guard.execution_allowed is False
    assert summary.readiness.guard.approval_required is True
    assert summary.contract_version == "v1"


@pytest.mark.unit
def test_execution_review_stage_status_literal_is_strict():
    from app.schemas.preopen import ExecutionReviewStage

    with pytest.raises(Exception):
        ExecutionReviewStage(
            stage_id="data_news",
            label="x",
            status="bogus",  # type: ignore[arg-type]
            summary="x",
        )


@pytest.mark.unit
def test_execution_review_stage_id_literal_is_strict():
    from app.schemas.preopen import ExecutionReviewStage

    with pytest.raises(Exception):
        ExecutionReviewStage(
            stage_id="not_a_stage",  # type: ignore[arg-type]
            label="x",
            status="ready",
            summary="x",
        )


@pytest.mark.unit
def test_preopen_response_accepts_execution_review_field():
    from app.schemas.execution_contracts import ExecutionGuard, ExecutionReadiness
    from app.schemas.preopen import (
        ExecutionReviewStage,
        ExecutionReviewSummary,
        PreopenLatestResponse,
    )

    review = ExecutionReviewSummary(
        readiness=ExecutionReadiness(
            account_mode="db_simulated",
            execution_source="preopen",
            is_ready=False,
            guard=ExecutionGuard(
                execution_allowed=False,
                approval_required=True,
                blocking_reasons=["mvp_read_only"],
            ),
        ),
        stages=[
            ExecutionReviewStage(
                stage_id="approval_required",
                label="Approval required",
                status="pending",
                summary="Mock execution requires explicit operator approval.",
            )
        ],
    )

    response = PreopenLatestResponse(
        has_run=False,
        run_uuid=None,
        market_scope=None,
        stage=None,
        status=None,
        strategy_name=None,
        source_profile=None,
        generated_at=None,
        created_at=None,
        notes=None,
        market_brief=None,
        source_freshness=None,
        source_warnings=[],
        advisory_links=[],
        candidate_count=0,
        reconciliation_count=0,
        candidates=[],
        reconciliations=[],
        linked_sessions=[],
        execution_review=review,
    )

    assert response.execution_review is not None
    assert response.execution_review.advisory_only is True


@pytest.mark.unit
def test_preopen_response_execution_review_is_optional_for_backward_compat():
    from app.schemas.preopen import PreopenLatestResponse

    response = PreopenLatestResponse(
        has_run=False,
        run_uuid=None,
        market_scope=None,
        stage=None,
        status=None,
        strategy_name=None,
        source_profile=None,
        generated_at=None,
        created_at=None,
        notes=None,
        market_brief=None,
        source_freshness=None,
        source_warnings=[],
        advisory_links=[],
        candidate_count=0,
        reconciliation_count=0,
        candidates=[],
        reconciliations=[],
        linked_sessions=[],
    )
    assert response.execution_review is None


@pytest.mark.unit
def test_build_execution_review_no_run_is_unavailable_and_blocked():
    from app.services.preopen_dashboard_service import _build_execution_review

    review = _build_execution_review(
        has_run=False,
        market_scope="kr",
        stage="preopen",
        candidates=[],
        reconciliations=[],
        news=None,
        briefing_artifact=None,
    )

    assert review.advisory_only is True
    assert review.execution_allowed is False
    assert review.readiness.is_ready is False
    assert "mvp_read_only" in review.readiness.guard.blocking_reasons
    assert "no_open_preopen_run" in review.readiness.guard.blocking_reasons
    assert review.basket_preview is None

    stage_ids = {s.stage_id for s in review.stages}
    assert stage_ids == {
        "data_news",
        "candidate_review",
        "cash_holdings_quotes",
        "basket_preview",
        "approval_required",
        "post_order_reconcile",
    }
    candidate_stage = next(s for s in review.stages if s.stage_id == "candidate_review")
    assert candidate_stage.status == "unavailable"
    cash_stage = next(s for s in review.stages if s.stage_id == "cash_holdings_quotes")
    assert cash_stage.status == "unavailable"
    assert "not_in_current_preopen_contract" in cash_stage.warnings


def _candidate(side: str, symbol: str, qty: str, price: str) -> object:
    from uuid import uuid4

    from app.schemas.preopen import CandidateSummary

    return CandidateSummary(
        candidate_uuid=uuid4(),
        symbol=symbol,
        instrument_type="equity_kr",
        side=side,  # type: ignore[arg-type]
        candidate_kind="proposed",
        proposed_price=Decimal(price),
        proposed_qty=Decimal(qty),
        confidence=70,
        rationale=f"reason for {symbol}",
        currency="KRW",
        warnings=[],
    )


def _ready_news() -> object:
    from app.schemas.preopen import NewsReadinessSummary

    return NewsReadinessSummary(
        status="ready",
        is_ready=True,
        is_stale=False,
        latest_run_uuid="news-1",
        latest_status="success",
        latest_finished_at=None,
        latest_article_published_at=None,
        source_counts={},
        source_coverage=[],
        warnings=[],
        max_age_minutes=180,
    )


@pytest.mark.unit
def test_build_execution_review_with_buy_candidates_emits_basket_preview():
    from app.services.preopen_dashboard_service import _build_execution_review

    review = _build_execution_review(
        has_run=True,
        market_scope="kr",
        stage="preopen",
        candidates=[
            _candidate("buy", "005930", "10", "70000"),
            _candidate("buy", "035720", "5", "60000"),
            _candidate("sell", "000660", "1", "120000"),
        ],
        reconciliations=[],
        news=_ready_news(),
        briefing_artifact=None,
    )

    basket = review.basket_preview
    assert basket is not None
    assert basket.account_mode == "db_simulated"
    assert basket.execution_source == "preopen"
    assert [line.symbol for line in basket.lines] == ["005930", "035720"]
    for line in basket.lines:
        assert line.guard.execution_allowed is False
        assert line.guard.approval_required is True

    candidate_stage = next(s for s in review.stages if s.stage_id == "candidate_review")
    assert candidate_stage.status == "ready"
    basket_stage = next(s for s in review.stages if s.stage_id == "basket_preview")
    assert basket_stage.status == "ready"
    assert basket_stage.details["line_count"] == 2

    # ``mvp_read_only`` always blocks even when news is fresh and run is open.
    assert "mvp_read_only" in review.readiness.guard.blocking_reasons


@pytest.mark.unit
def test_build_execution_review_pending_reconciliations_marked_pending():
    from app.schemas.preopen import ReconciliationSummary
    from app.services.preopen_dashboard_service import _build_execution_review

    review = _build_execution_review(
        has_run=True,
        market_scope="kr",
        stage="preopen",
        candidates=[],
        reconciliations=[
            ReconciliationSummary(
                order_id="ORD-1",
                symbol="005930",
                market="kr",
                side="buy",
                classification="near_fill",
                nxt_classification=None,
                nxt_actionable=None,
                gap_pct=Decimal("0.5"),
                summary="near fill",
                reasons=[],
                warnings=[],
            )
        ],
        news=_ready_news(),
        briefing_artifact=None,
    )

    recon_stage = next(s for s in review.stages if s.stage_id == "post_order_reconcile")
    assert recon_stage.status == "pending"
    assert recon_stage.details["pending_reconciliation_count"] == 1


@pytest.mark.unit
def test_build_execution_review_lines_match_basket_invariant_holds():
    """OrderBasketPreview's own validator must accept what we emit."""
    from app.services.preopen_dashboard_service import _build_execution_review

    review = _build_execution_review(
        has_run=True,
        market_scope="kr",
        stage="preopen",
        candidates=[_candidate("buy", "005930", "1", "1")],
        reconciliations=[],
        news=_ready_news(),
        briefing_artifact=None,
    )
    basket = review.basket_preview
    assert basket is not None
    for line in basket.lines:
        assert line.account_mode == basket.account_mode
        assert line.execution_source == basket.execution_source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_latest_dashboard_no_run_includes_execution_review(monkeypatch):
    from app.services import preopen_dashboard_service, research_run_service

    async def _no_run(*args, **kwargs):
        return None

    monkeypatch.setattr(research_run_service, "get_latest_research_run", _no_run)

    fake_db = MagicMock()
    response = await preopen_dashboard_service.get_latest_preopen_dashboard(
        fake_db, user_id=1, market_scope="kr", stage="preopen"
    )

    review = response.execution_review
    assert review is not None
    assert review.advisory_only is True
    assert review.execution_allowed is False
    assert "no_open_preopen_run" in review.readiness.guard.blocking_reasons
