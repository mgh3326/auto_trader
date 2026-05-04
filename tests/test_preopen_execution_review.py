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
