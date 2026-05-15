"""Read-only execution review builders for preopen dashboard data."""

from __future__ import annotations

from app.schemas.execution_contracts import (
    ExecutionGuard,
    ExecutionReadiness,
    OrderBasketPreview,
    OrderPreviewLine,
)
from app.schemas.preopen import (
    CandidateSummary,
    ExecutionReviewStage,
    ExecutionReviewSummary,
    NewsReadinessSummary,
    PreopenBriefingArtifact,
    ReconciliationSummary,
)


def _build_execution_review(
    *,
    has_run: bool,
    market_scope: str | None,
    stage: str | None,
    candidates: list[CandidateSummary],
    reconciliations: list[ReconciliationSummary],
    news: NewsReadinessSummary | None,
    briefing_artifact: PreopenBriefingArtifact | None,
) -> ExecutionReviewSummary:
    """Deterministic, read-only execution review built from already-loaded data.

    Always returns an ``advisory_only=True`` summary with conservative
    ``ExecutionGuard`` defaults. No broker / order / watch / credential code is
    consulted: ``cash``, ``holdings``, ``quotes``, ``broker_order``, and
    ``watch`` are reported as ``unavailable`` with the
    ``not_in_current_preopen_contract`` warning so the UI can render them as
    degraded rather than silently dropping them.
    """

    blocking_reasons: list[str] = ["mvp_read_only"]
    warnings: list[str] = []

    if not has_run:
        blocking_reasons.append("no_open_preopen_run")

    news_status = news.status if news is not None else "unavailable"
    if news is None or not news.is_ready:
        blocking_reasons.append(f"news_{news_status}")

    buy_candidates = [c for c in candidates if c.side == "buy"]

    stages: list[ExecutionReviewStage] = []

    # 1) data / news
    if news is None:
        data_status: str = "unavailable"
        data_summary = "News readiness is unavailable."
        data_warnings = ["news_readiness_unavailable"]
    elif news.is_ready:
        data_status = "ready"
        data_summary = "News readiness is fresh."
        data_warnings = []
    else:
        data_status = "degraded"
        data_summary = f"News readiness is {news.status}."
        data_warnings = list(news.warnings)
    stages.append(
        ExecutionReviewStage(
            stage_id="data_news",
            label="Data / news readiness",
            status=data_status,  # type: ignore[arg-type]
            summary=data_summary,
            warnings=data_warnings,
            details={"news_status": news_status},
        )
    )

    # 2) candidate review
    if not has_run:
        candidate_status = "unavailable"
        candidate_summary = "No open preopen research run."
    elif not candidates:
        candidate_status = "degraded"
        candidate_summary = "Open run has no candidates."
    else:
        candidate_status = "ready"
        candidate_summary = f"{len(candidates)} candidates ({len(buy_candidates)} buy)."
    stages.append(
        ExecutionReviewStage(
            stage_id="candidate_review",
            label="Candidate review",
            status=candidate_status,  # type: ignore[arg-type]
            summary=candidate_summary,
            details={
                "candidate_count": len(candidates),
                "buy_candidate_count": len(buy_candidates),
            },
        )
    )

    # 3) cash / holdings / quotes (always unavailable in MVP read-only)
    stages.append(
        ExecutionReviewStage(
            stage_id="cash_holdings_quotes",
            label="Cash / holdings / quotes check",
            status="unavailable",
            summary="Live cash, holdings, and quotes lookups are not wired in this MVP.",
            warnings=["not_in_current_preopen_contract"],
            details={},
        )
    )

    # 4) basket preview
    basket_preview: OrderBasketPreview | None = None
    if buy_candidates:
        readiness_for_basket = ExecutionReadiness(
            account_mode="db_simulated",
            execution_source="preopen",
            is_ready=False,
            guard=ExecutionGuard(
                execution_allowed=False,
                approval_required=True,
                blocking_reasons=["mvp_read_only"],
            ),
        )
        basket_preview = OrderBasketPreview(
            account_mode="db_simulated",
            execution_source="preopen",
            readiness=readiness_for_basket,
            lines=[
                OrderPreviewLine(
                    symbol=c.symbol,
                    market=market_scope or "kr",
                    side="buy",
                    account_mode="db_simulated",
                    execution_source="preopen",
                    quantity=c.proposed_qty,
                    limit_price=c.proposed_price,
                    currency=c.currency,
                    guard=ExecutionGuard(
                        execution_allowed=False,
                        approval_required=True,
                        blocking_reasons=["mvp_read_only"],
                    ),
                    rationale=[c.rationale] if c.rationale else [],
                )
                for c in buy_candidates
            ],
            basket_warnings=["mvp_read_only"],
        )
        basket_status = "ready"
        basket_summary = (
            f"{len(buy_candidates)} buy candidates rendered as a basket preview."
        )
    elif has_run:
        basket_status = "degraded"
        basket_summary = "No buy candidates available for basket preview."
    else:
        basket_status = "unavailable"
        basket_summary = "No open run to derive a basket from."
    stages.append(
        ExecutionReviewStage(
            stage_id="basket_preview",
            label="Basket preview",
            status=basket_status,  # type: ignore[arg-type]
            summary=basket_summary,
            details={"line_count": len(basket_preview.lines) if basket_preview else 0},
        )
    )

    # 5) approval required
    stages.append(
        ExecutionReviewStage(
            stage_id="approval_required",
            label="Approval required",
            status="pending",
            summary=(
                "Mock execution requires later explicit operator approval. "
                "This page does not submit orders."
            ),
            details={"advisory_only": True, "execution_allowed": False},
        )
    )

    # 6) post-order reconcile
    pending_recs = len(reconciliations)
    if not has_run:
        recon_status = "unavailable"
        recon_summary = "No open run to drive reconciliation."
    elif pending_recs == 0:
        recon_status = "skipped"
        recon_summary = "No pending reconciliations on the latest run."
    else:
        recon_status = "pending"
        recon_summary = f"{pending_recs} pending reconciliations to review."
    stages.append(
        ExecutionReviewStage(
            stage_id="post_order_reconcile",
            label="Post-order reconciliation",
            status=recon_status,  # type: ignore[arg-type]
            summary=recon_summary,
            details={"pending_reconciliation_count": pending_recs},
        )
    )

    # Lift any per-stage warnings into the summary warnings (de-duplicated).
    for s in stages:
        for w in s.warnings:
            if w not in warnings:
                warnings.append(w)

    readiness = ExecutionReadiness(
        account_mode="db_simulated",
        execution_source="preopen",
        is_ready=False,
        guard=ExecutionGuard(
            execution_allowed=False,
            approval_required=True,
            blocking_reasons=blocking_reasons,
            warnings=warnings,
        ),
        notes=[
            "Advisory read-only review; no broker submit on this page.",
        ],
    )

    notes: list[str] = [
        "advisory_only",
        "no_live_execution",
        "mock_execution_requires_explicit_approval",
    ]

    return ExecutionReviewSummary(
        readiness=readiness,
        stages=stages,
        basket_preview=basket_preview,
        blocking_reasons=blocking_reasons,
        warnings=warnings,
        notes=notes,
    )
