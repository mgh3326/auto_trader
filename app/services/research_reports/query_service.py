"""Read-only query service returning citation-shaped results (ROB-140).

Never returns body/full-text fields.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from sqlalchemy import func, or_, select
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
        market: str | None = None,
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

        if market is not None:
            # ROB-366 B8 — scope to a market via the per-candidate market tag so
            # KR research does not bleed into a US bundle. Mirrors find_feed_page.
            stmt = stmt.where(
                ResearchReport.symbol_candidates.cast(JSONB).op("@>")(
                    [{"market": market}]
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

    async def find_feed_page(
        self,
        *,
        limit: int,
        cursor: dict | None,
        source: str | None = None,
        symbol: str | None = None,
        analyst: str | None = None,
        category: str | None = None,
        query: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        market_filter: Literal["kr", "us"] | None = None,
        symbol_in: list[str] | None = None,
    ) -> tuple[list[ResearchReport], dict | None]:
        """Paginated feed query for /invest/api/feed/research (ROB-179).

        Returns (rows, next_cursor_dict or None). Does not modify find_relevant.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")

        effective_limit = min(limit, self.MAX_LIMIT)
        fetch_limit = effective_limit + 1

        stmt = select(ResearchReport).order_by(
            ResearchReport.published_at.desc().nulls_last(),
            ResearchReport.id.desc(),
        )

        if source is not None:
            stmt = stmt.where(ResearchReport.source == source)
        if category is not None:
            stmt = stmt.where(ResearchReport.category == category)
        if analyst is not None:
            stmt = stmt.where(ResearchReport.analyst.ilike(f"%{analyst}%"))
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
        if from_date is not None:
            stmt = stmt.where(func.date(ResearchReport.published_at) >= from_date)
        if to_date is not None:
            stmt = stmt.where(func.date(ResearchReport.published_at) <= to_date)
        if market_filter is not None:
            stmt = stmt.where(
                ResearchReport.symbol_candidates.cast(JSONB).op("@>")(
                    [{"market": market_filter}]
                )
            )
        if symbol_in:
            or_clauses = [
                ResearchReport.symbol_candidates.cast(JSONB).op("@>")([{"symbol": s}])
                for s in symbol_in
            ]
            stmt = stmt.where(or_(*or_clauses))

        if cursor is not None:
            p = cursor.get("p")
            i = cursor.get("i")
            if p is not None:
                cursor_dt = datetime.fromisoformat(p)
                # NULL rows sort last (after all dated rows), so include them on
                # any page whose cursor points into the dated section.
                stmt = stmt.where(
                    (ResearchReport.published_at < cursor_dt)
                    | (
                        (ResearchReport.published_at == cursor_dt)
                        & (ResearchReport.id < i)
                    )
                    | ResearchReport.published_at.is_(None)
                )
            else:
                # cursor into the null section: only null rows with lower id
                stmt = stmt.where(
                    ResearchReport.published_at.is_(None) & (ResearchReport.id < i)
                )

        stmt = stmt.limit(fetch_limit)
        rows = list((await self.db.execute(stmt)).scalars().all())

        if len(rows) > effective_limit:
            rows = rows[:effective_limit]
            last = rows[-1]
            next_cursor: dict | None = {
                "p": last.published_at.isoformat() if last.published_at else None,
                "i": last.id,
            }
        else:
            next_cursor = None

        return rows, next_cursor
