"""Pure builders for preopen dashboard briefing artifacts."""

from __future__ import annotations

from typing import Any

from app.models.research_run import ResearchRun
from app.schemas.preopen import (
    CandidateSummary,
    LinkedSessionRef,
    NewsReadinessSummary,
    PreopenArtifactReadinessItem,
    PreopenArtifactSection,
    PreopenBriefingArtifact,
    PreopenDecisionSessionCta,
    PreopenMarketNewsBriefing,
    ReconciliationSummary,
)
from app.schemas.preopen_news_brief import KRPreopenNewsBrief


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
    stage: str,
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
                "source_coverage": [
                    coverage.model_dump(mode="json")
                    for coverage in (news.source_coverage if news else [])
                ],
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
        stage=stage,  # type: ignore[arg-type]
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
