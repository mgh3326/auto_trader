"""Read-only query service returning citation-shaped results (ROB-140).

Never returns body/full-text fields.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_reports import ResearchReport
from app.schemas.research_reports import (
    ResearchReportCitation,
    ResearchReportCitationListResponse,
    ResearchReportSymbolCandidate,
)


def _row_to_citation(row: ResearchReport) -> ResearchReportCitation:
    candidates: list[ResearchReportSymbolCandidate] = []
    if row.symbol_candidates:
        for sc in row.symbol_candidates:
            try:
                candidates.append(ResearchReportSymbolCandidate.model_validate(sc))
            except Exception:
                continue
    excerpt = row.detail_excerpt or row.summary_text
    return ResearchReportCitation(
        source=row.source,
        title=row.title or row.detail_title,
        analyst=row.analyst,
        published_at_text=row.published_at_text,
        published_at=row.published_at,
        category=row.category,
        detail_url=row.detail_url,
        pdf_url=row.pdf_url,
        excerpt=excerpt,
        symbol_candidates=candidates,
        attribution_publisher=row.attribution_publisher,
        attribution_copyright_notice=row.attribution_copyright_notice,
    )


class ResearchReportsQueryService:
    DEFAULT_LIMIT = 20
    MAX_LIMIT = 100

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_relevant(
        self,
        *,
        symbol: str | None = None,
        query: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> ResearchReportCitationListResponse:
        effective_limit = min(
            self.MAX_LIMIT,
            max(1, limit or self.DEFAULT_LIMIT),
        )

        stmt = select(ResearchReport).order_by(
            ResearchReport.published_at.desc().nulls_last(),
            ResearchReport.id.desc(),
        )

        if source is not None:
            stmt = stmt.where(ResearchReport.source == source)
        if since is not None:
            stmt = stmt.where(ResearchReport.published_at >= since)
        if until is not None:
            stmt = stmt.where(ResearchReport.published_at <= until)

        if symbol is not None:
            stmt = stmt.where(
                ResearchReport.symbol_candidates.cast(JSONB).op("@>")(
                    [{"symbol": symbol}]
                )
            )

        if query is not None:
            like_q = f"%{query}%"
            stmt = stmt.where(
                ResearchReport.title.ilike(like_q)
                | ResearchReport.summary_text.ilike(like_q)
                | ResearchReport.detail_excerpt.ilike(like_q)
            )

        stmt = stmt.limit(effective_limit)
        rows = (await self.db.execute(stmt)).scalars().all()
        citations = [_row_to_citation(r) for r in rows]
        return ResearchReportCitationListResponse(
            count=len(citations), citations=citations
        )
