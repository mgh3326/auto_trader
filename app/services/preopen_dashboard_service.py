"""Preopen dashboard aggregation service (ROB-39).

Read-only. Never imports broker, order, watch, intent, or credential modules.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle
from app.models.research_run import ResearchRun, ResearchRunCandidate
from app.models.trading_decision import TradingDecisionProposal, TradingDecisionSession
from app.schemas.preopen import (
    CandidateSummary,
    LinkedSessionRef,
    NewsArticlePreview,
    NewsReadinessSummary,
    PreopenArtifactReadinessItem,
    PreopenArtifactSection,
    PreopenBriefingArtifact,
    PreopenBriefingRelevance,
    PreopenDecisionSessionCta,
    PreopenLatestResponse,
    PreopenMarketNewsBriefing,
    PreopenMarketNewsItem,
    PreopenMarketNewsSection,
    PreopenQaCheck,
    PreopenQaEvaluatorSummary,
    PreopenQaScore,
    ReconciliationSummary,
)
from app.schemas.preopen_news_brief import KRPreopenNewsBrief
from app.services import kr_preopen_news_brief_service, research_run_service
from app.services.llm_news_service import (
    get_latest_news_preview,
    get_news_readiness,
)
from app.services.market_news_briefing_formatter import (
    BriefingItem,
    format_market_news_briefing,
)

logger = logging.getLogger(__name__)

_FAIL_OPEN = PreopenLatestResponse(
    has_run=False,
    advisory_used=False,
    advisory_skipped_reason="no_open_preopen_run",
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
    news=None,
    news_preview=[],
    news_brief=None,
    market_news_briefing=None,
    briefing_artifact=PreopenBriefingArtifact(
        status="unavailable",
        source_run_status=None,
        readiness=[
            PreopenArtifactReadinessItem(
                key="research_run",
                status="unavailable",
                is_ready=False,
                warnings=["no_open_preopen_run"],
                details={},
            )
        ],
        market_summary=None,
        news_summary=None,
        sections=[],
        risk_notes=["no_open_preopen_run"],
        cta=PreopenDecisionSessionCta(
            state="unavailable",
            label="Create decision session unavailable",
            disabled_reason="no_open_preopen_run",
            requires_confirmation=True,
        ),
        qa={
            "read_only": True,
            "mutation_paths": [],
            "decision_session_created": False,
        },
    ),
)


async def _linked_sessions(
    db: AsyncSession,
    *,
    run: ResearchRun,
    user_id: int,
) -> list[LinkedSessionRef]:
    """Best-effort: find TradingDecisionSessions created from this run."""
    run_uuid_str = str(run.run_uuid)
    try:
        stmt = (
            select(TradingDecisionSession)
            .join(
                TradingDecisionProposal,
                TradingDecisionProposal.session_id == TradingDecisionSession.id,
            )
            .where(
                TradingDecisionSession.user_id == user_id,
                TradingDecisionProposal.original_payload["research_run_id"].astext
                == run_uuid_str,
            )
            .distinct()
            .order_by(TradingDecisionSession.created_at.desc())
            .limit(5)
        )
        result = await db.execute(stmt)
        sessions = result.scalars().all()
        return [
            LinkedSessionRef(
                session_uuid=s.session_uuid,
                status=s.status,
                created_at=s.created_at,
            )
            for s in sessions
        ]
    except Exception:
        # Fail-open: linked session lookup must not block the page
        logger.warning(
            "Failed to look up linked preopen decision sessions",
            exc_info=True,
            extra={"run_uuid": run_uuid_str, "user_id": user_id},
        )
        return []


def _map_candidates(run: ResearchRun) -> list[CandidateSummary]:
    def sort_key(c: ResearchRunCandidate) -> tuple:
        side_order = {"buy": 0, "sell": 1, "none": 2}
        return (side_order.get(c.side, 9), -(c.confidence or -1), c.symbol)

    return [
        CandidateSummary(
            candidate_uuid=c.candidate_uuid,
            symbol=c.symbol,
            instrument_type=c.instrument_type.value
            if hasattr(c.instrument_type, "value")
            else str(c.instrument_type),
            side=c.side,  # type: ignore[arg-type]
            candidate_kind=c.candidate_kind,
            proposed_price=c.proposed_price,
            proposed_qty=c.proposed_qty,
            confidence=c.confidence,
            rationale=c.rationale,
            currency=c.currency,
            warnings=list(c.warnings),
        )
        for c in sorted(run.candidates, key=sort_key)
    ]


def _map_reconciliations(run: ResearchRun) -> list[ReconciliationSummary]:
    return [
        ReconciliationSummary(
            order_id=r.order_id,
            symbol=r.symbol,
            market=r.market,
            side=r.side,  # type: ignore[arg-type]
            classification=r.classification,
            nxt_classification=r.nxt_classification,
            nxt_actionable=r.nxt_actionable,
            gap_pct=r.gap_pct,
            summary=r.summary,
            reasons=list(r.reasons),
            warnings=list(r.warnings),
        )
        for r in sorted(run.reconciliations, key=lambda r: (r.classification, r.symbol))
    ]


def _advisory_skipped_reason(run: ResearchRun) -> str | None:
    if not run.candidates:
        return "no_candidates"
    advisory_failure_markers = {
        "advisory_failed",
        "advisory_error",
        "advisory_timeout",
        "tradingagents_not_configured",
    }
    for w in run.source_warnings:
        if w in advisory_failure_markers:
            return w
    return None


def _derive_news_status(readiness) -> str:
    warnings = list(readiness.warnings or [])
    if "news_unavailable" in warnings or readiness.latest_run_uuid is None:
        return "unavailable"
    if readiness.is_stale or "news_stale" in warnings:
        return "stale"
    if readiness.is_ready:
        return "ready"
    return "stale"


async def _build_news_section(
    db: AsyncSession,
    *,
    market_scope: str,
    source_freshness: dict | None,
    source_warnings: list[str],
) -> tuple[
    NewsReadinessSummary | None,
    list[NewsArticlePreview],
    dict | None,
    list[str],
    object | None,  # raw readiness object for brief assembly
]:
    """Fetch readiness + latest preview, return both typed and merged-dict views."""
    try:
        readiness = await get_news_readiness(market=market_scope, db=db)
    except Exception:
        logger.warning(
            "Failed to look up news readiness for preopen dashboard",
            exc_info=True,
            extra={"market_scope": market_scope},
        )
        merged_warnings = list(source_warnings)
        if "news_readiness_unavailable" not in merged_warnings:
            merged_warnings.append("news_readiness_unavailable")
        return None, [], source_freshness, merged_warnings, None

    merged_freshness = dict(source_freshness or {})
    merged_freshness["news"] = {
        "is_ready": readiness.is_ready,
        "is_stale": readiness.is_stale,
        "latest_run_uuid": readiness.latest_run_uuid,
        "latest_status": readiness.latest_status,
        "latest_finished_at": readiness.latest_finished_at.isoformat()
        if readiness.latest_finished_at
        else None,
        "latest_article_published_at": readiness.latest_article_published_at.isoformat()
        if readiness.latest_article_published_at
        else None,
        "source_counts": readiness.source_counts,
        "warnings": readiness.warnings,
        "max_age_minutes": readiness.max_age_minutes,
    }
    merged_warnings = list(source_warnings)
    for warning in readiness.warnings:
        if warning not in merged_warnings:
            merged_warnings.append(warning)

    summary = NewsReadinessSummary(
        status=_derive_news_status(readiness),
        is_ready=readiness.is_ready,
        is_stale=readiness.is_stale,
        latest_run_uuid=str(readiness.latest_run_uuid)
        if readiness.latest_run_uuid
        else None,
        latest_status=readiness.latest_status,
        latest_finished_at=readiness.latest_finished_at,
        latest_article_published_at=readiness.latest_article_published_at,
        source_counts=dict(readiness.source_counts or {}),
        warnings=list(readiness.warnings or []),
        max_age_minutes=readiness.max_age_minutes,
    )

    feed_sources = list((readiness.source_counts or {}).keys())
    try:
        preview = await get_latest_news_preview(
            db=db, feed_sources=feed_sources, limit=5
        )
    except Exception:
        logger.warning(
            "Failed to load news preview for preopen dashboard",
            exc_info=True,
            extra={"market_scope": market_scope},
        )
        preview = []

    return summary, preview, merged_freshness, merged_warnings, readiness


def _article_field(article: Any, name: str) -> Any:
    if isinstance(article, dict):
        return article.get(name)
    return getattr(article, name, None)


def _map_market_news_item(item: BriefingItem) -> PreopenMarketNewsItem:
    article = item.article
    relevance = item.relevance
    published_at = _article_field(article, "article_published_at") or _article_field(
        article, "published_at"
    )
    return PreopenMarketNewsItem(
        id=int(_article_field(article, "id")),
        title=str(_article_field(article, "title") or ""),
        url=str(_article_field(article, "url") or ""),
        source=_article_field(article, "source"),
        feed_source=_article_field(article, "feed_source"),
        published_at=published_at,
        summary=_article_field(article, "summary"),
        briefing_relevance=PreopenBriefingRelevance(
            score=relevance.score,
            reason=relevance.reason or "matched_section_terms",
            section_id=relevance.section_id,
            matched_terms=list(relevance.matched_terms),
        ),
        crypto_relevance=_article_field(article, "crypto_relevance"),
    )


async def _build_market_news_briefing(
    db: AsyncSession,
    *,
    market_scope: str,
) -> PreopenMarketNewsBriefing | None:
    """Build a read-only market news briefing DTO for the preopen dashboard."""
    try:
        stmt = (
            select(NewsArticle)
            .where(NewsArticle.market == market_scope)
            .order_by(
                NewsArticle.article_published_at.desc().nullslast(),
                NewsArticle.scraped_at.desc(),
            )
            .limit(30)
        )
        result = await db.execute(stmt)
        scalars = result.scalars()
        if isawaitable(scalars):
            scalars = await scalars
        articles = scalars.all()
        if isawaitable(articles):
            articles = await articles
        articles = list(articles)
        briefing = format_market_news_briefing(
            articles,
            market=market_scope,
            limit=10,
        )
        return PreopenMarketNewsBriefing(
            summary=dict(briefing.summary),
            sections=[
                PreopenMarketNewsSection(
                    section_id=section.section_id,
                    title=section.title,
                    items=[_map_market_news_item(item) for item in section.items],
                )
                for section in briefing.sections
            ],
            excluded_count=len(briefing.excluded),
            top_excluded=[
                _map_market_news_item(item) for item in briefing.excluded[:3]
            ],
        )
    except Exception:
        logger.warning(
            "Failed to build market news briefing for preopen dashboard",
            exc_info=True,
            extra={"market_scope": market_scope},
        )
        return None


def _build_news_brief(
    readiness_raw: object | None,
    run: ResearchRun | None,
) -> KRPreopenNewsBrief | None:
    """Assemble the news brief from already-fetched readiness + run. Never raises."""
    if readiness_raw is None:
        return None
    try:
        return kr_preopen_news_brief_service.build_brief(
            readiness=readiness_raw,
            research_run=run,
        )
    except Exception:
        logger.warning("Failed to build KR preopen news brief", exc_info=True)
        return None


def _summarize_market_brief(market_brief: dict[str, Any] | None) -> str | None:
    if not market_brief:
        return None
    summary = market_brief.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary
    return None


def _build_briefing_artifact(
    *,
    run: ResearchRun,
    candidates: list[CandidateSummary],
    reconciliations: list[ReconciliationSummary],
    linked: list[LinkedSessionRef],
    news: NewsReadinessSummary | None,
    news_brief: KRPreopenNewsBrief | None,
    market_news_briefing: PreopenMarketNewsBriefing | None,
    source_warnings: list[str],
) -> PreopenBriefingArtifact:
    """Build the additive read-only preopen artifact from already-loaded data.

    The artifact is a transport-level workflow summary only. It deliberately does
    not query broker/order/watch providers or persist anything.
    """

    buy_candidates = [c for c in candidates if c.side == "buy"]
    holding_actions = [c for c in candidates if c.side in {"sell", "none"}]
    risk_notes = list(dict.fromkeys([*source_warnings]))
    if news is None:
        risk_notes.append("news_readiness_unavailable")
    elif not news.is_ready:
        risk_notes.extend(news.warnings or [f"news_{news.status}"])
    if market_news_briefing is None:
        risk_notes.append("market_news_briefing_unavailable")

    news_status = news.status if news is not None else "unavailable"
    readiness = [
        PreopenArtifactReadinessItem(
            key="research_run",
            status="ready",
            is_ready=True,
            warnings=[],
            details={"source_run_status": run.status},
        ),
        PreopenArtifactReadinessItem(
            key="news",
            status=news_status,  # type: ignore[arg-type]
            is_ready=bool(news and news.is_ready),
            warnings=list(news.warnings if news else ["news_readiness_unavailable"]),
            details={
                "latest_run_uuid": news.latest_run_uuid if news else None,
                "latest_status": news.latest_status if news else None,
                "source_counts": news.source_counts if news else {},
            },
        ),
        PreopenArtifactReadinessItem(
            key="cash",
            status="unavailable",
            is_ready=False,
            warnings=["not_in_current_preopen_contract"],
            details={},
        ),
        PreopenArtifactReadinessItem(
            key="holdings",
            status="partial" if holding_actions or reconciliations else "unavailable",
            is_ready=bool(holding_actions or reconciliations),
            warnings=[]
            if holding_actions or reconciliations
            else ["not_in_current_preopen_contract"],
            details={
                "holding_action_count": len(holding_actions),
                "reconciliation_count": len(reconciliations),
            },
        ),
        PreopenArtifactReadinessItem(
            key="quotes",
            status="unavailable",
            is_ready=False,
            warnings=["not_in_current_preopen_contract"],
            details={},
        ),
    ]

    market_news_count = (
        sum(len(section.items) for section in market_news_briefing.sections)
        if market_news_briefing
        else 0
    )
    sections = [
        PreopenArtifactSection(
            section_id="market_news",
            title="Market news briefing",
            item_count=market_news_count,
            status="ready" if market_news_count else "unavailable",
            summary=(
                f"{market_news_count} high-signal articles across "
                f"{len(market_news_briefing.sections)} sections"
                if market_news_briefing
                else "Market news briefing is unavailable."
            ),
            items=[
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "item_count": len(section.items),
                }
                for section in (
                    market_news_briefing.sections if market_news_briefing else []
                )
            ],
        ),
        PreopenArtifactSection(
            section_id="new_buy_candidates",
            title="New buy candidates",
            item_count=len(buy_candidates),
            status="ready" if buy_candidates else "unavailable",
            summary=(
                f"{len(buy_candidates)} buy candidates prepared before decision-session review."
                if buy_candidates
                else "No buy candidates in the current run."
            ),
            items=[
                {
                    "symbol": c.symbol,
                    "confidence": c.confidence,
                    "rationale": c.rationale,
                    "proposed_price": str(c.proposed_price)
                    if c.proposed_price is not None
                    else None,
                    "proposed_qty": str(c.proposed_qty)
                    if c.proposed_qty is not None
                    else None,
                }
                for c in buy_candidates[:5]
            ],
        ),
        PreopenArtifactSection(
            section_id="holdings_actions",
            title="Holdings actions",
            item_count=len(holding_actions) + len(reconciliations),
            status="ready" if holding_actions or reconciliations else "unavailable",
            summary=(
                f"{len(holding_actions)} candidate actions and {len(reconciliations)} pending reconciliations."
            ),
            items=[
                {"symbol": c.symbol, "side": c.side, "rationale": c.rationale}
                for c in holding_actions[:5]
            ]
            + [
                {
                    "symbol": r.symbol,
                    "classification": r.classification,
                    "summary": r.summary,
                }
                for r in reconciliations[:5]
            ],
        ),
    ]

    cta = (
        PreopenDecisionSessionCta(
            state="linked_session_exists",
            label="Open linked decision session",
            run_uuid=run.run_uuid,
            linked_session_uuid=linked[0].session_uuid,
            requires_confirmation=False,
        )
        if linked
        else PreopenDecisionSessionCta(
            state="create_available",
            label="Create decision session",
            run_uuid=run.run_uuid,
            requires_confirmation=True,
        )
    )

    status = "ready"
    if risk_notes or any(
        not item.is_ready for item in readiness if item.key in {"research_run", "news"}
    ):
        status = "degraded"

    news_summary = None
    if news_brief is not None:
        signal_count = len(news_brief.sector_flags) + len(news_brief.candidate_flags)
        news_summary = (
            f"News readiness is {news_brief.news_readiness}; "
            f"{signal_count} advisory signals summarized."
        )
    elif news is not None:
        news_summary = f"News readiness is {news.status}."

    return PreopenBriefingArtifact(
        status=status,  # type: ignore[arg-type]
        run_uuid=run.run_uuid,
        market_scope=run.market_scope,  # type: ignore[arg-type]
        stage="preopen",
        generated_at=run.generated_at,
        source_run_status=run.status,
        readiness=readiness,
        market_summary=_summarize_market_brief(run.market_brief),
        news_summary=news_summary,
        sections=sections,
        risk_notes=risk_notes,
        cta=cta,
        qa={
            "read_only": True,
            "mutation_paths": [],
            "decision_session_created": False,
            "source": "latest_open_research_run",
        },
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
                {"reconciliation_count": reconciliation_count, "items": len(reconciliations)},
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
        market_news_count = sum(len(section.items) for section in market_news_briefing.sections)
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
    warnings = [check.summary for check in checks if check.status in {"warn", "unknown"}]
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


async def get_latest_preopen_dashboard(
    db: AsyncSession,
    *,
    user_id: int,
    market_scope: str,
) -> PreopenLatestResponse:
    run = await research_run_service.get_latest_research_run(
        db,
        user_id=user_id,
        market_scope=market_scope,
        stage="preopen",
        status="open",
    )

    if run is None:
        return _FAIL_OPEN.model_copy(
            update={
                "qa_evaluator": _build_qa_evaluator_summary(
                    has_run=False,
                    generated_at=None,
                    candidate_count=0,
                    reconciliation_count=0,
                    candidates=[],
                    reconciliations=[],
                    linked=[],
                    news=None,
                    market_news_briefing=None,
                    briefing_artifact=_FAIL_OPEN.briefing_artifact,
                    advisory_skipped_reason="no_open_preopen_run",
                )
            }
        )

    candidates = _map_candidates(run)
    reconciliations = _map_reconciliations(run)
    (
        news_summary,
        news_preview,
        source_freshness,
        source_warnings,
        readiness_raw,
    ) = await _build_news_section(
        db,
        market_scope=market_scope,
        source_freshness=run.source_freshness,
        source_warnings=list(run.source_warnings),
    )
    news_brief = _build_news_brief(readiness_raw, run)
    market_news_briefing = await _build_market_news_briefing(
        db,
        market_scope=market_scope,
    )
    advisory_reason = _advisory_skipped_reason(run)
    linked = await _linked_sessions(db, run=run, user_id=user_id)

    briefing_artifact = _build_briefing_artifact(
        run=run,
        candidates=candidates,
        reconciliations=reconciliations,
        linked=linked,
        news=news_summary,
        news_brief=news_brief,
        market_news_briefing=market_news_briefing,
        source_warnings=source_warnings,
    )

    qa_evaluator = _build_qa_evaluator_summary(
        has_run=True,
        generated_at=run.generated_at,
        candidate_count=len(candidates),
        reconciliation_count=len(reconciliations),
        candidates=candidates,
        reconciliations=reconciliations,
        linked=linked,
        news=news_summary,
        market_news_briefing=market_news_briefing,
        briefing_artifact=briefing_artifact,
        advisory_skipped_reason=advisory_reason,
    )

    return PreopenLatestResponse(
        has_run=True,
        advisory_used=bool(run.advisory_links) and advisory_reason is None,
        advisory_skipped_reason=advisory_reason,
        run_uuid=run.run_uuid,
        market_scope=run.market_scope,  # type: ignore[arg-type]
        stage="preopen",
        status=run.status,
        strategy_name=run.strategy_name,
        source_profile=run.source_profile,
        generated_at=run.generated_at,
        created_at=run.created_at,
        notes=run.notes,
        market_brief=run.market_brief,
        source_freshness=source_freshness,
        source_warnings=source_warnings,
        advisory_links=list(run.advisory_links),
        candidate_count=len(candidates),
        reconciliation_count=len(reconciliations),
        candidates=candidates,
        reconciliations=reconciliations,
        linked_sessions=linked,
        news=news_summary,
        news_preview=news_preview,
        news_brief=news_brief,
        market_news_briefing=market_news_briefing,
        briefing_artifact=briefing_artifact,
        qa_evaluator=qa_evaluator,
    )
