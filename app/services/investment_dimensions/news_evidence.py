"""Deterministic News dimension evidence bundle (ROB-310).

Assembles recent research-report citations into a market-wide News evidence
bundle, mirroring ``market_evidence``. No prose, no LLM — raw material for the
Hermes News dimension report. ``research_reports`` is empty until ingestion is
enabled (operator gate); this degrades gracefully to zero citations.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.services.research_reports.query_service import ResearchReportsQueryService

CITATION_LIMIT = 20


async def build_news_evidence(
    query_service: ResearchReportsQueryService,
    *,
    market: str,
    lookback_hours: int = 24,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or dt.datetime.now(tz=dt.UTC)
    # ROB-366 B8 — scope to the bundle market via the per-candidate market tag so
    # KR research does not bleed into a US bundle. No ``since`` so freshness
    # (fresh vs stale) is meaningful rather than always-fresh.
    response = await query_service.find_relevant(limit=CITATION_LIMIT, market=market)

    citations: list[dict[str, Any]] = []
    latest_published: dt.datetime | None = None
    for c in response.citations:
        citations.append(
            {
                "title": c.title,
                "source": c.source,
                "analyst": c.analyst,
                "published_at": c.published_at.isoformat() if c.published_at else None,
                "excerpt": c.excerpt,
                "symbol_candidates": [sc.model_dump() for sc in c.symbol_candidates],
            }
        )
        if c.published_at is not None and (
            latest_published is None or c.published_at > latest_published
        ):
            latest_published = c.published_at

    if not citations:
        status = "unavailable"
    elif latest_published is not None and latest_published >= now_dt - dt.timedelta(
        hours=lookback_hours
    ):
        status = "fresh"
    else:
        status = "stale"

    return {
        "market": market,
        "citations": citations,
        "count": len(citations),
        "freshness": {
            "status": status,
            "latest_published_at": latest_published.isoformat()
            if latest_published
            else None,
        },
        "data_health": {"available_count": len(citations)},
    }
