"""Deterministic News dimension evidence bundle (ROB-310).

Assembles a market-wide News evidence bundle, mirroring ``market_evidence``. No
prose, no LLM — raw material for the Hermes News dimension report.

Source preference (ROB-374 B3): when the snapshot bundle carries a news-article
snapshot — the *same* articles ``NewsStage`` summarizes for ``stage_inputs`` —
the evidence is built from it so ``dimension_evidence.news`` and
``stage_inputs.news`` can no longer disagree (the live bug: stage "20 articles"
vs dimension count 0). Only when no article snapshot is present does this fall
back to the ``research_reports`` query (older citation-path bundles / no news
kind). ``research_reports`` is empty until ingestion is enabled (operator gate),
so the fallback degrades gracefully to zero citations.
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
    snapshot_payload: dict[str, Any] | None = None,
    lookback_hours: int = 24,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or dt.datetime.now(tz=dt.UTC)

    # ROB-374 B3 — prefer the bundle's own news-article snapshot. A present-but-
    # empty ``articles`` list is authoritative ("queried, nothing in window") and
    # must NOT silently fall back to the research_reports query — otherwise the
    # dimension would again diverge from the stage.
    if snapshot_payload is not None and snapshot_payload.get("articles") is not None:
        return _evidence_from_articles(
            market,
            snapshot_payload.get("articles") or [],
            now_dt=now_dt,
            lookback_hours=lookback_hours,
        )

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

    return _build_result(
        market,
        citations,
        latest_published,
        now_dt=now_dt,
        lookback_hours=lookback_hours,
        source="research_reports",
    )


def _evidence_from_articles(
    market: str,
    articles: list[Any],
    *,
    now_dt: dt.datetime,
    lookback_hours: int,
) -> dict[str, Any]:
    """Build the evidence bundle from the news snapshot's ``articles`` payload.

    Mirrors the ``research_reports`` citation schema so Hermes sees one shape
    regardless of source; ``analyst`` is ``None`` (articles have no analyst).
    """
    citations: list[dict[str, Any]] = []
    latest_published: dt.datetime | None = None
    for article in articles:
        if not isinstance(article, dict):
            continue
        published_at = _parse_published_at(article.get("published_at"))
        source = article.get("source") or article.get("feed_source")
        symbol_candidates: list[dict[str, Any]] = []
        symbol = article.get("stock_symbol")
        if symbol:
            candidate: dict[str, Any] = {
                "symbol": str(symbol),
                "market": market,
                "source": source,
            }
            name = article.get("stock_name")
            if name:
                candidate["name"] = name
            symbol_candidates.append(candidate)
        citations.append(
            {
                "title": article.get("title"),
                "source": source,
                "analyst": None,
                "published_at": (
                    published_at.isoformat() if published_at is not None else None
                ),
                "excerpt": article.get("summary"),
                "symbol_candidates": symbol_candidates,
            }
        )
        if published_at is not None and (
            latest_published is None or published_at > latest_published
        ):
            latest_published = published_at

    return _build_result(
        market,
        citations,
        latest_published,
        now_dt=now_dt,
        lookback_hours=lookback_hours,
        source="news_articles",
    )


def _parse_published_at(value: Any) -> dt.datetime | None:
    """Coerce an ISO string / datetime to a tz-aware UTC datetime, or ``None``."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
    return None


def _build_result(
    market: str,
    citations: list[dict[str, Any]],
    latest_published: dt.datetime | None,
    *,
    now_dt: dt.datetime,
    lookback_hours: int,
    source: str,
) -> dict[str, Any]:
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
        "data_health": {"available_count": len(citations), "source": source},
    }
