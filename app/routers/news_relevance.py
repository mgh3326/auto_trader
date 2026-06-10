"""News-relevance judgment surface (ROB-491 PR2).

Token-authed via the AuthMiddleware ``NEWS_RELEVANCE_PATH_PREFIX`` branch
(default-off: token unset → 403). Pending read + idempotent judgment ingest.
Status is derived server-side (``symbol_news_store.derive_status``); no
broker/order surface, no LLM calls.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas.news_relevance import NewsRelevanceIngestRequest
from app.services import symbol_news_store

router = APIRouter(prefix="/trading/api/news-relevance", tags=["news-relevance"])


@router.get("/pending")
async def get_pending(
    market: str = Query(default="kr"),
    limit: int = Query(default=50, ge=1, le=200),
    symbol: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    pending = await symbol_news_store.list_pending(db, market, limit, symbol=symbol)
    return {"market": market, "count": len(pending), "pending": pending}


@router.post("/ingest/bulk")
async def ingest_bulk(
    request: NewsRelevanceIngestRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, judgment in enumerate(request.judgments):
        status = await symbol_news_store.apply_judgment(
            db,
            article_id=judgment.article_id,
            market=judgment.market,
            symbol=judgment.symbol,
            relationship=judgment.relationship,
            relevance=judgment.relevance,
            price_relevance=judgment.price_relevance,
            score=judgment.score,
            reason=judgment.reason,
            judged_by=judgment.judged_by,
        )
        if status is None:
            errors.append(
                {
                    "index": index,
                    "article_id": judgment.article_id,
                    "error": "link_not_found",
                }
            )
        else:
            applied.append(
                {
                    "article_id": judgment.article_id,
                    "market": judgment.market,
                    "symbol": judgment.symbol,
                    "status": status,
                }
            )
    await db.commit()
    return {"applied": applied, "errors": errors}
