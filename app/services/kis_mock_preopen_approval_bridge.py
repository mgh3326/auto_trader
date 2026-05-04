"""Pure KIS mock preopen approval preview bridge (ROB-95).

Maps already-built KR preopen transport objects into operator-facing KIS
official mock dry-run preview metadata without touching broker, account,
network, cache, scheduler, persistence, or approval systems.
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
from app.services.preopen_approval_bridge_common import (
    bridge_result,
    bridge_warnings,
    dedupe,
    qa_blocking_reasons,
    unsupported_candidate,
)
from app.services.preopen_approval_safety import (
    is_kr_equity_symbol,
    is_positive_integer_decimal,
)


def _build_approval_copy(
    symbol: str, side: str, quantity: int, price: str
) -> list[str]:
    return [
        f"KIS official mock only — symbol={symbol} side={side} qty={quantity} price={price}",
        "No KIS live order will be submitted.",
        "Preview: dry_run=True — confirms KIS mock routing before any mock submit.",
        "Final submit: dry_run=False requires second exact approval from operator.",
    ]


def _bridge_market_scope(market_scope: str | None) -> str | None:
    return market_scope if market_scope in {"kr", "us", "crypto"} else None


def _build_kr_candidate(
    candidate: CandidateSummary,
    *,
    bridge_has_warnings: bool,
) -> PreopenPaperApprovalCandidate:
    if candidate.instrument_type != "equity_kr":
        return unsupported_candidate(
            candidate,
            reason=f"unsupported_instrument_type:{candidate.instrument_type}",
        )

    if not is_kr_equity_symbol(candidate.symbol):
        return unsupported_candidate(
            candidate,
            reason=f"unsupported_symbol:{candidate.symbol}",
        )

    if candidate.side not in {"buy", "sell"}:
        return unsupported_candidate(
            candidate, reason=f"unsupported_side:{candidate.side}"
        )

    if candidate.side == "sell" and candidate.proposed_qty is None:
        return unsupported_candidate(candidate, reason="missing_quantity")

    if candidate.proposed_price is None:
        return unsupported_candidate(candidate, reason="missing_price")
    if not is_positive_integer_decimal(candidate.proposed_price):
        return unsupported_candidate(candidate, reason="invalid_price")
    if candidate.proposed_qty is not None and not is_positive_integer_decimal(
        candidate.proposed_qty
    ):
        return unsupported_candidate(candidate, reason="invalid_quantity")

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
    blocking_reasons = qa_blocking_reasons(qa_evaluator, has_run=has_run)
    warnings = bridge_warnings(qa_evaluator, briefing_artifact, candidates)
    generated_at = generated_at or datetime.now(UTC)

    if blocking_reasons:
        return bridge_result(
            status="blocked",
            generated_at=generated_at,
            market_scope=_bridge_market_scope(market_scope),
            candidate_count=len(candidates),
            candidates=[],
            has_run=has_run,
            blocking_reasons=blocking_reasons,
            warnings=warnings,
        )

    if market_scope != "kr":
        reason = f"unsupported_market_scope:{market_scope or 'unknown'}"
        return bridge_result(
            status="unavailable",
            generated_at=generated_at,
            market_scope=_bridge_market_scope(market_scope),
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

    return bridge_result(
        status=status,
        generated_at=generated_at,
        market_scope="kr",
        eligible_count=eligible_count,
        candidate_count=len(candidates),
        candidates=bridge_candidates,
        has_run=has_run,
        warnings=warnings,
        unsupported_reasons=dedupe(unsupported_reasons),
    )


__all__ = ["build_kis_mock_preopen_approval_bridge"]
