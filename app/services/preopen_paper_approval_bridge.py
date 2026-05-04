"""Pure preopen paper approval preview bridge (ROB-81).

This module is intentionally read-layer only. It maps already-built preopen
transport objects into operator-facing preview metadata without touching broker,
account, network, cache, scheduler, persistence, or approval systems.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.preopen import (
    CandidateSummary,
    PreopenBriefingArtifact,
    PreopenPaperApprovalBridge,
    PreopenPaperApprovalCandidate,
    PreopenQaEvaluatorSummary,
)
from app.services.crypto_execution_mapping import (
    CryptoExecutionMappingError,
    build_crypto_paper_approval_metadata,
)

_FORBIDDEN_PREVIEW_KEYS = frozenset(
    {
        "confirm",
        "dry_run",
        "order_id",
        "client_order_id",
        "submitted",
        "submit",
        "action",
    }
)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _unsupported_candidate(
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


def _qa_blocking_reasons(
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
    return _dedupe(reasons)


def _bridge_warnings(
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
    return _dedupe(warnings)


def _build_crypto_candidate(
    candidate: CandidateSummary,
    *,
    bridge_has_warnings: bool,
) -> PreopenPaperApprovalCandidate:
    try:
        metadata = build_crypto_paper_approval_metadata(
            candidate.symbol,
            stage="crypto_weekend",
            purpose="paper_plumbing_smoke",
        )
    except CryptoExecutionMappingError as exc:
        return _unsupported_candidate(candidate, reason=str(exc))

    preview_payload = metadata.preview_payload.model_dump(mode="json")
    forbidden = sorted(_FORBIDDEN_PREVIEW_KEYS.intersection(preview_payload))
    if forbidden:
        return _unsupported_candidate(
            candidate,
            reason="forbidden_preview_payload_keys:" + ",".join(forbidden),
        )

    candidate_warnings = list(candidate.warnings)
    status = "warning" if bridge_has_warnings or candidate_warnings else "available"
    return PreopenPaperApprovalCandidate(
        candidate_uuid=candidate.candidate_uuid,
        symbol=candidate.symbol,
        status=status,
        reason=None,
        warnings=candidate_warnings,
        signal_symbol=metadata.mapping.signal_symbol,
        signal_venue=metadata.mapping.signal_venue,
        execution_symbol=metadata.mapping.execution_symbol,
        execution_venue=metadata.mapping.execution_venue,
        execution_asset_class=metadata.mapping.asset_class,
        workflow_stage=metadata.stage,
        purpose=metadata.purpose,
        preview_payload=preview_payload,
        approval_copy=metadata.approval_copy,
    )


def build_preopen_paper_approval_bridge(
    *,
    has_run: bool,
    market_scope: str | None,
    candidates: list[CandidateSummary],
    briefing_artifact: PreopenBriefingArtifact | None,
    qa_evaluator: PreopenQaEvaluatorSummary | None,
    generated_at: datetime | None = None,
    stage: str | None = None,
) -> PreopenPaperApprovalBridge:
    """Build deterministic paper approval preview metadata for preopen output."""
    stage = stage or "preopen"
    blocking_reasons = _qa_blocking_reasons(qa_evaluator, has_run=has_run)
    warnings = _bridge_warnings(qa_evaluator, briefing_artifact, candidates)
    generated_at = generated_at or datetime.now(UTC)

    bridge_candidates: list[PreopenPaperApprovalCandidate] = []
    unsupported_reasons: list[str] = []

    if blocking_reasons:
        return PreopenPaperApprovalBridge(
            status="blocked",
            generated_at=generated_at,
            market_scope=market_scope,  # type: ignore[arg-type]
            stage=stage if has_run else None,  # type: ignore[arg-type]
            candidate_count=len(candidates),
            candidates=[],
            blocking_reasons=blocking_reasons,
            warnings=warnings,
            unsupported_reasons=[],
        )

    if market_scope != "crypto":
        reason = f"unsupported_market_scope:{market_scope or 'unknown'}"
        return PreopenPaperApprovalBridge(
            status="unavailable",
            generated_at=generated_at,
            market_scope=market_scope,  # type: ignore[arg-type]
            stage=stage if has_run else None,  # type: ignore[arg-type]
            candidate_count=len(candidates),
            candidates=[
                _unsupported_candidate(candidate, reason=reason)
                for candidate in candidates
            ],
            warnings=warnings,
            unsupported_reasons=[reason],
        )

    bridge_has_warnings = bool(warnings)
    for candidate in candidates:
        if candidate.instrument_type != "crypto":
            reason = f"unsupported_instrument_type:{candidate.instrument_type}"
            bridge_candidates.append(_unsupported_candidate(candidate, reason=reason))
            unsupported_reasons.append(reason)
            continue
        if candidate.side != "buy":
            reason = f"unsupported_side:{candidate.side}"
            bridge_candidates.append(_unsupported_candidate(candidate, reason=reason))
            unsupported_reasons.append(reason)
            continue
        bridge_candidate = _build_crypto_candidate(
            candidate,
            bridge_has_warnings=bridge_has_warnings,
        )
        bridge_candidates.append(bridge_candidate)
        if bridge_candidate.status == "unavailable" and bridge_candidate.reason:
            unsupported_reasons.append(bridge_candidate.reason)

    eligible_count = sum(
        1
        for candidate in bridge_candidates
        if candidate.status in {"available", "warning"}
    )
    if eligible_count == 0:
        status = "unavailable"
    elif bridge_has_warnings or any(
        candidate.status == "warning" for candidate in bridge_candidates
    ):
        status = "warning"
    else:
        status = "available"

    return PreopenPaperApprovalBridge(
        status=status,
        generated_at=generated_at,
        market_scope="crypto",
        stage=stage if has_run else None,  # type: ignore[arg-type]
        eligible_count=eligible_count,
        candidate_count=len(candidates),
        candidates=bridge_candidates,
        blocking_reasons=[],
        warnings=warnings,
        unsupported_reasons=_dedupe(unsupported_reasons),
    )


__all__ = ["build_preopen_paper_approval_bridge"]
