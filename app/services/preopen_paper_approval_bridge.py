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
from app.services.preopen_approval_bridge_common import (
    bridge_result,
    bridge_warnings,
    dedupe,
    qa_blocking_reasons,
    unsupported_candidate,
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
        return unsupported_candidate(candidate, reason=str(exc))

    preview_payload = metadata.preview_payload.model_dump(mode="json")
    forbidden = sorted(_FORBIDDEN_PREVIEW_KEYS.intersection(preview_payload))
    if forbidden:
        return unsupported_candidate(
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
    blocking_reasons = qa_blocking_reasons(qa_evaluator, has_run=has_run)
    warnings = bridge_warnings(qa_evaluator, briefing_artifact, candidates)
    generated_at = generated_at or datetime.now(UTC)

    bridge_candidates: list[PreopenPaperApprovalCandidate] = []
    unsupported_reasons: list[str] = []

    if blocking_reasons:
        return bridge_result(
            status="blocked",
            generated_at=generated_at,
            market_scope=market_scope,
            stage=stage,
            candidate_count=len(candidates),
            candidates=[],
            has_run=has_run,
            blocking_reasons=blocking_reasons,
            warnings=warnings,
        )

    if market_scope != "crypto":
        reason = f"unsupported_market_scope:{market_scope or 'unknown'}"
        return bridge_result(
            status="unavailable",
            generated_at=generated_at,
            market_scope=market_scope,
            stage=stage,
            candidate_count=len(candidates),
            candidates=[
                unsupported_candidate(candidate, reason=reason)
                for candidate in candidates
            ],
            has_run=has_run,
            warnings=warnings,
            unsupported_reasons=[reason],
        )

    bridge_has_warnings = bool(warnings)
    for candidate in candidates:
        if candidate.instrument_type != "crypto":
            reason = f"unsupported_instrument_type:{candidate.instrument_type}"
            bridge_candidates.append(unsupported_candidate(candidate, reason=reason))
            unsupported_reasons.append(reason)
            continue
        if candidate.side != "buy":
            reason = f"unsupported_side:{candidate.side}"
            bridge_candidates.append(unsupported_candidate(candidate, reason=reason))
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

    return bridge_result(
        status=status,
        generated_at=generated_at,
        market_scope="crypto",
        stage=stage,
        eligible_count=eligible_count,
        candidate_count=len(candidates),
        candidates=bridge_candidates,
        has_run=has_run,
        warnings=warnings,
        unsupported_reasons=dedupe(unsupported_reasons),
    )


__all__ = ["build_preopen_paper_approval_bridge"]
