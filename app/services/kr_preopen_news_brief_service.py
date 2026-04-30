"""KR preopen Hermes news brief assembly service (ROB-62).

Advisory-only: no broker/order/watch/intent imports. No LLM calls.
Deterministic aggregation from existing news readiness + research_run evidence.

Persistence is opt-in via record_kr_preopen_news_brief in research_run_service.
This service only builds the brief in-memory — the GET dashboard path never writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.preopen_news_brief import (
    BriefConfidence,
    CandidateImpactFlag,
    KRPreopenNewsBrief,
    RiskFlag,
    SectorImpactFlag,
)

# Confidence cap table — centralized constant so service and tests agree.
READINESS_CONFIDENCE_CAP: dict[str, int] = {
    "ok": 90,
    "stale": 60,
    "degraded": 40,
    "unavailable": 0,
}

_READINESS_RISK_FLAG: dict[str, tuple[str, str] | None] = {
    "ok": None,
    "stale": ("news_stale", "warn"),
    "degraded": ("ingestion_partial", "warn"),
    "unavailable": ("news_unavailable", "warn"),
}


def _map_readiness_status(readiness_obj: Any) -> str:
    """Map a NewsReadinessResponse/NewsReadinessSummary to KRPreopenNewsBrief readiness."""
    warnings = list(getattr(readiness_obj, "warnings", []) or [])
    latest_run_uuid = getattr(readiness_obj, "latest_run_uuid", None)
    if "news_unavailable" in warnings or latest_run_uuid is None:
        return "unavailable"
    is_stale = getattr(readiness_obj, "is_stale", False)
    is_ready = getattr(readiness_obj, "is_ready", False)
    if "news_stale" in warnings or is_stale:
        return "stale"
    if "ingestion_partial" in warnings or "news_sources_empty" in warnings:
        return "degraded"
    if is_ready:
        return "ok"
    return "stale"


def _has_tradingagents_evidence(research_run: Any | None) -> bool:
    """True if the research_run carries TradingAgents-backed advisory_links."""
    if research_run is None:
        return False
    links = list(getattr(research_run, "advisory_links", []) or [])
    return any(
        isinstance(link, dict)
        and "tradingagents" in str(link.get("provider", "")).lower()
        for link in links
    )


def _extract_candidate_flags(
    research_run: Any,
    overall_confidence: int,
) -> list[CandidateImpactFlag]:
    """Extract advisory-only CandidateImpactFlag list from research_run candidates.

    Maps only 'proposed'/'other' kind candidates. CandidateImpactFlag has no
    execution fields by schema design; no quantity/price/side/order fields appear here.
    """
    candidates = list(getattr(research_run, "candidates", []) or [])
    flags: list[CandidateImpactFlag] = []

    for c in candidates:
        kind = getattr(c, "candidate_kind", "")
        if kind not in ("proposed", "other"):
            continue

        symbol = getattr(c, "symbol", "")
        payload = dict(getattr(c, "payload", {}) or {})

        side = getattr(c, "side", "none")
        if side == "buy":
            direction = "positive"
        elif side == "sell":
            direction = "negative"
        else:
            direction = "unclear"

        raw_confidence = getattr(c, "confidence", None)
        candidate_confidence = min(
            raw_confidence if raw_confidence is not None else overall_confidence,
            overall_confidence,
        )

        rationale = getattr(c, "rationale", None) or ""
        reasons: list[str] = [rationale] if rationale else []
        payload_reasons = payload.get("reasons", [])
        if isinstance(payload_reasons, list):
            for r in payload_reasons:
                if r and r not in reasons:
                    reasons.append(str(r))
        reasons = reasons[:3]

        flags.append(
            CandidateImpactFlag(
                symbol=symbol,
                name=payload.get("name", symbol),
                direction=direction,  # type: ignore[arg-type]
                confidence=candidate_confidence,
                sector=payload.get("sector"),
                reasons=reasons,
                research_run_candidate_id=getattr(c, "id", None),
            )
        )

    return flags


def _sector_flags_from_candidates(
    candidate_flags: list[CandidateImpactFlag],
    overall_confidence: int,
) -> list[SectorImpactFlag]:
    """Aggregate candidate flags into sector-level advisory impact flags."""
    grouped: dict[str, list[CandidateImpactFlag]] = {}
    for flag in candidate_flags:
        if flag.sector:
            grouped.setdefault(flag.sector, []).append(flag)

    sector_flags: list[SectorImpactFlag] = []
    for sector, flags in grouped.items():
        directions = {f.direction for f in flags if f.direction != "unclear"}
        if len(directions) == 1:
            direction = next(iter(directions))
        elif len(directions) > 1:
            direction = "mixed"
        else:
            direction = "unclear"
        confidence = min(
            max((f.confidence for f in flags), default=0), overall_confidence
        )
        reasons = []
        for flag in flags:
            reasons.extend(flag.reasons)
        sector_flags.append(
            SectorImpactFlag(
                sector=sector,
                direction=direction,  # type: ignore[arg-type]
                confidence=confidence,
                reasons=list(dict.fromkeys(reasons))[:3],
            )
        )
    return sorted(sector_flags, key=lambda f: (-f.confidence, f.sector))[:5]


def build_brief(
    *,
    readiness: Any,
    research_run: Any | None,
    base_confidence: int = 70,
) -> KRPreopenNewsBrief:
    """Assemble a KRPreopenNewsBrief from news readiness + optional research run.

    MVP: deterministic aggregation only — no LLM calls, no outbound I/O.
    Absence of TradingAgents evidence is treated as an info flag, not a failure.
    """
    now = datetime.now(UTC)
    news_readiness_str = _map_readiness_status(readiness)
    cap = READINESS_CONFIDENCE_CAP[news_readiness_str]
    max_age = getattr(readiness, "max_age_minutes", None)

    risk_flags: list[RiskFlag] = []

    flag_spec = _READINESS_RISK_FLAG[news_readiness_str]
    if flag_spec is not None:
        code, severity = flag_spec
        risk_flags.append(
            RiskFlag(
                code=code,  # type: ignore[arg-type]
                severity=severity,  # type: ignore[arg-type]
                message=f"뉴스 신선도: {news_readiness_str}",
            )
        )

    if news_readiness_str == "unavailable":
        return KRPreopenNewsBrief(
            generated_at=now,
            news_readiness="unavailable",
            news_max_age_minutes=max_age,
            confidence=BriefConfidence(overall=0, cap_reason="news_unavailable"),
            sector_flags=[],
            candidate_flags=[],
            risk_flags=risk_flags,
            research_run_id=None,
            advisory_only=True,
        )

    has_tradingagents = _has_tradingagents_evidence(research_run)
    if not has_tradingagents:
        risk_flags.append(
            RiskFlag(
                code="tradingagents_unavailable",
                severity="info",
                message="TradingAgents 증거 없음 — 뉴스 단독 집계",
            )
        )

    overall = min(base_confidence + (10 if has_tradingagents else 0), cap)

    if news_readiness_str == "stale":
        cap_reason = "news_stale"
    elif not has_tradingagents:
        cap_reason = "no_tradingagents_evidence"
    else:
        cap_reason = "ok"

    sector_flags: list[SectorImpactFlag] = []
    candidate_flags: list[CandidateImpactFlag] = []
    research_run_id: int | None = None

    if research_run is not None:
        research_run_id = getattr(research_run, "id", None)
        candidate_flags = _extract_candidate_flags(research_run, overall)[:10]
        sector_flags = _sector_flags_from_candidates(candidate_flags, overall)

    return KRPreopenNewsBrief(
        generated_at=now,
        news_readiness=news_readiness_str,  # type: ignore[arg-type]
        news_max_age_minutes=max_age,
        confidence=BriefConfidence(overall=overall, cap_reason=cap_reason),  # type: ignore[arg-type]
        sector_flags=sector_flags,
        candidate_flags=candidate_flags,
        risk_flags=risk_flags,
        research_run_id=research_run_id,
        advisory_only=True,
    )
