from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.news import NewsArticle
from app.schemas.news import (
    NewsAnalysisRequest,
    NewsAnalysisResponse,
    NewsAnalysisResultResponse,
    NewsArticleResponse,
    NewsListResponse,
)
from app.services.llm_news_service import (
    NewsAnalyzer,
    create_news_article,
    get_news_analysis,
    get_news_articles,
)

router = APIRouter(prefix="/api/v1/news", tags=["News Analysis"])


@router.post(
    "/analyze", response_model=NewsAnalysisResponse, status_code=status.HTTP_201_CREATED
)
async def analyze_news_article(
    request: NewsAnalysisRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        article = await create_news_article(
            title=request.title,
            url=request.url,
            content=request.content,
            source=request.source,
            stock_symbol=request.stock_symbol,
            stock_name=request.stock_name,
        )

        analyzer = NewsAnalyzer()
        analysis = await analyzer.analyze_news(
            article_id=article.id,
            title=request.title,
            content=request.content,
            stock_symbol=request.stock_symbol,
            stock_name=request.stock_name,
            source=request.source,
        )
        await analyzer.close()

        return NewsAnalysisResponse(
            article=NewsArticleResponse.model_validate(article),
            analysis=NewsAnalysisResultResponse.model_validate(analysis),
        )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to analyze news: {str(e)}",
        )


@router.get("", response_model=NewsListResponse)
async def list_news_articles(
    stock_symbol: str | None = Query(None, description="종목 코드로 필터링"),
    source: str | None = Query(None, description="뉴스 출처로 필터링"),
    limit: int = Query(10, ge=1, le=100, description="반환할 뉴스 수"),
    offset: int = Query(0, ge=0, description="건너뛸 뉴스 수"),
    db: AsyncSession = Depends(get_db),
):
    try:
        articles, total = await get_news_articles(
            stock_symbol=stock_symbol,
            source=source,
            limit=limit,
            offset=offset,
        )

        page_info = {
            "limit": limit,
            "offset": offset,
            "total": total,
        }

        return NewsListResponse(
            total=total,
            items=[NewsArticleResponse.model_validate(a) for a in articles],
            page_info=page_info,
        )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to query news: {str(e)}",
        )


@router.get("/{article_id}", response_model=NewsAnalysisResponse)
async def get_news_article_with_analysis(
    article_id: int,
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await db.execute(
            select(NewsArticle).where(NewsArticle.id == article_id)
        )
        article = result.scalars().first()

        if not article:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"News article with id {article_id} not found",
            )

        analysis = await get_news_analysis(article_id)

        article_response = NewsArticleResponse.model_validate(article)

        if analysis:
            analysis_response = NewsAnalysisResultResponse.model_validate(analysis)
        else:
            analysis_response = None

        return NewsAnalysisResponse(
            article=article_response,
            analysis=analysis_response,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get news article: {str(e)}",
        )


@router.get("/{article_id}/analysis", response_model=NewsAnalysisResultResponse)
async def get_news_analysis_only(
    article_id: int,
    db: AsyncSession = Depends(get_db),
):
    try:
        analysis = await get_news_analysis(article_id)

        if not analysis:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No analysis found for article {article_id}",
            )

        return NewsAnalysisResultResponse.model_validate(analysis)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get analysis: {str(e)}",
        )
