"""Schema tests for ROB-101 execution review additions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

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
