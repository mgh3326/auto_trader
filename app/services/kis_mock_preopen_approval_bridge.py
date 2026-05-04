"""Pure KIS mock preopen approval preview bridge (ROB-95).

Maps already-built KR preopen transport objects into operator-facing KIS
official mock dry-run preview metadata without touching broker, account,
network, cache, scheduler, persistence, or approval systems.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.preopen import (
    CandidateSummary,
    PreopenBriefingArtifact,
    PreopenPaperApprovalBridge,
    PreopenPaperApprovalCandidate,
    PreopenQaEvaluatorSummary,
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


def _build_approval_copy(
    symbol: str, side: str, quantity: int, price: str
) -> list[str]:
    return [
        f"KIS official mock only — symbol={symbol} side={side} qty={quantity} price={price}",
        "No KIS live order will be submitted.",
        "Preview: dry_run=True — confirms KIS mock routing before any mock submit.",
        "Final submit: dry_run=False requires second exact approval from operator.",
    ]


def _is_positive_integer_decimal(value: Decimal | None) -> bool:
    if value is None:
        return False
    return value > 0 and value == value.to_integral_value()


def _is_kr_equity_symbol(symbol: str) -> bool:
    return len(symbol) == 6 and symbol.isdigit()


def _bridge_market_scope(market_scope: str | None) -> str | None:
    return market_scope if market_scope in {"kr", "us", "crypto"} else None


def _build_kr_candidate(
    candidate: CandidateSummary,
    *,
    bridge_has_warnings: bool,
) -> PreopenPaperApprovalCandidate:
    if candidate.instrument_type != "equity_kr":
        return _unsupported_candidate(
            candidate,
            reason=f"unsupported_instrument_type:{candidate.instrument_type}",
        )

    if not _is_kr_equity_symbol(candidate.symbol):
        return _unsupported_candidate(
            candidate,
            reason=f"unsupported_symbol:{candidate.symbol}",
        )

    if candidate.side not in {"buy", "sell"}:
        return _unsupported_candidate(
            candidate, reason=f"unsupported_side:{candidate.side}"
        )

    if candidate.side == "sell" and candidate.proposed_qty is None:
        return PreopenPaperApprovalCandidate(
            candidate_uuid=candidate.candidate_uuid,
            symbol=candidate.symbol,
            status="unavailable",
            reason="missing_quantity",
            warnings=list(candidate.warnings),
        )

    if candidate.proposed_price is None:
        return _unsupported_candidate(candidate, reason="missing_price")
    if not _is_positive_integer_decimal(candidate.proposed_price):
        return _unsupported_candidate(candidate, reason="invalid_price")
    if candidate.proposed_qty is not None and not _is_positive_integer_decimal(
        candidate.proposed_qty
    ):
        return _unsupported_candidate(candidate, reason="invalid_quantity")

    quantity = int(candidate.proposed_qty) if candidate.proposed_qty is not None else 1
    price = str(int(candidate.proposed_price))

    preview_payload = {
        "tool": "kis_mock_place_order",
        "symbol": candidate.symbol,
        "side": candidate.side,
        "order_type": "limit",
        "quantity": quantity,
        "price": price,
        "account_mode": "kis_mock",
        "execution_venue": "kis_mock",
        "execution_asset_class": "equity_kr",
        "dry_run": True,
        "regular_session_only": True,
        "requires_final_mock_submit_approval": True,
    }

    candidate_warnings = list(candidate.warnings)
    status = "warning" if bridge_has_warnings or candidate_warnings else "available"

    return PreopenPaperApprovalCandidate(
        candidate_uuid=candidate.candidate_uuid,
        symbol=candidate.symbol,
        status=status,
        reason=None,
        warnings=candidate_warnings,
        signal_symbol=candidate.symbol,
        signal_venue="kr_preopen",
        execution_symbol=candidate.symbol,
        execution_venue="kis_mock",
        execution_asset_class="equity_kr",
        workflow_stage="kr_market_open_mock",
        purpose="kis_mock_market_open_pilot",
        preview_payload=preview_payload,
        approval_copy=_build_approval_copy(
            candidate.symbol, candidate.side, quantity, price
        ),
    )


def build_kis_mock_preopen_approval_bridge(
    *,
    has_run: bool,
    market_scope: str | None,
    candidates: list[CandidateSummary],
    briefing_artifact: PreopenBriefingArtifact | None,
    qa_evaluator: PreopenQaEvaluatorSummary | None,
    generated_at: datetime | None = None,
) -> PreopenPaperApprovalBridge:
    """Build deterministic KIS mock preopen approval preview metadata."""
    blocking_reasons = _qa_blocking_reasons(qa_evaluator, has_run=has_run)
    warnings = _bridge_warnings(qa_evaluator, briefing_artifact, candidates)
    generated_at = generated_at or datetime.now(UTC)

    if blocking_reasons:
        return PreopenPaperApprovalBridge(
            status="blocked",
            generated_at=generated_at,
            market_scope=_bridge_market_scope(market_scope),
            stage="preopen" if has_run else None,
            candidate_count=len(candidates),
            candidates=[],
            blocking_reasons=blocking_reasons,
            warnings=warnings,
            unsupported_reasons=[],
        )

    if market_scope != "kr":
        reason = f"unsupported_market_scope:{market_scope or 'unknown'}"
        return PreopenPaperApprovalBridge(
            status="unavailable",
            generated_at=generated_at,
            market_scope=_bridge_market_scope(market_scope),
            stage="preopen" if has_run else None,
            candidate_count=len(candidates),
            candidates=[
                _unsupported_candidate(candidate, reason=reason)
                for candidate in candidates
            ],
            warnings=warnings,
            unsupported_reasons=[reason],
        )

    bridge_has_warnings = bool(warnings)
    bridge_candidates: list[PreopenPaperApprovalCandidate] = []
    unsupported_reasons: list[str] = []

    for candidate in candidates:
        bridge_candidate = _build_kr_candidate(
            candidate,
            bridge_has_warnings=bridge_has_warnings,
        )
        bridge_candidates.append(bridge_candidate)
        if bridge_candidate.status == "unavailable" and bridge_candidate.reason:
            unsupported_reasons.append(bridge_candidate.reason)

    eligible_count = sum(
        1 for c in bridge_candidates if c.status in {"available", "warning"}
    )

    if eligible_count == 0:
        status = "unavailable"
    elif bridge_has_warnings or any(c.status == "warning" for c in bridge_candidates):
        status = "warning"
    else:
        status = "available"

    return PreopenPaperApprovalBridge(
        status=status,
        generated_at=generated_at,
        market_scope="kr",
        stage="preopen" if has_run else None,
        eligible_count=eligible_count,
        candidate_count=len(candidates),
        candidates=bridge_candidates,
        blocking_reasons=[],
        warnings=warnings,
        unsupported_reasons=_dedupe(unsupported_reasons),
    )


__all__ = ["build_kis_mock_preopen_approval_bridge"]
