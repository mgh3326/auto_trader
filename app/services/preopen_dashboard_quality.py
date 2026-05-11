"""Deterministic QA evaluator builders for preopen dashboard data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.preopen import (
    CandidateSummary,
    LinkedSessionRef,
    NewsReadinessSummary,
    PreopenBriefingArtifact,
    PreopenMarketNewsBriefing,
    PreopenQaCheck,
    PreopenQaEvaluatorSummary,
    PreopenQaScore,
    ReconciliationSummary,
)


def _qa_check(
    check_id: str,
    label: str,
    status: str,
    severity: str,
    summary: str,
    details: dict[str, Any] | None = None,
) -> PreopenQaCheck:
    return PreopenQaCheck(
        id=check_id,
        label=label,
        status=status,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        summary=summary,
        details=details,
    )


def _score_qa_checks(
    checks: list[PreopenQaCheck],
    *,
    has_run: bool,
) -> PreopenQaScore:
    if not has_run:
        return PreopenQaScore(
            score=None,
            grade="unavailable",
            confidence="unavailable",
            reason="no_open_preopen_run",
        )
    score = 100
    for check in checks:
        if check.status == "fail":
            score -= 30
        elif check.status == "warn":
            score -= 10
        elif check.status == "unknown":
            score -= 5
    score = max(0, min(100, score))
    if score >= 90:
        grade = "excellent"
    elif score >= 75:
        grade = "good"
    elif score >= 50:
        grade = "watch"
    else:
        grade = "poor"
    confidence = "high" if score >= 75 else "medium" if score >= 50 else "low"
    return PreopenQaScore(
        score=score,
        grade=grade,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        reason="deterministic checks over already-loaded preopen response data",
    )


def _build_qa_evaluator_summary(
    *,
    has_run: bool,
    generated_at: datetime | None,
    candidate_count: int,
    reconciliation_count: int,
    candidates: list[CandidateSummary],
    reconciliations: list[ReconciliationSummary],
    linked: list[LinkedSessionRef],
    news: NewsReadinessSummary | None,
    market_news_briefing: PreopenMarketNewsBriefing | None,
    briefing_artifact: PreopenBriefingArtifact | None,
    advisory_skipped_reason: str | None,
) -> PreopenQaEvaluatorSummary:
    """Build a deterministic, read-only QA summary from already-loaded DTOs only."""

    coverage: dict[str, Any] = {
        "candidate_count": candidate_count,
        "candidate_items": len(candidates),
        "reconciliation_count": reconciliation_count,
        "reconciliation_items": len(reconciliations),
        "linked_session_count": len(linked),
        "news_status": news.status if news else "unavailable",
        "market_news_sections": len(market_news_briefing.sections)
        if market_news_briefing
        else 0,
        "briefing_artifact_status": briefing_artifact.status
        if briefing_artifact
        else "unavailable",
        "advisory_only": True,
        "execution_allowed": False,
        "advisory_skipped_reason": advisory_skipped_reason,
    }

    if not has_run:
        checks = [
            _qa_check(
                "has_open_run",
                "Open preopen run",
                "fail",
                "high",
                "No open preopen research run is available.",
                {"reason": advisory_skipped_reason or "no_open_preopen_run"},
            ),
            _qa_check(
                "actionability_guardrail",
                "Actionability guardrail",
                "pass",
                "info",
                "Evaluator is advisory-only and execution remains disabled.",
                {"advisory_only": True, "execution_allowed": False},
            ),
        ]
        return PreopenQaEvaluatorSummary(
            status="unavailable",
            generated_at=generated_at,
            overall=_score_qa_checks(checks, has_run=False),
            checks=checks,
            blocking_reasons=["no_open_preopen_run"],
            warnings=[],
            coverage=coverage,
        )

    checks: list[PreopenQaCheck] = [
        _qa_check(
            "has_open_run",
            "Open preopen run",
            "pass",
            "info",
            "Open preopen research run loaded for read-only evaluation.",
        )
    ]

    if briefing_artifact is None:
        checks.append(
            _qa_check(
                "briefing_artifact_available",
                "Briefing artifact",
                "fail",
                "high",
                "Preopen briefing artifact is missing.",
            )
        )
    elif briefing_artifact.status == "ready":
        checks.append(
            _qa_check(
                "briefing_artifact_available",
                "Briefing artifact",
                "pass",
                "info",
                "Briefing artifact is ready.",
                {"status": briefing_artifact.status},
            )
        )
    elif briefing_artifact.status == "degraded":
        checks.append(
            _qa_check(
                "briefing_artifact_available",
                "Briefing artifact",
                "warn",
                "medium",
                "Briefing artifact is degraded and should be reviewed.",
                {"risk_notes": list(briefing_artifact.risk_notes)},
            )
        )
    else:
        checks.append(
            _qa_check(
                "briefing_artifact_available",
                "Briefing artifact",
                "fail",
                "high",
                f"Briefing artifact status is {briefing_artifact.status}.",
            )
        )

    if news is None:
        checks.append(
            _qa_check(
                "news_readiness",
                "News readiness",
                "fail",
                "high",
                "News readiness is unavailable.",
            )
        )
    elif news.status == "ready" and news.is_ready:
        checks.append(
            _qa_check(
                "news_readiness",
                "News readiness",
                "pass",
                "info",
                "News readiness is fresh.",
                {"source_counts": news.source_counts},
            )
        )
    elif news.status == "stale" or news.is_stale:
        checks.append(
            _qa_check(
                "news_readiness",
                "News readiness",
                "fail",
                "high",
                "News readiness is stale; review before relying on recommendations.",
                {"warnings": list(news.warnings)},
            )
        )
    else:
        checks.append(
            _qa_check(
                "news_readiness",
                "News readiness",
                "fail",
                "high",
                "News readiness is unavailable.",
                {"warnings": list(news.warnings)},
            )
        )

    if len(candidates) != candidate_count:
        checks.append(
            _qa_check(
                "candidate_coverage",
                "Candidate coverage",
                "fail",
                "high",
                "Candidate count does not match candidate list length.",
                {"candidate_count": candidate_count, "items": len(candidates)},
            )
        )
    elif candidate_count == 0:
        checks.append(
            _qa_check(
                "candidate_coverage",
                "Candidate coverage",
                "warn",
                "medium",
                "No candidates are present in the latest run.",
            )
        )
    else:
        checks.append(
            _qa_check(
                "candidate_coverage",
                "Candidate coverage",
                "pass",
                "info",
                f"{candidate_count} candidates are present.",
            )
        )

    if len(reconciliations) != reconciliation_count:
        checks.append(
            _qa_check(
                "reconciliation_coverage",
                "Reconciliation coverage",
                "fail",
                "high",
                "Reconciliation count does not match reconciliation list length.",
                {
                    "reconciliation_count": reconciliation_count,
                    "items": len(reconciliations),
                },
            )
        )
    else:
        checks.append(
            _qa_check(
                "reconciliation_coverage",
                "Reconciliation coverage",
                "pass" if reconciliation_count else "skipped",
                "info",
                f"{reconciliation_count} pending reconciliations summarized.",
            )
        )

    if market_news_briefing is None:
        checks.append(
            _qa_check(
                "market_news_briefing",
                "Market news briefing",
                "warn",
                "medium",
                "Market news briefing is unavailable; raw news readiness may still be shown.",
            )
        )
    else:
        market_news_count = sum(
            len(section.items) for section in market_news_briefing.sections
        )
        checks.append(
            _qa_check(
                "market_news_briefing",
                "Market news briefing",
                "pass" if market_news_count else "warn",
                "low" if market_news_count else "medium",
                f"{market_news_count} market news briefing items summarized.",
                {"sections": len(market_news_briefing.sections)},
            )
        )

    checks.append(
        _qa_check(
            "linked_session_safety",
            "Linked session safety",
            "pass" if linked else "warn",
            "info" if linked else "low",
            "Linked sessions are read-only summaries; evaluator does not create sessions."
            if linked
            else "No linked decision session found; evaluator did not create one.",
            {"linked_session_count": len(linked)},
        )
    )
    checks.append(
        _qa_check(
            "actionability_guardrail",
            "Actionability guardrail",
            "pass",
            "info",
            "QA evaluator is advisory-only and execution remains disabled.",
            {"advisory_only": True, "execution_allowed": False},
        )
    )

    overall = _score_qa_checks(checks, has_run=True)
    blocking_reasons = [check.id for check in checks if check.status == "fail"]
    warnings = [
        check.summary for check in checks if check.status in {"warn", "unknown"}
    ]
    status = (
        "ready"
        if not blocking_reasons and overall.score is not None and overall.score >= 75
        else "needs_review"
    )
    return PreopenQaEvaluatorSummary(
        status=status,
        generated_at=generated_at or datetime.now(UTC),
        overall=overall,
        checks=checks,
        blocking_reasons=blocking_reasons,
        warnings=warnings,
        coverage=coverage,
    )
