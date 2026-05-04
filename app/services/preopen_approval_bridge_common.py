"""Pure shared helpers for preopen approval preview bridge builders."""

from __future__ import annotations

from datetime import datetime

from app.schemas.preopen import (
    CandidateSummary,
    PreopenBriefingArtifact,
    PreopenPaperApprovalBridge,
    PreopenPaperApprovalCandidate,
    PreopenQaEvaluatorSummary,
)


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def unsupported_candidate(
    candidate: CandidateSummary,
    *,
    reason: str,
) -> PreopenPaperApprovalCandidate:
    return PreopenPaperApprovalCandidate(
        candidate_uuid=candidate.candidate_uuid,
        symbol=candidate.symbol,
        status="unavailable",
        reason=reason,
        warnings=list(candidate.warnings),
    )


def qa_blocking_reasons(
    qa_evaluator: PreopenQaEvaluatorSummary | None,
    *,
    has_run: bool,
) -> list[str]:
    if not has_run:
        return ["no_open_preopen_run"]
    if qa_evaluator is None:
        return ["qa_evaluator_unavailable"]
    if qa_evaluator.status in {"unavailable", "skipped"}:
        return [f"qa_evaluator_{qa_evaluator.status}"]

    reasons = list(qa_evaluator.blocking_reasons)
    for check in qa_evaluator.checks:
        if check.status == "fail" and check.severity == "high":
            reasons.append(f"high_severity_fail:{check.id}")
        if check.id == "actionability_guardrail" and check.status != "pass":
            reasons.append("safety_guardrail_not_passed")

    coverage = qa_evaluator.coverage or {}
    if coverage.get("advisory_only") is not True:
        reasons.append("advisory_only_guard_missing")
    if coverage.get("execution_allowed") is not False:
        reasons.append("execution_allowed_guard_missing")
    return dedupe(reasons)


def bridge_warnings(
    qa_evaluator: PreopenQaEvaluatorSummary | None,
    briefing_artifact: PreopenBriefingArtifact | None,
    candidates: list[CandidateSummary],
) -> list[str]:
    warnings: list[str] = []
    if qa_evaluator is not None:
        if qa_evaluator.status == "needs_review":
            warnings.append("qa_needs_review")
        warnings.extend(qa_evaluator.warnings)
    if briefing_artifact is not None:
        if briefing_artifact.status == "degraded":
            warnings.append("briefing_artifact_degraded")
        warnings.extend(briefing_artifact.risk_notes)
    for candidate in candidates:
        warnings.extend(candidate.warnings)
    return dedupe(warnings)


def bridge_result(
    *,
    status: str,
    generated_at: datetime,
    market_scope: str | None,
    has_run: bool,
    candidate_count: int,
    candidates: list[PreopenPaperApprovalCandidate],
    warnings: list[str],
    blocking_reasons: list[str] | None = None,
    unsupported_reasons: list[str] | None = None,
    eligible_count: int = 0,
    stage: str = "preopen",
) -> PreopenPaperApprovalBridge:
    return PreopenPaperApprovalBridge(
        status=status,
        generated_at=generated_at,
        market_scope=market_scope,  # type: ignore[arg-type]
        stage=stage if has_run else None,  # type: ignore[arg-type]
        eligible_count=eligible_count,
        candidate_count=candidate_count,
        candidates=candidates,
        blocking_reasons=blocking_reasons or [],
        warnings=warnings,
        unsupported_reasons=unsupported_reasons or [],
    )


__all__ = [
    "bridge_result",
    "bridge_warnings",
    "dedupe",
    "qa_blocking_reasons",
    "unsupported_candidate",
]
